"""add monitoring shadow alerts

Revision ID: a1b2c3d4e5f6
Revises: 9fbe4a2c470f
Create Date: 2026-06-22 15:52:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = '9fbe4a2c470f'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'monitoring_shadow_alerts',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('rule_id', sa.String(length=64), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('instance', sa.String(length=100), nullable=True),
        sa.Column('severity', sa.String(length=20), nullable=True),
        sa.Column('state', sa.String(length=20), nullable=True, comment="pending / firing / resolved"),
        sa.Column('value', sa.String(length=64), nullable=True, comment="最近一次评估的样本值"),
        sa.Column('labels', sa.Text(), nullable=True, comment="完整 labels 的 JSON 字符串"),
        sa.Column('first_breach_at', sa.DateTime(), nullable=True),
        sa.Column('active_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True)
    )
    op.create_index('ix_monitoring_shadow_alerts_fingerprint', 'monitoring_shadow_alerts', ['fingerprint'], unique=True)
    op.create_index('ix_monitoring_shadow_alerts_rule_id', 'monitoring_shadow_alerts', ['rule_id'])
    op.create_index('ix_monitoring_shadow_alerts_instance', 'monitoring_shadow_alerts', ['instance'])

def downgrade() -> None:
    op.drop_index('ix_monitoring_shadow_alerts_instance', table_name='monitoring_shadow_alerts')
    op.drop_index('ix_monitoring_shadow_alerts_rule_id', table_name='monitoring_shadow_alerts')
    op.drop_index('ix_monitoring_shadow_alerts_fingerprint', table_name='monitoring_shadow_alerts')
    op.drop_table('monitoring_shadow_alerts')
