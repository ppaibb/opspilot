"""add disks_json field to assets"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply schema changes."""
    op.add_column(
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
    op.drop_column('assets', 'disks_json')
