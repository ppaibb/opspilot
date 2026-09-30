"""费用管理接口（第一版：阿里云 BSS 账单 + 预算告警 + 实例费用联动）。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.models.system_config import SystemConfig
from app.models.user import User
from app.schemas.finance import (
    BillSyncRequest,
    BillSyncRunResponse,
    CloudAccountCreate,
    CloudAccountResponse,
    CloudBillItem,
    CloudBillListResponse,
    CloudBillGroupResponse,
    CredentialOptionResponse,
    FinanceDimensionItem,
    FinanceOverviewResponse,
    FinanceTrendPoint,
)
from app.services.application_time import format_app_datetime, now_dt
from app.services.bill_sync import (
    load_account_keys,
    month_cycle,
    month_days,
    run_sync_in_background,
    sync_account_cycle,
    verify_account_credentials,
)
from app.services.credential_access import filter_credentials_for_user

router = APIRouter()

# 预算配置存储键（system_configs 表 JSON；避免为单个配置项建表迁移）。
BUDGET_CONFIG_KEY = "finance_budget_config"
# 预算默认值：不设预算（0 = 禁用），告警阈值 80% 警告 / 100% 严重。
DEFAULT_BUDGET_CONFIG: dict[str, Any] = {
    "monthly_budget": 0.0,
    "warn_ratio": 0.8,
    "critical_ratio": 1.0,
    "enabled": False,
    "updated_at": "",
}


def _read_budget_config(db: Session) -> dict[str, Any]:
    """读取预算配置，缺省字段回落默认值（兼容配置项逐步演进）。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == BUDGET_CONFIG_KEY).first()
    config = dict(DEFAULT_BUDGET_CONFIG)
    if row and row.config_value:
        try:
            stored = json.loads(row.config_value)
            if isinstance(stored, dict):
                config.update({k: v for k, v in stored.items() if k in DEFAULT_BUDGET_CONFIG})
        except Exception:  # noqa: BLE001
            pass
    return config


def _write_budget_config(db: Session, config: dict[str, Any]) -> None:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == BUDGET_CONFIG_KEY).first()
    if not row:
        row = SystemConfig(config_key=BUDGET_CONFIG_KEY)
        db.add(row)
    row.config_value = json.dumps(config, ensure_ascii=False)
    row.updated_at = format_app_datetime(now_dt())
    db.commit()


def _serialize_account(db: Session, account: CloudAccount) -> CloudAccountResponse:
    cred_name = ""
    if account.credential_ref:
        cred = db.query(Credential).filter(Credential.cred_id == account.credential_ref).first()
        cred_name = cred.name if cred else ""
    return CloudAccountResponse(
        id=account.id,
        name=account.name or "",
        provider=account.provider or "aliyun",
        credential_ref=account.credential_ref or "",
        credential_name=cred_name,
        vendor_account_id=account.vendor_account_id or "",
        status=account.status or "pending",
        status_message=account.status_message or "",
        last_sync_at=account.last_sync_at or "",
        enabled=bool(account.enabled),
        created_at=account.created_at or "",
    )


@router.get("/credentials-options", response_model=list[CredentialOptionResponse])
def list_credential_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """可绑定的 AK/SK 凭证下拉（仅 AK/SK 类型，且对当前用户可见）。"""
    creds = db.query(Credential).all()
    options = []
    for cred in filter_credentials_for_user(creds, current_user):
        ctype = (cred.type or "").lower()
        if "key" not in ctype and "ak" not in ctype:
            continue
        options.append(
            CredentialOptionResponse(cred_id=cred.cred_id or "", name=cred.name or "", type=cred.type or "")
        )
    return options


@router.get("/accounts", response_model=list[CloudAccountResponse])
def list_cloud_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accounts = db.query(CloudAccount).order_by(CloudAccount.id.desc()).all()
    return [_serialize_account(db, a) for a in accounts]


