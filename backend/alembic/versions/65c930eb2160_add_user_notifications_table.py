"""add user_notifications table"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '65c930eb2160'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create user_notifications table for in-app user notifications."""
    op.create_table(
        'user_notifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False, comment='接收用户 ID'),
        sa.Column('type', sa.String(length=20), nullable=False, comment='通知类型: success / error / warning / info'),
        sa.Column('category', sa.String(length=50), nullable=False, comment='业务分类: certificate / vm / agent / system 等'),
        sa.Column('title', sa.String(length=200), nullable=False, comment='标题'),
        sa.Column('content', sa.Text(), nullable=False, comment='详情'),
        sa.Column('link', sa.String(length=500), nullable=True, comment='点击跳转的前端路由'),
        sa.Column('resource_id', sa.String(length=100), nullable=True, comment='关联资源 ID'),
        sa.Column('is_read', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_notifications_created_at'), 'user_notifications', ['created_at'], unique=False)
    op.create_index(op.f('ix_user_notifications_id'), 'user_notifications', ['id'], unique=False)
    op.create_index(op.f('ix_user_notifications_is_read'), 'user_notifications', ['is_read'], unique=False)
    op.create_index(op.f('ix_user_notifications_user_id'), 'user_notifications', ['user_id'], unique=False)


def downgrade() -> None:
    """Drop user_notifications table."""
    op.drop_index(op.f('ix_user_notifications_user_id'), table_name='user_notifications')
    op.drop_index(op.f('ix_user_notifications_is_read'), table_name='user_notifications')
    op.drop_index(op.f('ix_user_notifications_id'), table_name='user_notifications')
    op.drop_index(op.f('ix_user_notifications_created_at'), table_name='user_notifications')
    op.drop_table('user_notifications')
