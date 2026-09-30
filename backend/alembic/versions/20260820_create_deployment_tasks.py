"""create deployment tasks table

Revision ID: 20260820_deployment_tasks
Revises: f6a7b8c9d0e1
Create Date: 2026-08-20

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20260820_deployment_tasks'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'application_deployment_tasks',
        sa.Column('task_id', sa.String(36), primary_key=True, comment='任务 ID（UUID）'),
        sa.Column('app_id', sa.String(36), sa.ForeignKey('applications.app_id', ondelete='CASCADE'), nullable=False, index=True, comment='关联应用 ID'),
        sa.Column('task_type', sa.String(20), nullable=False, comment='任务类型：deploy/restart/stop/start'),
        sa.Column('status', sa.String(20), nullable=False, default='pending', index=True, comment='任务状态：pending/running/success/failed/cancelled'),
        sa.Column('progress', sa.Integer, default=0, comment='进度百分比 0-100'),
        sa.Column('current_step', sa.String(100), comment='当前步骤描述'),
        sa.Column('total_steps', sa.Integer, comment='总步骤数'),
        sa.Column('result_message', sa.Text, comment='结果消息或错误信息'),
        sa.Column('output_log', sa.Text, comment='执行日志输出'),
        sa.Column('deployment_config', postgresql.JSONB, comment='部署配置（JSON）'),
        sa.Column('created_by', sa.String(50), comment='创建人'),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False, comment='创建时间'),
        sa.Column('started_at', sa.DateTime, comment='开始执行时间'),
        sa.Column('finished_at', sa.DateTime, comment='完成时间'),
        comment='应用部署任务表（异步任务）'
    )
    
    # 创建索引
    op.create_index('idx_deployment_tasks_status', 'application_deployment_tasks', ['status'])
    op.create_index('idx_deployment_tasks_app_id', 'application_deployment_tasks', ['app_id'])
    op.create_index('idx_deployment_tasks_created_at', 'application_deployment_tasks', ['created_at'])


def downgrade():
    op.drop_table('application_deployment_tasks')
