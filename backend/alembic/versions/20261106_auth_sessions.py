"""增加 refresh token 轮换与登录限流持久化表。

Revision ID: 20261106_auth_sessions
Revises: 20261106_risk_ticket_v2
"""

import sqlalchemy as sa
from alembic import op

from app import alembic_guards as g

revision = "20261106_auth_sessions"
down_revision = "20261106_risk_ticket_v2"
branch_labels = None
depends_on = None


def upgrade():
    """创建认证会话与限流表；只保存 refresh token 摘要。"""
    g.create_table_if_missing(
        op,
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("issued_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("used_at", sa.String(40), nullable=True),
        sa.Column("revoked_at", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    g.create_index_if_missing(op, "ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    g.create_index_if_missing(op, "ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    g.create_index_if_missing(op, "ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    g.create_index_if_missing(op, "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    g.create_table_if_missing(
        op,
        "login_attempts",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False, server_default=""),
        sa.Column("source_ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.String(40), nullable=False),
        sa.Column("locked_until", sa.String(40), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )


def downgrade():
    """移除认证会话表；降级会使所有 refresh 会话失效。"""
    g.drop_table_if_exists(op, "login_attempts")
    g.drop_table_if_exists(op, "refresh_tokens")
