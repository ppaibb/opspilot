"""add disks_json field to assets"""
from __future__ import annotations

from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply schema changes."""
    g.add_column_if_missing(op, 
        'assets',
        sa.Column(
            'disks_json',
            sa.String(length=2000),
            nullable=True,
            server_default='[]',
            comment='磁盘明细JSON（系统盘+数据盘），仅保存盘名和容量',
        ),
    )


def downgrade() -> None:
    """Rollback schema changes."""
    g.drop_column_if_exists(op, 'assets', 'disks_json')
