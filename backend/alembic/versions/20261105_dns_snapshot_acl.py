"""解绑DNS快照保留原凭证的动态授权约束。"""

import sqlalchemy as sa
from alembic import op
from app import alembic_guards as g

revision = "20261105_dns_snapshot_acl"
down_revision = "20261105_dns_providers"
branch_labels = None
depends_on = None


def upgrade():
    g.add_column_if_missing(op, "domain_records", sa.Column(
        "snapshot_credential_id", sa.String(80), nullable=True, server_default=""))


def downgrade():
    g.drop_column_if_exists(op, "domain_records", "snapshot_credential_id")