@router.post("/accounts", response_model=CloudAccountResponse)
def create_cloud_account(
    payload: CloudAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.accounts.manage")),
    _risk=Depends(require_risk_ack("action.finance.accounts.manage", "create")),
):
    """新增云账号（finance:create 二次确认；创建即拉取账单校验凭证）。"""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="账号名称不能为空")
    if not payload.credential_ref:
        raise HTTPException(status_code=422, detail="请选择 AK/SK 凭证")
    if payload.provider != "aliyun":
        raise HTTPException(status_code=422, detail="第一版仅支持阿里云账号")

    account = CloudAccount(
        name=name,
        provider="aliyun",
        credential_ref=payload.credential_ref,
        enabled=1 if payload.enabled else 0,
        status="pending",
        created_at=format_app_datetime(now_dt()),
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    # 创建即校验一次凭证（失败不阻断创建，反馈在状态里）。
    _verify_and_update(db, account)
    db.commit()
    db.refresh(account)
    return _serialize_account(db, account)


@router.put("/accounts/{account_id}", response_model=CloudAccountResponse)
def update_cloud_account(
    account_id: int,
    payload: CloudAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.accounts.manage")),
):
    account = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="云账号不存在")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="账号名称不能为空")
    credential_changed = account.credential_ref != payload.credential_ref
    account.name = name
    account.credential_ref = payload.credential_ref
    account.enabled = 1 if payload.enabled else 0
    if credential_changed:
        account.status = "pending"
        account.status_message = ""
        _verify_and_update(db, account)
    db.commit()
    db.refresh(account)
    return _serialize_account(db, account)


@router.delete("/accounts/{account_id}")
def delete_cloud_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.accounts.manage")),
    _risk=Depends(require_risk_ack("action.finance.accounts.manage", "delete")),
):
    """删除云账号（高危：连带清空账单明细与同步记录，需 finance:delete 确认）。"""
    account = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="云账号不存在")
    db.query(CloudBill).filter(CloudBill.account_id == account_id).delete(synchronize_session=False)
    db.query(BillSyncRun).filter(BillSyncRun.account_id == account_id).delete(synchronize_session=False)
    db.delete(account)
    db.commit()
    return {"detail": "云账号及其账单数据已删除"}


@router.post("/accounts/{account_id}/verify", response_model=CloudAccountResponse)
def verify_cloud_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.accounts.manage")),
):
    account = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="云账号不存在")
    _verify_and_update(db, account)
    db.commit()
    db.refresh(account)
    return _serialize_account(db, account)


def _verify_and_update(db: Session, account: CloudAccount) -> None:
    access_key, secret_key, err = load_account_keys(db, account)
    if err:
        account.status = "error"
        account.status_message = err
        return
    ok, message, owner_id = verify_account_credentials(access_key, secret_key)
    account.status = "active" if ok else "error"
    account.status_message = "" if ok else message
    if owner_id and not account.vendor_account_id:
        account.vendor_account_id = owner_id


@router.post("/accounts/{account_id}/sync")
def trigger_account_sync(
    account_id: int,
    payload: BillSyncRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.sync")),
):
    account = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="云账号不存在")
    if account.status == "error" and not account.credential_ref:
        raise HTTPException(status_code=422, detail="账号凭证未配置，无法同步")

    payload = payload or BillSyncRequest()
    cycles = [payload.billing_cycle or month_cycle()]
    if payload.include_previous:
        current = cycles[0]
        year, month = int(current[:4]), int(current[5:7])
        cycles.append(f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}")

    run_sync_in_background(account_id, cycles, trigger="manual")
    return {"detail": "同步任务已提交", "billing_cycles": cycles}


