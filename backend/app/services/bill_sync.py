"""阿里云账单同步服务。

设计要点（参考 CloudExplorer finance-management 的同步思路，自行实现）：
1. 按天拉取：BSS OpenAPI DescribeInstanceBill，Granularity=DAILY，逐日分页拉取；
2. 幂等落库：同一账号同一天先删后插，账单延迟出账时重复同步安全；
3. 凭证复用：AK/SK 来自凭据保险箱（credentials.content，加密存储）；
4. 限流保护：BSS QPS 有限，分页之间加短间隔。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.db.database import SessionLocal
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.services.application_time import format_app_datetime, now_dt
from app.services.leases import acquire_lease, lease_owner, release_lease

# 阿里云 BSS 账单只支持查询近 18 个月；拉取分页上限 300。
BILL_PAGE_SIZE = 300
PAGE_INTERVAL_SECONDS = 0.15
# 跨进程同步租约有效期（秒）：正常单账号同步远小于该值；
# 实例崩溃后最多 30 分钟即可被其他实例重新接管。
BILL_SYNC_LEASE_TTL_SECONDS = 1800

logger = logging.getLogger(__name__)

_sync_locks: dict[int, threading.Lock] = {}
_sync_locks_guard = threading.Lock()


def _account_lock(account_id: int) -> threading.Lock:
    with _sync_locks_guard:
        if account_id not in _sync_locks:
            _sync_locks[account_id] = threading.Lock()
        return _sync_locks[account_id]


def month_cycle(value: date | datetime | str | None = None) -> str:
    """返回 YYYY-MM 格式账期。"""
    if isinstance(value, str) and len(value) >= 7:
        return value[:7]
    dt = value or now_dt()
    return f"{dt.year:04d}-{dt.month:02d}"


def month_days(cycle: str, up_to_today: bool = True) -> list[str]:
    """枚举一个账期内的出账日期，未来日期跳过（尚无数据）。"""
    year, month = int(cycle[:4]), int(cycle[5:7])
    first = date(year, month, 1)
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    if up_to_today:
        today = now_dt().date()
        last = min(last, today)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def parse_access_key_content(raw: str) -> tuple[str, str]:
    """兼容 JSON / key=value 两种 AK/SK 存储格式（与资产云导入保持一致）。"""
    text = (raw or "").strip()
    if not text:
        return "", ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            ak = str(parsed.get("access_key") or parsed.get("accessKey") or parsed.get("AccessKeyId") or "").strip()
            sk = str(parsed.get("secret_key") or parsed.get("secretKey") or parsed.get("AccessKeySecret") or "").strip()
            return ak, sk
    except Exception:
        pass
    kv: dict[str, str] = {}
    for line in text.splitlines():
        row = line.strip()
        if not row:
            continue
        if "=" in row:
            k, v = row.split("=", 1)
        elif ":" in row:
            k, v = row.split(":", 1)
        else:
            continue
        kv[k.strip().lower()] = v.strip().strip('"').strip("'")
    ak = kv.get("access_key") or kv.get("accesskey") or kv.get("accesskeyid") or ""
    sk = kv.get("secret_key") or kv.get("secretkey") or kv.get("accesskeysecret") or ""
    return ak.strip(), sk.strip()


def load_account_keys(db: Session, account: CloudAccount) -> tuple[str, str, str]:
    """从凭据保险箱解析云账号 AK/SK，返回 (ak, sk, error)。"""
    if not account.credential_ref:
        return "", "", "未绑定 AK/SK 凭证"
    cred = db.query(Credential).filter(Credential.cred_id == account.credential_ref).first()
    if not cred:
        return "", "", "绑定的凭证不存在或已被删除"
    raw = decrypt_secret(cred.content or "")
    ak, sk = parse_access_key_content(raw)
    if len(ak) < 10 or len(sk) < 10:
        return "", "", "凭证内容无法解析出 AccessKey/SecretKey"
    return ak, sk, ""


def build_bss_client(access_key: str, secret_key: str):
    from alibabacloud_bssopenapi20171214.client import Client as BssClient
    from alibabacloud_tea_openapi import models as open_api_models

    config = open_api_models.Config(
        access_key_id=access_key,
        access_key_secret=secret_key,
        endpoint="business.aliyuncs.com",
    )
    return BssClient(config)


def _s(item: Any, attr: str) -> str:
    value = getattr(item, attr, None)
    return str(value) if value is not None and str(value) != "None" else ""


def _f(item: Any, attr: str) -> float:
    value = getattr(item, attr, None)
    try:
        return round(float(value or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _parse_bill_tags(raw: Any) -> str:
    """解析 BSS 返回的标签字符串。

    接口给的 Tag 不是对象数组，而是形如
    `key:acs:ros:stackId value:4fa9...;key:env value:prod` 的字符串。
    按 `;` 分段，每段用 `key:` 取到下一个空格为止、`value:` 取到段尾，
    这样值里带空格（如中文备注）也不会被切断。
    """
    text = str(raw or "").strip()
    if not text:
        return "[]"
    pairs: list[dict[str, str]] = []
    for segment in text.split(";"):
        seg = segment.strip()
        if not seg:
            continue
        key_match = re.search(r"key:(\S*)", seg)
        value_match = re.search(r"value:(.*)$", seg)
        key = key_match.group(1) if key_match else ""
        value = (value_match.group(1) or "").strip() if value_match else ""
        if key:
            pairs.append({"key": key, "value": value})
    return json.dumps(pairs, ensure_ascii=False)


def _dedup_key(item: Any, seq: int) -> str:
    basis = "|".join(
        [
            _s(item, "instance_id"),
            _s(item, "product_code"),
            _s(item, "billing_item"),
            _s(item, "subscription_type"),
            _s(item, "region"),
            _s(item, "bill_account_id"),
            str(seq),
        ]
    )
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:32]


def _row_from_item(account: CloudAccount, item: Any, billing_date: str) -> CloudBill:
    # 属性名必须是 SDK 模型的 snake_case（ProductName/PretaxAmount 这类 PascalCase
    # 只是 to_map() 的 JSON 键，getattr 取不到，曾导致整表金额与产品名全空）。
    return CloudBill(
        account_id=account.id,
        billing_cycle=billing_date[:7],
        billing_date=billing_date,
        granularity="DAILY",
        provider=account.provider or "aliyun",
        region=_s(item, "region"),
        zone=_s(item, "zone"),
        product_code=_s(item, "product_code"),
        product_name=_s(item, "product_name"),
        product_detail=_s(item, "product_detail"),
        subscription_type=_s(item, "subscription_type"),
        billing_item_code=_s(item, "billing_item_code") or _s(item, "pip_code"),
        # 接口没有 BillingItemName，计费项中文名在 item_name（如「后付费账单」）
        billing_item_name=_s(item, "item_name"),
        instance_id=_s(item, "instance_id"),
        # 接口没有 ResourceName，实例备注名是最接近的字段
        resource_name=_s(item, "nick_name"),
        resource_group=_s(item, "resource_group"),
        cost_unit=_s(item, "cost_unit"),
        tags=_parse_bill_tags(getattr(item, "tag", None)),
        pretax_amount=_f(item, "pretax_amount"),
        # DescribeInstanceBill 不返回 PaymentAmount/CashAmount（to_map 里就没这两个键），
        # 实付金额只能用 QueryBillOverview 另外取；这里保持 0，不要凭猜映射成别的字段。
        payment_amount=_f(item, "payment_amount"),
        cash_amount=_f(item, "cash_amount"),
        deducted_by_coupons=_f(item, "deducted_by_coupons"),
        currency=_s(item, "currency") or "CNY",
    )


def fetch_daily_bills(client: Any, billing_date: str) -> tuple[list[Any], str]:
    """拉取某一天的实例账单明细，返回 (items, owner_id)。"""
    from alibabacloud_bssopenapi20171214 import models as bss_models

    items: list[Any] = []
    owner_id = ""
    next_token = ""
    while True:
        request = bss_models.DescribeInstanceBillRequest(
            billing_cycle=billing_date[:7],
            billing_date=billing_date,
            granularity="DAILY",
            max_results=BILL_PAGE_SIZE,
            next_token=next_token,
        )
        response = client.describe_instance_bill_with_options(request, _runtime())
        body = getattr(response, "body", None)
        data = getattr(body, "data", None) if body else None
        if owner_id == "":
            owner_id = _s(data, "account_id") or _s(data, "account_name")
        page_items = list(getattr(data, "items", None) or [])
        items.extend(page_items)
        next_token = _s(data, "next_token")
        if not next_token or not page_items:
            break
        time.sleep(PAGE_INTERVAL_SECONDS)
    return items, owner_id


def _runtime():
    from alibabacloud_tea_util import models as util_models

    return util_models.RuntimeOptions(
        read_timeout=30000,
        connect_timeout=10000,
        autoretry=True,
        max_attempts=3,
    )


def verify_account_credentials(access_key: str, secret_key: str) -> tuple[bool, str, str]:
    """校验 AK/SK 能否访问 BSS（拉当月第一天的账单），返回 (ok, message, owner_id)。"""
    try:
        client = build_bss_client(access_key, secret_key)
        first_day = f"{month_cycle()}-01"
        items, owner_id = fetch_daily_bills(client, first_day)
        return True, "凭证有效", owner_id
    except Exception as exc:  # noqa: BLE001
        message = _friendly_bss_error(exc)
        return False, message, ""


def _friendly_bss_error(exc: Exception) -> str:
    raw = str(exc)
    if "InvalidAccessKeyId" in raw or "AccessKey" in raw and "not exist" in raw:
        return "AccessKey 不存在或已禁用"
    if "SignatureDoesNotMatch" in raw:
        return "SecretKey 校验失败（签名不匹配）"
    if "Forbidden" in raw or "no permission" in raw.lower() or "NotAuthorized" in raw:
        return "AK/SK 缺少费用中心只读权限，请授予 AliyunBSSReadOnlyAccess"
    if "Throttling" in raw:
        return "触发阿里云限流，请稍后重试"
    return raw[:300]


def sync_account_cycle(account_id: int, cycle: str, trigger: str = "manual") -> BillSyncRun | None:
    """同步一个云账号一个账期的账单（阻塞执行，供线程调用）。

    并发保护分两层：进程内 threading.Lock 拦截同进程重复触发；
    task_leases 数据库租约（bill-sync:{account_id}，TTL 1800 秒）拦截
    多 worker 部署下跨进程的定时/手动并发同步。

    Args:
        account_id: cloud_accounts.id。
        cycle: 账期（YYYY-MM）。
        trigger: 触发来源（manual/scheduled）。

    Returns:
        同步记录；账号不存在或同进程已有同步在跑时返回 None（静默跳过）；
        跨进程租约被其他实例持有时写一条 status='skipped' 的同步记录再返回
        None——手动同步至少能在页面看到"被跳过"，而不是毫无反应。
    """
    db = SessionLocal()
    lock = _account_lock(account_id)
    lease_key = f"bill-sync:{account_id}"
    owner = lease_owner()
    lease_held = False
    try:
        if not lock.acquire(blocking=False):
            return None
        # 跨进程租约：拿不到说明其他实例正在同步同一账号，写 skipped 记录后
        # 跳过；TTL 到期自动兜底，实例崩溃也不会永久卡住同步。
        if not acquire_lease(db, lease_key, owner, ttl_seconds=BILL_SYNC_LEASE_TTL_SECONDS):
            logger.debug("账单同步租约 %s 被其他实例持有，跳过本次同步", lease_key)
            _record_skipped_run(db, account_id, cycle, trigger)
            return None
        lease_held = True
        account = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
        if not account:
            return None
        run = BillSyncRun(
            account_id=account_id,
            billing_cycle=cycle,
            status="running",
            trigger=trigger,
            started_at=format_app_datetime(now_dt()),
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        access_key, secret_key, err = load_account_keys(db, account)
        if err:
            run.status = "failed"
            run.error_message = err
            run.finished_at = format_app_datetime(now_dt())
            account.status = "error"
            account.status_message = err
            db.commit()
            return run

        try:
            client = build_bss_client(access_key, secret_key)
            total_rows = 0
            owner_id = ""
            for billing_date in month_days(cycle):
                items, day_owner = fetch_daily_bills(client, billing_date)
                if day_owner and not owner_id:
                    owner_id = day_owner
                if not items:
                    continue
                # 幂等：同账号同日先清后写，覆盖延迟出账造成的金额修正。
                db.query(CloudBill).filter(
                    CloudBill.account_id == account_id,
                    CloudBill.billing_date == billing_date,
                ).delete(synchronize_session=False)
                rows = []
                for seq, item in enumerate(items):
                    row = _row_from_item(account, item, billing_date)
                    row.dedup_key = _dedup_key(item, seq)
                    rows.append(row)
                db.add_all(rows)
                total_rows += len(rows)
                db.commit()
                time.sleep(PAGE_INTERVAL_SECONDS)

            run.status = "success"
            run.rows_synced = total_rows
            account.status = "active"
            account.status_message = ""
            if owner_id and not account.vendor_account_id:
                account.vendor_account_id = owner_id
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            run.status = "failed"
            run.error_message = _friendly_bss_error(exc)
            account.status = "error"
            account.status_message = run.error_message or "同步失败"

        run.finished_at = format_app_datetime(now_dt())
        account.last_sync_at = run.finished_at
        db.commit()
        db.refresh(run)
        return run
    finally:
        if lease_held:
            # 同步结束后立即释放，手动重试无需等 TTL；owner 不匹配（已被
            # 其他实例抢占）时为幂等空操作。
            try:
                release_lease(db, lease_key, owner)
            except Exception as exc:  # noqa: BLE001
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("账单同步租约 %s 释放失败（将等待过期自动失效）: %s", lease_key, exc)
        try:
            lock.release()
        except RuntimeError:
            pass
        db.close()


def _record_skipped_run(db: Session, account_id: int, cycle: str, trigger: str) -> None:
    """租约被占时写一条 status='skipped' 的占位同步记录（best-effort）。

    手动触发同步走的是后台线程，调用方拿不到返回值；没有这条记录时
    "点了同步但页面毫无变化"。账号已不存在则不写（避免脏数据），
    写失败只记日志，不影响调用方。
    """
    try:
        exists = db.query(CloudAccount.id).filter(CloudAccount.id == account_id).first()
        if not exists:
            return
        now_text = format_app_datetime(now_dt())
        db.add(BillSyncRun(
            account_id=account_id,
            billing_cycle=cycle,
            status="skipped",
            trigger=trigger,
            rows_synced=0,
            error_message="另一同步正在进行，本次同步被跳过",
            started_at=now_text,
            finished_at=now_text,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("账单同步 skipped 记录写入失败（account=%s）: %s", account_id, exc)


def run_sync_in_background(account_id: int, cycles: Iterable[str], trigger: str = "manual") -> None:
    """在独立线程中逐账期同步（FastAPI 手动触发场景）。"""
    def worker() -> None:
        for cycle in cycles:
            sync_account_cycle(account_id, cycle, trigger=trigger)

    threading.Thread(target=worker, daemon=True, name=f"bill-sync-{account_id}").start()


async def start_bill_sync_runner() -> None:
    """定时同步 runner：每 6 小时刷新所有启用账号的当月与上月账单。"""
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "bill_sync",
        "云账单同步",
        "每 6 小时同步所有启用云账号的当月与上月账单（阿里云 BSS 出账约有 1 天延迟）",
        6 * 3600,
    )
    while True:
        with track_run("bill_sync"):
            try:
                await asyncio.to_thread(_sync_all_enabled_accounts)
            except Exception as exc:  # noqa: BLE001
                # 单账号失败在 sync_account_cycle 内已记录，这里兜底防 runner 退出。
                logger.warning(f"[Bill Sync] runner 轮次异常: {exc}")
        await asyncio.sleep(6 * 3600)


def _sync_all_enabled_accounts() -> None:
    db = SessionLocal()
    try:
        accounts = db.query(CloudAccount).filter(CloudAccount.enabled == 1).all()
        ids = [a.id for a in accounts]
    finally:
        db.close()
    current = month_cycle()
    prev_cycle = _previous_cycle(current)
    for account_id in ids:
        sync_account_cycle(account_id, current, trigger="scheduled")
        sync_account_cycle(account_id, prev_cycle, trigger="scheduled")


def _previous_cycle(cycle: str) -> str:
    year, month = int(cycle[:4]), int(cycle[5:7])
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"
