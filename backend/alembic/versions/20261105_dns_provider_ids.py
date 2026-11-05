"""DNS厂商远程身份与完整记录集语义。

Revision ID: 20261105_dns_providers
Revises: 20261105_huawei_identity
"""

import sqlalchemy as sa
from alembic import op
from app import alembic_guards as g

revision = "20261105_dns_providers"
down_revision = "20261105_huawei_identity"
branch_labels = None
depends_on = None


def _columns():
    """保持旧单值列不变，完整长TXT与多值写入独立Text列。"""
    return {
        "domain_records": [sa.Column("provider_zone_id", sa.String(128), nullable=True, server_default="")],
        "domain_dns_records": [
            sa.Column("provider_record_id", sa.String(128), nullable=True, server_default=""),
            sa.Column("record_values_json", sa.Text(), nullable=True, server_default=""),
            sa.Column("proxied", sa.Boolean(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=True),
            sa.Column("read_only", sa.Boolean(), nullable=True, server_default=sa.false()),
        ],
    }


def upgrade():
    for table, columns in _columns().items():
        for column in columns:
            g.add_column_if_missing(op, table, column)


def downgrade():
    for table, columns in reversed(list(_columns().items())):
        for column in reversed(columns):
            g.drop_column_if_exists(op, table, column.name)
