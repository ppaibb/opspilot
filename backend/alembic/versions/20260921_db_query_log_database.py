"""db_query_logs.database：记录自助查询实际连接的目标库。

查询面板可以临时切库，而实例本身有默认库。缺了这一列，"表不存在"这类错误
在审计日志里看不出是 SQL 写错了还是连错了库，只能靠复现倒推。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import alembic_guards as g

revision = "20260921_db_query_log_database"
down_revision = "20260921_cert_renew_cooldown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.add_column_if_missing(op, 
        "db_query_logs",
        sa.Column("database", sa.String(length=100), nullable=False, server_default=""),
    )


def downgrade() -> None:
    g.drop_column_if_exists(op, "db_query_logs", "database")
