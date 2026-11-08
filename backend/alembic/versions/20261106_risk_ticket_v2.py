"""风险票据绑定 HTTP 方法、路由、资源和请求摘要。

Revision ID: 20261106_risk_ticket_v2
Revises: 20261105_dns_snapshot_acl
"""

import sqlalchemy as sa
from alembic import op

from app import alembic_guards as g

revision = "20261106_risk_ticket_v2"
down_revision = "20261105_dns_snapshot_acl"
branch_labels = None
depends_on = None


def upgrade():
    """增加可滚动升级的绑定列，并作废无法精确归属的旧短键票据。"""
    g.add_column_if_missing(op, "risk_ack_tickets", sa.Column("http_method", sa.String(12), nullable=True))
    g.add_column_if_missing(op, "risk_ack_tickets", sa.Column("route_template", sa.String(255), nullable=True))
    g.add_column_if_missing(op, "risk_ack_tickets", sa.Column("request_digest", sa.String(80), nullable=True))
    # 使用 SQLAlchemy 表达式生成各数据库方言的布尔字面量，兼容 PostgreSQL/MySQL。
    tickets = sa.table(
        "risk_ack_tickets",
        sa.column("short_key", sa.Boolean()),
        sa.column("consumed_at", sa.String(30)),
    )
    op.execute(
        tickets.delete().where(
            tickets.c.short_key.is_(True),
            tickets.c.consumed_at.is_(None),
        )
    )


def downgrade():
    """仅移除新增元数据列，不恢复已作废的泛化短键票据。"""
    g.drop_column_if_exists(op, "risk_ack_tickets", "request_digest")
    g.drop_column_if_exists(op, "risk_ack_tickets", "route_template")
    g.drop_column_if_exists(op, "risk_ack_tickets", "http_method")
