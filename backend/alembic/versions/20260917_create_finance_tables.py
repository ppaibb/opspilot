"""create finance tables (cloud_accounts / cloud_bills / bill_sync_runs)

Revision ID: 20260917_finance
Revises: 20260820_deployment_tasks
Create Date: 2026-09-17

"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260917_finance'
down_revision = '20260820_deployment_tasks'
branch_labels = None
depends_on = None


def upgrade():
    g.create_table_if_missing(op, 
        'cloud_accounts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False, comment='账号显示名'),
        sa.Column('provider', sa.String(20), server_default='aliyun', comment='云厂商: aliyun（后续扩展 tencent/aws）'),
        sa.Column('credential_ref', sa.String(50), comment='关联 credentials 表 cred_id（AK/SK 凭证）'),
        sa.Column('vendor_account_id', sa.String(64), server_default='', comment='云厂商主账号 UID（同步时自动发现）'),
        sa.Column('status', sa.String(20), server_default='pending', comment='pending/active/error'),
        sa.Column('status_message', sa.String(500), server_default='', comment='最近一次校验/同步的提示信息'),
        sa.Column('last_sync_at', sa.String(50), server_default='', comment='最近一次账单同步完成时间'),
        sa.Column('enabled', sa.Integer(), server_default='1', comment='是否参与自动同步: 1/0'),
        sa.Column('created_at', sa.String(50), comment='创建时间'),
        comment='费用管理云账号表',
    )

    g.create_table_if_missing(op, 
        'cloud_bills',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account_id', sa.Integer(), nullable=False, comment='关联 cloud_accounts.id'),
        sa.Column('billing_cycle', sa.String(7), nullable=False, comment='账期: 2026-09'),
        sa.Column('billing_date', sa.String(10), nullable=False, comment='出账日期: 2026-09-15（月结为账期首日）'),
        sa.Column('granularity', sa.String(10), server_default='DAILY', comment='DAILY/MONTHLY'),
        sa.Column('provider', sa.String(20), server_default='aliyun', comment='云厂商'),
        sa.Column('region', sa.String(50), server_default='', comment='地域'),
        sa.Column('zone', sa.String(50), server_default='', comment='可用区'),
        sa.Column('product_code', sa.String(80), server_default='', comment='产品编码: ecs/oss/slb'),
        sa.Column('product_name', sa.String(200), server_default='', comment='产品名称'),
        sa.Column('product_detail', sa.String(200), server_default='', comment='产品明细/规格'),
        sa.Column('subscription_type', sa.String(20), server_default='', comment='Subscription=预付费 / PayAsYouGo=后付费'),
        sa.Column('billing_item_code', sa.String(200), server_default='', comment='计费项编码'),
        sa.Column('billing_item_name', sa.String(200), server_default='', comment='计费项名称'),
        sa.Column('instance_id', sa.String(120), server_default='', comment='实例/资源 ID'),
        sa.Column('resource_name', sa.String(200), server_default='', comment='资源名称'),
        sa.Column('resource_group', sa.String(120), server_default='', comment='资源组'),
        sa.Column('cost_unit', sa.String(120), server_default='', comment='财务单元'),
        sa.Column('tags', sa.Text(), comment='标签（JSON 数组字符串）'),
        sa.Column('pretax_amount', sa.Float(), server_default='0', comment='费用金额（优惠后）'),
        sa.Column('payment_amount', sa.Float(), server_default='0', comment='现金支付金额'),
        sa.Column('cash_amount', sa.Float(), server_default='0', comment='代金券抵扣'),
        sa.Column('deducted_by_coupons', sa.Float(), server_default='0', comment='优惠券抵扣'),
        sa.Column('credit_amount', sa.Float(), server_default='0', comment='信控额度抵扣'),
        sa.Column('currency', sa.String(10), server_default='CNY', comment='币种'),
        sa.Column('dedup_key', sa.String(120), nullable=False, comment='行幂等键（实例+计费项+序号哈希）'),
        sa.UniqueConstraint('account_id', 'billing_date', 'dedup_key', name='uq_cloud_bill_dedup'),
        comment='云账单明细表',
    )
    g.create_index_if_missing(op, 'idx_cloud_bills_cycle', 'cloud_bills', ['account_id', 'billing_cycle'])
    g.create_index_if_missing(op, 'idx_cloud_bills_product', 'cloud_bills', ['billing_cycle', 'product_code'])
    g.create_index_if_missing(op, 'idx_cloud_bills_date', 'cloud_bills', ['billing_date'])

    g.create_table_if_missing(op, 
        'bill_sync_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account_id', sa.Integer(), nullable=False, comment='关联 cloud_accounts.id'),
        sa.Column('billing_cycle', sa.String(7), nullable=False, comment='同步的账期'),
        sa.Column('status', sa.String(20), server_default='running', comment='running/success/failed'),
        sa.Column('trigger', sa.String(20), server_default='manual', comment='manual/scheduled'),
        sa.Column('rows_synced', sa.Integer(), server_default='0', comment='本次落库行数'),
        sa.Column('error_message', sa.Text(), comment='失败原因'),
        sa.Column('started_at', sa.String(50), comment='开始时间'),
        sa.Column('finished_at', sa.String(50), server_default='', comment='结束时间'),
        comment='账单同步执行记录表',
    )
    g.create_index_if_missing(op, 'idx_bill_sync_runs_account', 'bill_sync_runs', ['account_id'])


def downgrade():
    g.drop_table_if_exists(op, 'bill_sync_runs')
    g.drop_table_if_exists(op, 'cloud_bills')
    g.drop_table_if_exists(op, 'cloud_accounts')
