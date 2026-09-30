"""费用管理接口的请求与响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CloudAccountCreate(BaseModel):
    """新增/编辑云账号。"""

    name: str
    provider: str = "aliyun"
    credential_ref: str = ""
    enabled: bool = True


class CloudAccountResponse(BaseModel):
    """云账号列表项（不回传任何密钥内容）。"""

    id: int
    name: str
    provider: str
    credential_ref: str
    credential_name: str = ""
    vendor_account_id: str = ""
    status: str
    status_message: str = ""
    last_sync_at: str = ""
    enabled: bool = True
    created_at: str = ""


class BillSyncRequest(BaseModel):
    """手动触发同步。"""

    billing_cycle: str = ""  # 空 = 当月；格式 2026-09
    include_previous: bool = True  # 是否连带同步上个月（数据延迟补偿）


class BillSyncRunResponse(BaseModel):
    """同步执行记录。"""

    id: int
    account_id: int
    billing_cycle: str
    status: str
    trigger: str
    rows_synced: int
    error_message: str = ""
    started_at: str = ""
    finished_at: str = ""


class FinanceDimensionItem(BaseModel):
    """单维度聚合项（产品/账号/计费项等）。"""

    name: str
    amount: float
    ratio: float = 0.0


class FinanceTrendPoint(BaseModel):
    """趋势图单点。"""

    period: str
    amount: float


class FinanceOverviewResponse(BaseModel):
    """费用总览。"""

    month: str
    total: float
    prev_total: float
    mom_delta: float = 0.0
    by_account: list[FinanceDimensionItem] = Field(default_factory=list)
    by_product: list[FinanceDimensionItem] = Field(default_factory=list)
    by_subscription: list[FinanceDimensionItem] = Field(default_factory=list)
    daily_trend: list[FinanceTrendPoint] = Field(default_factory=list)
    monthly_trend: list[FinanceTrendPoint] = Field(default_factory=list)
    last_synced_at: str = ""


class CloudBillItem(BaseModel):
    """账单明细行。"""

    id: int
    account_id: int
    account_name: str = ""
    billing_cycle: str
    billing_date: str
    provider: str = "aliyun"
    region: str = ""
    product_code: str = ""
    product_name: str = ""
    product_detail: str = ""
    subscription_type: str = ""
    billing_item_code: str = ""
    billing_item_name: str = ""
    instance_id: str = ""
    resource_name: str = ""
    resource_group: str = ""
    cost_unit: str = ""
    pretax_amount: float = 0.0
    payment_amount: float = 0.0
    cash_amount: float = 0.0
    deducted_by_coupons: float = 0.0
    currency: str = "CNY"


class CloudBillListResponse(BaseModel):
    """账单明细分页响应。"""

    items: list[CloudBillItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    grand_total: float = 0.0


class CloudBillGroupResponse(BaseModel):
    """账单按维度聚合响应。"""

    items: list[FinanceDimensionItem] = Field(default_factory=list)


class CredentialOptionResponse(BaseModel):
    """可绑定的 AK/SK 凭证下拉项。"""

    cred_id: str
    name: str
    type: str
