"""记录云资产账号与项目身份，供华为云重复导入进行归属校验。

Revision ID: 20261105_huawei_identity
Revises: 20260922_security_hardening_merge
"""

from alembic import op
import sqlalchemy as sa
from app import alembic_guards as g

revision = "20261105_huawei_identity"
down_revision = "20260922_security_hardening_merge"
branch_labels = None
depends_on = None


def upgrade():
    """历史资产保留空身份，仅对新接入资产严格校验云端归属。"""
    # 基线迁移使用当前模型 create_all，全新库可能已含这两列，必须幂等。
    g.add_column_if_missing(op, "assets", sa.Column("cloud_account_id", sa.String(64), nullable=True, server_default=""))
    g.add_column_if_missing(op, "assets", sa.Column("cloud_project_id", sa.String(64), nullable=True, server_default=""))


def downgrade():
    """回退仅删除云身份元数据，不触碰主机凭证与资产本体。"""
    g.drop_column_if_exists(op, "assets", "cloud_project_id")
    g.drop_column_if_exists(op, "assets", "cloud_account_id")
