"""certificates 表加 last_renew_attempt_at：自动续签失败重试冷却。

背景：到期证书若自动续签一直失败（DNS 未托管/凭证失效等），过去每轮轮询
+ 每次进程重启都会重新触发并重复发通知；该列记录最近一次触发时间，
冷却期内跳过，避免通知轰炸与 ACME 限流。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import alembic_guards as g

revision = "20260921_cert_renew_cooldown"
down_revision = "20260921_im_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.add_column_if_missing(op, 
        "certificates",
        sa.Column("last_renew_attempt_at", sa.String(length=30), nullable=False, server_default=""),
    )


def downgrade() -> None:
    g.drop_column_if_exists(op, "certificates", "last_renew_attempt_at")
