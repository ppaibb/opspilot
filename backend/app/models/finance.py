"""
费用管理表（第一版：阿里云 BSS 账单）
"""
from sqlalchemy import Column, Integer, String, Float, Text, Index, UniqueConstraint

from app.db.database import Base


class CloudAccount(Base):
    """云账号：费用管理纳管的云厂商账号，AK/SK 引用凭据保险箱。"""
    __tablename__ = "cloud_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="账号显示名")
    provider = Column(String(20), default="aliyun", comment="云厂商: aliyun（后续扩展 tencent/aws）")
    credential_ref = Column(String(50), comment="关联 credentials 表 cred_id（AK/SK 凭证）")
    vendor_account_id = Column(String(64), default="", comment="云厂商主账号 UID（同步时自动发现）")
    status = Column(String(20), default="pending", comment="pending/active/error")
    status_message = Column(String(500), default="", comment="最近一次校验/同步的提示信息")
    last_sync_at = Column(String(50), default="", comment="最近一次账单同步完成时间")
    enabled = Column(Integer, default=1, comment="是否参与自动同步: 1/0")
    created_at = Column(String(50), comment="创建时间")


class CloudBill(Base):
    """云账单明细（阿里云 DescribeInstanceBill 按日粒度落库）。"""
    __tablename__ = "cloud_bills"
    __table_args__ = (
        UniqueConstraint("account_id", "billing_date", "dedup_key", name="uq_cloud_bill_dedup"),
        Index("idx_cloud_bills_cycle", "account_id", "billing_cycle"),
        Index("idx_cloud_bills_product", "billing_cycle", "product_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, comment="关联 cloud_accounts.id")
    billing_cycle = Column(String(7), nullable=False, comment="账期: 2026-09")
    billing_date = Column(String(10), nullable=False, comment="出账日期: 2026-09-15（月结为账期首日）")
    granularity = Column(String(10), default="DAILY", comment="DAILY/MONTHLY")
    provider = Column(String(20), default="aliyun", comment="云厂商")
    region = Column(String(50), default="", comment="地域")
    zone = Column(String(50), default="", comment="可用区")
    product_code = Column(String(80), default="", comment="产品编码: ecs/oss/slb")
    product_name = Column(String(200), default="", comment="产品名称")
    product_detail = Column(String(200), default="", comment="产品明细/规格")
    subscription_type = Column(String(20), default="", comment="Subscription=预付费 / PayAsYouGo=后付费")
    billing_item_code = Column(String(200), default="", comment="计费项编码")
    billing_item_name = Column(String(200), default="", comment="计费项名称")
    instance_id = Column(String(120), default="", comment="实例/资源 ID")
    resource_name = Column(String(200), default="", comment="资源名称")
    resource_group = Column(String(120), default="", comment="资源组")
    cost_unit = Column(String(120), default="", comment="财务单元")
    tags = Column(Text, default="", comment="标签（JSON 数组字符串）")
    pretax_amount = Column(Float, default=0.0, comment="费用金额（优惠后）")
    payment_amount = Column(Float, default=0.0, comment="现金支付金额")
    cash_amount = Column(Float, default=0.0, comment="代金券抵扣")
    deducted_by_coupons = Column(Float, default=0.0, comment="优惠券抵扣")
    credit_amount = Column(Float, default=0.0, comment="信控额度抵扣")
    currency = Column(String(10), default="CNY", comment="币种")
    dedup_key = Column(String(120), nullable=False, comment="行幂等键（实例+计费项+序号哈希）")


class BillSyncRun(Base):
    """账单同步执行记录。"""
    __tablename__ = "bill_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True, comment="关联 cloud_accounts.id")
    billing_cycle = Column(String(7), nullable=False, comment="同步的账期")
    status = Column(String(20), default="running", comment="running/success/failed/skipped（skipped=租约被其他实例持有，本次未执行）")
    trigger = Column(String(20), default="manual", comment="manual/scheduled")
    rows_synced = Column(Integer, default=0, comment="本次落库行数")
    error_message = Column(Text, default="", comment="失败原因")
    started_at = Column(String(50), comment="开始时间")
    finished_at = Column(String(50), default="", comment="结束时间")
