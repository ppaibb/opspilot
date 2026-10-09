"""add group column to monitoring_history_alerts

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-23 23:25:00.000000

给历史告警表新增 group 列(来自告警规则 group 标签快照),便于历史告警按分组筛选。
存量行 group 为空字符串(无法追溯历史分组),新归档行写入当时的 group。可逆。
"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.add_column_if_missing(op, 
        'monitoring_history_alerts',
        sa.Column('group', sa.String(length=60), nullable=False, server_default=''),
    )


def downgrade() -> None:
    g.drop_column_if_exists(op, 'monitoring_history_alerts', 'group')
