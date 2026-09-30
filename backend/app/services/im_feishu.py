"""飞书 IM 服务：应用凭证管理 / 身份解析 / 群自定义机器人 @ 通知。

【架构定位（第一版范围）】
 平台事件通知到人的出口。所有用户站内通知（create_notification）与告警派发
 （dispatch_active_alerts）在完成自身逻辑后，调用本模块把消息 @ 到飞书群。

 第一版只做"群自定义机器人 + @本人"，刻意不做：
 1. 私信——飞书自定义机器人没有私信能力，需自建应用消息接口（规划 P2）；
 2. 卡片回调（在 IM 里审批/认领）——需公网可达地址（规划 P3）。

【安全边界】
 1. open_id 只允许由后端用 app 凭证调通讯录接口解析，前端不能直填，
    防止冒用他人身份接收通知或在群里 @ 别人；
 2. tenant_access_token 进程内缓存并提前 5 分钟过期，避免每发一条消息
    就换一次 token（飞书该接口有频控）；
 3. app_secret 存储于 system_configs，读取接口一律不回明文；
 4. 所有外呼 requests 8s 超时，任何失败只记日志，绝不影响业务主流程。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

import requests

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 系统配置键：飞书应用凭证 + 默认群机器人
IM_FEISHU_CONFIG_KEY = "im_feishu_config"
# 飞书开放平台域名（国内）
FEISHU_BASE = "https://open.feishu.cn"
# 外呼统一超时（与告警派发一致）
HTTP_TIMEOUT_SECONDS = 8
# token 缓存提前过期余量
TOKEN_EXPIRE_MARGIN_SECONDS = 300
# 单次 @ 通知最多携带的用户数（防误配置刷屏）
MAX_NOTIFY_USERS = 20


class FeishuError(RuntimeError):
    """飞书接口调用失败（携带可读原因，供前端展示）。"""


# ── 配置读写 ──────────────────────────────────────────────────────────────────


def _read_config_row(db: Session):
    """读取飞书配置行（不存在返回 None）。"""
    from app.models.system_config import SystemConfig

    return db.query(SystemConfig).filter(SystemConfig.config_key == IM_FEISHU_CONFIG_KEY).first()


def read_feishu_config(db: Session) -> dict[str, str]:
    """读取飞书配置；secret 一律不回传明文，只返回是否已配置。"""
    row = _read_config_row(db)
    payload: dict[str, Any] = {}
    if row is not None:
        import json

        try:
            parsed = json.loads(row.config_value or "{}")
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:  # noqa: BLE001  脏数据按未配置处理
            payload = {}
    return {
        "app_id": str(payload.get("app_id") or "").strip(),
        "app_secret_set": bool(str(payload.get("app_secret") or "").strip()),
        "default_webhook_url": str(payload.get("default_webhook_url") or "").strip(),
    }


def save_feishu_config(
    db: Session,
    *,
    app_id: str,
    app_secret: str,
    default_webhook_url: str,
) -> dict[str, str]:
    """保存飞书配置。app_secret 传空字符串表示保持原值（表单不与否决回显）。"""
    import json

    from app.models.system_config import SystemConfig

    row = _read_config_row(db)
    old: dict[str, Any] = {}
    if row is not None:
        try:
            parsed = json.loads(row.config_value or "{}")
            if isinstance(parsed, dict):
                old = parsed
        except Exception:  # noqa: BLE001
            old = {}

    new_secret = str(app_secret or "").strip() or str(old.get("app_secret") or "").strip()
    content = json.dumps(
        {
            "app_id": str(app_id or "").strip()[:80],
            "app_secret": new_secret,
            "default_webhook_url": str(default_webhook_url or "").strip()[:500],
        },
        ensure_ascii=False,
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if row is None:
        row = SystemConfig(config_key=IM_FEISHU_CONFIG_KEY, config_value=content, updated_at=now)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now
    db.commit()
    # 凭证可能已变更，立即作废 token 缓存
    invalidate_token_cache()
    return read_feishu_config(db)


# ── tenant_access_token 缓存 ──────────────────────────────────────────────────

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "expire_at": 0.0, "app_id": ""}


def invalidate_token_cache() -> None:
    """作废 token 缓存（配置变更后调用）。"""
    with _token_lock:
        _token_cache.update({"token": "", "expire_at": 0.0, "app_id": ""})


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """获取 tenant_access_token，进程内缓存并提前 5 分钟过期。"""
    if not app_id or not app_secret:
        raise FeishuError("尚未配置飞书应用凭证（App ID / App Secret）")

    with _token_lock:
        cached_app = str(_token_cache.get("app_id") or "")
        if _token_cache.get("token") and cached_app == app_id and float(_token_cache.get("expire_at") or 0) > time.time():
            return str(_token_cache["token"])

    try:
        resp = requests.post(
            f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FeishuError(f"连接飞书开放平台失败: {exc}") from exc

    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise FeishuError(f"获取飞书 token 失败: {data.get('msg') or data}")

    token = str(data["tenant_access_token"])
    expire_in = int(data.get("expire") or 7200)
    with _token_lock:
        _token_cache.update(
            {
                "token": token,
                "expire_at": time.time() + max(expire_in - TOKEN_EXPIRE_MARGIN_SECONDS, 60),
                "app_id": app_id,
            }
        )
    return token


# ── 身份解析 ──────────────────────────────────────────────────────────────────


def resolve_open_id(app_id: str, app_secret: str, identity: str) -> str:
    """按手机号或邮箱解析飞书 open_id。

    复杂逻辑中文注释：
    - 飞书通讯录批量查询接口按 mobiles / emails 维度查询，需根据输入形态
      选择字段；输入含 @ 视为邮箱，否则按手机号处理；
    - 返回体里 user_id 字段即 open_id（ou_ 前缀），个别租户返回 open_id 字段，
      两者都兼容；
    - 解析不到（用户不在该企业内）必须报错，绝不允许退回空 open_id 发送。
    """
    identity = str(identity or "").strip()
    if not identity:
        raise FeishuError("请输入用于绑定的手机号或邮箱")

    token = get_tenant_access_token(app_id, app_secret)
    is_email = "@" in identity
    body = {"emails": [identity]} if is_email else {"mobiles": [identity]}
    try:
        resp = requests.post(
            f"{FEISHU_BASE}/open-apis/contact/v3/users/batch_get_id",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FeishuError(f"连接飞书通讯录接口失败: {exc}") from exc

    if data.get("code") != 0:
        raise FeishuError(f"飞书身份解析失败: {data.get('msg') or data}")

    users = (data.get("data") or {}).get("user_list") or []
    for item in users:
        if not isinstance(item, dict):
            continue
        open_id = str(item.get("user_id") or item.get("open_id") or "").strip()
        if open_id:
            return open_id
    raise FeishuError(f"未能在飞书通讯录中找到 {identity}，请确认该账号已加入企业组织")


# ── 群消息发送 ────────────────────────────────────────────────────────────────


def _build_at_text(open_id: str, title: str, content: str, link: str) -> str:
    """构造飞书自定义机器人文本消息（@ 本人 + 标题 + 详情 + 平台链接）。"""
    lines = [f'<at user_id="{open_id}"></at>']
    if title:
        lines.append(f"【{title}】")
    if content:
        lines.append(str(content))
    if link:
        lines.append(f"查看详情: {link}")
    return "\n".join(lines)


def send_group_at(webhook_url: str, open_id: str, title: str, content: str, link: str = "") -> dict[str, Any]:
    """向群自定义机器人发送 @ 本人的文本消息。"""
    webhook_url = str(webhook_url or "").strip()
    if not webhook_url:
        raise FeishuError("未配置群机器人 Webhook 地址")
    if not webhook_url.startswith(("http://", "https://")):
        raise FeishuError("群机器人 Webhook 地址必须是 http(s) 地址")
    if not open_id:
        raise FeishuError("缺少飞书 open_id")

    try:
        resp = requests.post(
            webhook_url,
            json={"msg_type": "text", "content": {"text": _build_at_text(open_id, title, content, link)}},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FeishuError(f"发送飞书群消息失败: {exc}") from exc

    # 飞书自定义机器人成功返回 {"code":0,"msg":"success"}（旧版返回 {"StatusCode":0}）
    if isinstance(data, dict) and (data.get("code") == 0 or data.get("StatusCode") == 0):
        return {"ok": True, "message": "已发送"}
    raise FeishuError(f"飞书群机器人拒收: {data.get('msg') or data.get('StatusMessage') or data}")


# ── 绑定读取与分发 ────────────────────────────────────────────────────────────


def get_binding(db: Session, user_id: int):
    """读取用户当前绑定（第一版平台固定 feishu）。"""
    from app.models.user_im_binding import UserImBinding

    return (
        db.query(UserImBinding)
        .filter(UserImBinding.user_id == user_id, UserImBinding.platform == "feishu")
        .first()
    )


def fanout_user_notification(db: Session, *, user_id: int, title: str, content: str, link: str = "") -> dict[str, Any]:
    """把一条用户通知 @ 到飞书群（best-effort，失败不影响站内通知主流程）。

    复杂逻辑中文注释：
    1. 未绑定 / 通知方式为 off / 既无个人群机器人也无系统默认群机器人，
       三种情况都静默跳过——个人渠道是用户主动行为，缺配置不应报错刷屏；
    2. webhook 选择优先级：个人群机器人 > 系统默认群机器人；
    3. 本函数抛出的任何异常由调用方捕获，这里只做日志。
    """
    binding = get_binding(db, user_id)
    if binding is None or not binding.open_id:
        return {"ok": False, "skipped": "未绑定"}
    if str(binding.notify_mode or "group_at") != "group_at":
        return {"ok": False, "skipped": "已关闭"}

    webhook_url = str(binding.webhook_url or "").strip()
    if not webhook_url:
        webhook_url = read_feishu_config(db)["default_webhook_url"]
    if not webhook_url:
        return {"ok": False, "skipped": "未配置群机器人"}

    try:
        return send_group_at(webhook_url, binding.open_id, title, content, link)
    except FeishuError as exc:
        logger.info("飞书群 @ 通知跳过(user=%s): %s", user_id, exc)
        return {"ok": False, "error": str(exc)}


def notify_users_alert(
    db: Session,
    user_ids: list[int],
    *,
    title: str,
    content: str,
    link: str = "",
) -> dict[str, Any]:
    """告警派发专用：给规则配置的通知人列表批量 @（best-effort）。

    与 fanout_user_notification 的差别：按 user_ids 列表发，且尊重每条
    绑定的个人群机器人；未绑定/关闭的用户逐个跳过并计入 skipped。
    """
    sent, skipped, failed = 0, 0, 0
    default_webhook = read_feishu_config(db)["default_webhook_url"]
    for uid in list(dict.fromkeys(user_ids))[:MAX_NOTIFY_USERS]:
        binding = get_binding(db, int(uid))
        if binding is None or not binding.open_id or str(binding.notify_mode or "") != "group_at":
            skipped += 1
            continue
        webhook_url = str(binding.webhook_url or "").strip() or default_webhook
        if not webhook_url:
            skipped += 1
            continue
        try:
            send_group_at(webhook_url, binding.open_id, title, content, link)
            sent += 1
        except FeishuError as exc:
            failed += 1
            logger.info("告警飞书 @ 通知失败(user=%s): %s", uid, exc)
    return {"sent": sent, "skipped": skipped, "failed": failed}
