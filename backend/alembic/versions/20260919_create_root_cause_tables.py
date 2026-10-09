"""create alert_root_causes table

Revision ID: 20260919_root_cause
Revises: 20260919_database
Create Date: 2026-09-19

"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = '20260919_root_cause'
down_revision = '20260919_database'
branch_labels = None
depends_on = None


def upgrade():
    g.create_table_if_missing(op, 
        'alert_root_causes',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('fingerprint', sa.String(120), server_default='', comment='告警 fingerprint'),
        sa.Column('alert_name', sa.String(200), server_default='', comment='告警规则名'),
        sa.Column('service', sa.String(200), server_default='', comment='service 标签'),
        sa.Column('instance', sa.String(200), server_default='', comment='instance 标签'),
        sa.Column('severity', sa.String(20), server_default='', comment='告警级别'),
        sa.Column('context_snapshot', sa.JSON(), comment='告警标签/注释快照'),
        sa.Column('ai_diagnosis', sa.JSON(), comment='AI 诊断'),
        sa.Column('ai_model', sa.String(100), server_default='', comment='诊断所用模型'),
        sa.Column('human_root_cause', sa.Text(), comment='人工根因'),
        sa.Column('human_resolution', sa.Text(), comment='处理方式'),
        sa.Column('ai_rating', sa.String(20), server_default='pending', comment='AI 评价'),
        sa.Column('username', sa.String(100), server_default='', comment='记录人'),
        sa.Column('created_at', sa.String(50), comment='创建时间'),
        comment='告警根因反馈闭环（三元组沉淀）',
    )
    g.create_index_if_missing(op, 'idx_arc_alert_name', 'alert_root_causes', ['alert_name'])
    g.create_index_if_missing(op, 'idx_arc_rating', 'alert_root_causes', ['ai_rating'])


def downgrade():
    g.drop_table_if_exists(op, 'alert_root_causes')
