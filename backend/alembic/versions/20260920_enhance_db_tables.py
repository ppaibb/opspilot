"""enhance db tables (roles / schedule / backup) and alert snapshot index

Revision ID: 20260920_db_enhance
Revises: 20260919_root_cause
Create Date: 2026-09-20

"""
from alembic import op
import sqlalchemy as sa

revision = '20260920_db_enhance'
down_revision = '20260919_root_cause'
branch_labels = None
depends_on = None


def upgrade():
    # 数据源：使用授权角色
    op.add_column('db_instances', sa.Column('allowed_roles', sa.String(2000), server_default='', comment='授权权限组（JSON 数组，空=全部）'))
    # 工单：定时执行 + 目标库 + 备份快照 + 回滚语句
    op.add_column('db_tickets', sa.Column('execute_after', sa.String(50), server_default='', comment='定时执行时间（空=立即）'))
    op.add_column('db_tickets', sa.Column('database', sa.String(100), server_default='', comment='目标库（空=实例默认库）'))
    op.add_column('db_tickets', sa.Column('backup_snapshot', sa.Text(), comment='受影响行备份（JSON）'))
    op.add_column('db_tickets', sa.Column('rollback_statements', sa.Text(), comment='回滚语句（INSERT 集合）'))


def downgrade():
    op.drop_column('db_tickets', 'rollback_statements')
    op.drop_column('db_tickets', 'backup_snapshot')
    op.drop_column('db_tickets', 'database')
    op.drop_column('db_tickets', 'execute_after')
    op.drop_column('db_instances', 'allowed_roles')