@router.get("/accounts/{account_id}/sync-records", response_model=list[BillSyncRunResponse])
def list_sync_records(
    account_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    runs = (
        db.query(BillSyncRun)
        .filter(BillSyncRun.account_id == account_id)
        .order_by(BillSyncRun.id.desc())
        .limit(limit)
        .all()
    )
    return [
        BillSyncRunResponse(
            id=run.id,
            account_id=run.account_id,
            billing_cycle=run.billing_cycle or "",
            status=run.status or "",
            trigger=run.trigger or "",
            rows_synced=run.rows_synced or 0,
            error_message=run.error_message or "",
            started_at=run.started_at or "",
            finished_at=run.finished_at or "",
        )
        for run in runs
    ]


def _bill_query(db: Session, month: str | None, account_id: int | None):
    query = db.query(CloudBill)
    if month:
        query = query.filter(CloudBill.billing_cycle == month)
    if account_id:
        query = query.filter(CloudBill.account_id == account_id)
    return query


def _account_names(db: Session) -> dict[int, str]:
    return {a.id: (a.name or f"账号#{a.id}") for a in db.query(CloudAccount).all()}


@router.get("/overview", response_model=FinanceOverviewResponse)
def finance_overview(
    month: str = Query(default=""),
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_month = (month or month_cycle())[:7]
    year, mon = int(target_month[:4]), int(target_month[5:7])
    prev_month = f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"

    def _total(cycle: str) -> float:
        query = _bill_query(db, cycle, account_id)
        value = query.with_entities(func.sum(CloudBill.pretax_amount)).scalar()
        return round(float(value or 0.0), 2)

    total = _total(target_month)
    prev_total = _total(prev_month)
    mom_delta = round(total - prev_total, 2) if prev_total else 0.0

    def _dimension(cycle: str, column, label_map: dict[int, str] | None = None) -> list[FinanceDimensionItem]:
        query = _bill_query(db, cycle, account_id)
        rows = (
            query.with_entities(column, func.sum(CloudBill.pretax_amount))
            .group_by(column)
            .all()
        )
        items = []
        for key, amount in rows:
            name = str(key or "").strip() or "未分类"
            if label_map and str(key).isdigit():
                name = label_map.get(int(key), name)
            items.append((name, round(float(amount or 0.0), 2)))
        items.sort(key=lambda pair: pair[1], reverse=True)
        return [
            FinanceDimensionItem(name=name, amount=amount, ratio=round(amount / total, 4) if total else 0.0)
            for name, amount in items
        ]

    names = _account_names(db)
    by_account = _dimension(target_month, CloudBill.account_id, names)
    by_product = _dimension(target_month, CloudBill.product_name)[:12]
    by_subscription = _dimension(target_month, CloudBill.subscription_type)

    daily_rows = (
        _bill_query(db, target_month, account_id)
        .with_entities(CloudBill.billing_date, func.sum(CloudBill.pretax_amount))
        .group_by(CloudBill.billing_date)
        .order_by(CloudBill.billing_date)
        .all()
    )
    daily_trend = [FinanceTrendPoint(period=row[0], amount=round(float(row[1] or 0.0), 2)) for row in daily_rows]

    monthly_trend: list[FinanceTrendPoint] = []
    cursor_month = prev_month
    month_keys = []
    for _ in range(6):
        month_keys.insert(0, cursor_month)
        y, m = int(cursor_month[:4]), int(cursor_month[5:7])
        cursor_month = f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"
    for cycle in month_keys:
        monthly_trend.append(FinanceTrendPoint(period=cycle, amount=_total(cycle)))

    last_synced = (
        db.query(CloudAccount.last_sync_at)
        .order_by(CloudAccount.last_sync_at.desc())
        .limit(1)
        .scalar()
        or ""
    )
    return FinanceOverviewResponse(
        month=target_month,
        total=total,
        prev_total=prev_total,
        mom_delta=mom_delta,
        by_account=by_account,
        by_product=by_product,
        by_subscription=by_subscription,
        daily_trend=daily_trend,
        monthly_trend=monthly_trend,
        last_synced_at=str(last_synced),
    )


@router.get("/bills", response_model=CloudBillListResponse)
def list_cloud_bills(
    month: str = Query(default=""),
    account_id: int | None = None,
    product: str = Query(default=""),
    keyword: str = Query(default=""),
    subscription_type: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_month = (month or month_cycle())[:7]
    query = _bill_query(db, target_month, account_id)
    if product:
        query = query.filter(CloudBill.product_name.contains(product) | CloudBill.product_code.contains(product))
    if subscription_type:
        query = query.filter(CloudBill.subscription_type == subscription_type)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            CloudBill.instance_id.like(like)
            | CloudBill.resource_name.like(like)
            | CloudBill.billing_item_name.like(like)
            | CloudBill.product_name.like(like)
            | CloudBill.product_code.like(like)
        )

    total = query.count()
    grand_total = round(float(query.with_entities(func.sum(CloudBill.pretax_amount)).scalar() or 0.0), 2)
    rows = (
        query.order_by(CloudBill.pretax_amount.desc(), CloudBill.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    names = _account_names(db)
    items = [
        CloudBillItem(
            id=row.id,
            account_id=row.account_id,
            account_name=names.get(row.account_id, f"账号#{row.account_id}"),
            billing_cycle=row.billing_cycle or "",
            billing_date=row.billing_date or "",
            provider=row.provider or "aliyun",
            region=row.region or "",
            product_code=row.product_code or "",
            product_name=row.product_name or "",
            product_detail=row.product_detail or "",
            subscription_type=row.subscription_type or "",
            billing_item_code=row.billing_item_code or "",
            billing_item_name=row.billing_item_name or "",
            instance_id=row.instance_id or "",
            resource_name=row.resource_name or "",
            resource_group=row.resource_group or "",
            cost_unit=row.cost_unit or "",
            pretax_amount=row.pretax_amount or 0.0,
            payment_amount=row.payment_amount or 0.0,
            cash_amount=row.cash_amount or 0.0,
            deducted_by_coupons=row.deducted_by_coupons or 0.0,
            currency=row.currency or "CNY",
        )
        for row in rows
    ]
    return CloudBillListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        grand_total=grand_total,
    )


@router.get("/bills/export")
def export_cloud_bills(
    month: str = Query(default=""),
    account_id: int | None = None,
    product: str = Query(default=""),
    keyword: str = Query(default=""),
    subscription_type: str = Query(default=""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导出账单明细 CSV（与 /bills 同筛选条件，不分页，按金额降序）。"""
    import csv
    import io

    target_month = (month or month_cycle())[:7]
    query = _bill_query(db, target_month, account_id)
    if product:
        query = query.filter(CloudBill.product_name.contains(product) | CloudBill.product_code.contains(product))
    if subscription_type:
        query = query.filter(CloudBill.subscription_type == subscription_type)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            CloudBill.instance_id.like(like)
            | CloudBill.resource_name.like(like)
            | CloudBill.billing_item_name.like(like)
            | CloudBill.product_name.like(like)
            | CloudBill.product_code.like(like)
        )
    rows = query.order_by(CloudBill.pretax_amount.desc(), CloudBill.id.desc()).all()
    names = _account_names(db)

    headers = [
        "账期", "出账日期", "云账号", "厂商", "地域", "可用区",
        "产品编码", "产品名称", "产品明细", "计费方式",
        "计费项编码", "计费项", "实例ID", "资源名称", "资源组", "财务单元",
        "税前金额", "现金支付", "代金券",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([
            row.billing_cycle or "",
            row.billing_date or "",
            names.get(row.account_id, f"账号#{row.account_id}"),
            row.provider or "",
            row.region or "",
            row.zone or "",
            row.product_code or "",
            row.product_name or "",
            row.product_detail or "",
            row.subscription_type or "",
            row.billing_item_code or "",
            row.billing_item_name or "",
            row.instance_id or "",
            row.resource_name or "",
            row.resource_group or "",
            row.cost_unit or "",
            f"{float(row.pretax_amount or 0):.2f}",
            f"{float(row.payment_amount or 0):.2f}",
            f"{float(row.cash_amount or 0):.2f}",
        ])
    # UTF-8 BOM，Excel 直接双击打开不乱码
    payload = "﻿" + buf.getvalue()
    filename = f"cloud-bills-{target_month}.csv"
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/bills/group-by", response_model=CloudBillGroupResponse)
def group_cloud_bills(
    dimension: str = Query(default="product"),
    month: str = Query(default=""),
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_month = (month or month_cycle())[:7]
    columns = {
        "product": CloudBill.product_name,
        "instance": CloudBill.instance_id,
        "billing_item": CloudBill.billing_item_name,
        "region": CloudBill.region,
        "subscription": CloudBill.subscription_type,
    }
    column = columns.get(dimension)
    if column is None:
        raise HTTPException(status_code=422, detail=f"不支持的聚合维度: {dimension}")

    query = _bill_query(db, target_month, account_id)
    rows = (
        query.with_entities(column, func.sum(CloudBill.pretax_amount))
        .group_by(column)
        .all()
    )
    total_value = sum(float(amount or 0.0) for _, amount in rows)
    items = []
    for key, amount in rows:
        items.append(
            FinanceDimensionItem(
                name=str(key or "").strip() or "未分类",
                amount=round(float(amount or 0.0), 2),
                ratio=round(float(amount or 0.0) / total_value, 4) if total_value else 0.0,
            )
        )
    items.sort(key=lambda item: item.amount, reverse=True)
    return CloudBillGroupResponse(items=items)


class BudgetConfigResponse(BaseModel):
    """预算配置读取响应。"""

    monthly_budget: float
    warn_ratio: float
    critical_ratio: float
    enabled: bool
    updated_at: str
    # 附带当月实时使用情况，前端预算卡一次拉齐。
    month: str
    spent: float
    usage_ratio: float


class BudgetConfigUpdate(BaseModel):
    """预算配置保存请求。"""

    monthly_budget: float = Field(default=0.0, ge=0.0, le=100000000.0)
    warn_ratio: float = Field(default=0.8, ge=0.1, le=2.0)
    critical_ratio: float = Field(default=1.0, ge=0.1, le=2.0)
    enabled: bool = False


@router.get("/budget", response_model=BudgetConfigResponse)
def get_budget_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    config = _read_budget_config(db)
    month = month_cycle()
    spent = round(
        float(
            db.query(func.sum(CloudBill.pretax_amount))
            .filter(CloudBill.billing_cycle == month)
            .scalar()
            or 0.0
        ),
        2,
    )
    budget = float(config.get("monthly_budget") or 0.0)
    return BudgetConfigResponse(
        monthly_budget=budget,
        warn_ratio=float(config.get("warn_ratio") or 0.8),
        critical_ratio=float(config.get("critical_ratio") or 1.0),
        enabled=bool(config.get("enabled")),
        updated_at=str(config.get("updated_at") or ""),
        month=month,
        spent=spent,
        usage_ratio=round(spent / budget, 4) if budget > 0 else 0.0,
    )


@router.put("/budget", response_model=BudgetConfigResponse)
def update_budget_config(
    payload: BudgetConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.finance.accounts.manage")),
):
    if payload.critical_ratio < payload.warn_ratio:
        raise HTTPException(status_code=422, detail="严重告警阈值不能低于警告阈值")
    if payload.enabled and payload.monthly_budget <= 0:
        raise HTTPException(status_code=422, detail="启用预算告警前必须设置大于 0 的月度预算")
    config = dict(DEFAULT_BUDGET_CONFIG)
    config.update(
        {
            "monthly_budget": round(payload.monthly_budget, 2),
            "warn_ratio": payload.warn_ratio,
            "critical_ratio": payload.critical_ratio,
            "enabled": payload.enabled,
        }
    )
    _write_budget_config(db, config)
    return get_budget_config(db=db, current_user=current_user)


class InstanceCostPoint(BaseModel):
    """实例月度费用点。"""

    month: str
    amount: float


class InstanceCostResponse(BaseModel):
    """单实例（云账单 × CMDB 资产联动）费用摘要。"""

    instance_id: str
    current_month: str
    current_month_amount: float
    previous_month_amount: float
    monthly: list[InstanceCostPoint]
    by_billing_item: list[FinanceDimensionItem]
    matched: bool  # 账单中是否存在该实例的记录


@router.get("/instance/{instance_id}/cost", response_model=InstanceCostResponse)
def get_instance_cost(
    instance_id: str,
    months: int = Query(default=6, ge=1, le=18),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按云实例 ID 查询费用（供主机详情页联动展示）。

    阿里云导入的资产直接以 ECS 实例 ID（i-xxx）作为 asset.instance_id，
    与 cloud_bills.instance_id 天然对齐，无需中间映射表。
    """
    safe_id = instance_id.strip()
    if not safe_id:
        raise HTTPException(status_code=422, detail="实例 ID 不能为空")

    current = month_cycle()
    cycle_keys: list[str] = []
    year, mon = int(current[:4]), int(current[5:7])
    for _ in range(months):
        cycle_keys.insert(0, f"{year:04d}-{mon:02d}")
        year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)

    monthly_rows = (
        db.query(CloudBill.billing_cycle, func.sum(CloudBill.pretax_amount))
        .filter(CloudBill.instance_id == safe_id, CloudBill.billing_cycle.in_(cycle_keys))
        .group_by(CloudBill.billing_cycle)
        .all()
    )
    monthly_map = {cycle: round(float(amount or 0.0), 2) for cycle, amount in monthly_rows}
    monthly = [InstanceCostPoint(month=cycle, amount=monthly_map.get(cycle, 0.0)) for cycle in cycle_keys]

    # 本月按计费项分解（Top 8），用于主机详情页的构成展示。
    item_rows = (
        db.query(CloudBill.billing_item_name, func.sum(CloudBill.pretax_amount))
        .filter(CloudBill.instance_id == safe_id, CloudBill.billing_cycle == current)
        .group_by(CloudBill.billing_item_name)
        .all()
    )
    month_total = monthly_map.get(current, 0.0)
    items = sorted(
        [(str(name or "").strip() or "未分类", round(float(amount or 0.0), 2)) for name, amount in item_rows],
        key=lambda pair: pair[1],
        reverse=True,
    )[:8]
    by_item = [
        FinanceDimensionItem(
            name=name,
            amount=amount,
            ratio=round(amount / month_total, 4) if month_total else 0.0,
        )
        for name, amount in items
    ]

    prev = cycle_keys[-2] if len(cycle_keys) >= 2 else ""
    return InstanceCostResponse(
        instance_id=safe_id,
        current_month=current,
        current_month_amount=month_total,
        previous_month_amount=monthly_map.get(prev, 0.0),
        monthly=monthly,
        by_billing_item=by_item,
        matched=bool(monthly_rows),
    )
