"""user_im_bindings 表：用户 IM 渠道绑定（第一版飞书，群内 @ 通知）。"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import alembic_guards as g

revision = "20260921_im_bindings"
down_revision = "20260920_db_enhance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.create_table_if_missing(op, 
        "user_im_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False, server_default="feishu"),
        sa.Column("identity", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("open_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("notify_mode", sa.String(length=20), nullable=False, server_default="group_at"),
        sa.Column("webhook_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("bound_at", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(length=30), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "platform", name="uq_user_im_binding"),
    )
    g.create_index_if_missing(op, "ix_user_im_bindings_id", "user_im_bindings", ["id"])
    g.create_index_if_missing(op, "ix_user_im_bindings_user_id", "user_im_bindings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_im_bindings_user_id", table_name="user_im_bindings")
    op.drop_index("ix_user_im_bindings_id", table_name="user_im_bindings")
    g.drop_table_if_exists(op, "user_im_bindings")
