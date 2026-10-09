"""create monitoring_notification_logs table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-24 11:30:00.000000

新增通知发送记录(审计)表:每次通过通知渠道推送告警(成功/失败)写一行,
用于「通知发送记录/审计」页面。可逆(downgrade 删表)。
"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.create_table_if_missing(op, 
        'monitoring_notification_logs',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('fingerprint', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('alert_name', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('instance', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('severity', sa.String(length=20), nullable=False, server_default='warning'),
        sa.Column('event_type', sa.String(length=20), nullable=False, server_default='firing'),
        sa.Column('channel_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('channel_name', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('channel_type', sa.String(length=30), nullable=False, server_default=''),
        sa.Column('target', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('message', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    g.create_index_if_missing(op, 'ix_monitoring_notification_logs_fingerprint', 'monitoring_notification_logs', ['fingerprint'])
    g.create_index_if_missing(op, 'ix_monitoring_notification_logs_alert_name', 'monitoring_notification_logs', ['alert_name'])
    g.create_index_if_missing(op, 'ix_monitoring_notification_logs_channel_name', 'monitoring_notification_logs', ['channel_name'])
    g.create_index_if_missing(op, 'ix_monitoring_notification_logs_success', 'monitoring_notification_logs', ['success'])
    g.create_index_if_missing(op, 'ix_monitoring_notification_logs_created_at', 'monitoring_notification_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_monitoring_notification_logs_created_at', table_name='monitoring_notification_logs')
    op.drop_index('ix_monitoring_notification_logs_success', table_name='monitoring_notification_logs')
    op.drop_index('ix_monitoring_notification_logs_channel_name', table_name='monitoring_notification_logs')
    op.drop_index('ix_monitoring_notification_logs_alert_name', table_name='monitoring_notification_logs')
    op.drop_index('ix_monitoring_notification_logs_fingerprint', table_name='monitoring_notification_logs')
    g.drop_table_if_exists(op, 'monitoring_notification_logs')
