"""安全加固合并迁移：收敛双 head + 风险票据/主机指纹/任务租约三表 + 用户会话版本。

包含两部分：
1. 合并历史上两条迁移分支（20260921_db_query_log_database 与 65c930eb2160），
   使版本图回到单 head；
2. 本轮安全加固的库结构：
   - risk_ack_tickets：高危操作一次性确认票据；
   - asset_host_keys：SSH 主机指纹钉扎（TOFU）；
   - task_leases：后台任务数据库租约（防多 worker 重复执行）；
   - users.token_version：会话版本号（重置密码/禁用账号时递增使旧 JWT 失效）。

注意：开发库由启动时 create_all + ensure_model_columns 维护，本迁移面向
生产环境（DB_AUTO_CREATE_TABLES=0）的 alembic upgrade head。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260922_security_hardening_merge"
down_revision = ("20260921_db_query_log_database", "65c930eb2160")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 高危操作确认票据 ──
    op.create_table(
        "risk_ack_tickets",
        sa.Column("id", sa.String(length=36), nullable=False, comment="票据 UUID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="签发给哪位用户"),
        sa.Column("permission_key", sa.String(length=150), nullable=False, comment="权限键（或短键标记）"),
        sa.Column("action_id", sa.String(length=64), nullable=False, comment="动作标识"),
        sa.Column("resource_id", sa.String(length=128), nullable=True, comment="绑定的资源 ID（可空）"),
        sa.Column("short_key", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="是否为前端短键票据"),
        sa.Column("created_at", sa.String(length=30), nullable=False, comment="签发时间"),
        sa.Column("expires_at", sa.String(length=30), nullable=False, comment="过期时间"),
        sa.Column("consumed_at", sa.String(length=30), nullable=True, comment="消费时间（一次性）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_ack_tickets_user_id", "risk_ack_tickets", ["user_id"])

    # ── SSH 主机指纹钉扎 ──
    op.create_table(
        "asset_host_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False, comment="主机地址"),
        sa.Column("port", sa.Integer(), nullable=False, comment="SSH 端口"),
        sa.Column("key_type", sa.String(length=32), nullable=False, comment="主机密钥类型"),
        sa.Column("fingerprint", sa.String(length=128), nullable=False, comment="指纹（SHA256 风格）"),
        sa.Column("first_seen_at", sa.String(length=30), nullable=False, comment="首次记录时间"),
        sa.Column("last_seen_at", sa.String(length=30), nullable=False, comment="最近校验时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host", "port", "key_type", name="uq_asset_host_keys_identity"),
    )

    # ── 后台任务租约 ──
    op.create_table(
        "task_leases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lease_key", sa.String(length=120), nullable=False, comment="任务租约键"),
        sa.Column("owner", sa.String(length=120), nullable=False, comment="持有者实例标识"),
        sa.Column("acquired_at", sa.String(length=30), nullable=False, comment="获取时间"),
        sa.Column("heartbeat_at", sa.String(length=30), nullable=False, comment="最近心跳时间"),
        sa.Column("expires_at", sa.String(length=30), nullable=False, comment="过期时间"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_leases_lease_key", "task_leases", ["lease_key"], unique=True)

    # ── 用户会话版本 ──
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="会话版本号，重置密码/禁用时递增使旧 JWT 失效"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
    op.drop_index("ix_task_leases_lease_key", table_name="task_leases")
    op.drop_table("task_leases")
    op.drop_table("asset_host_keys")
    op.drop_index("ix_risk_ack_tickets_user_id", table_name="risk_ack_tickets")
    op.drop_table("risk_ack_tickets")
