"""enhance db tables (roles / schedule / backup) and alert snapshot index

Revision ID: 20260920_db_enhance
Revises: 20260919_root_cause
Create Date: 2026-09-20

"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = '20260920_db_enhance'
down_revision = '20260919_root_cause'
branch_labels = None
depends_on = None


def upgrade():
    # 数据源：使用授权角色
    g.add_column_if_missing(op, 'db_instances', sa.Column('allowed_roles', sa.String(2000), server_default='', comment='授权权限组（JSON 数组，空=全部）'))
    # 工单：定时执行 + 目标库 + 备份快照 + 回滚语句
    g.add_column_if_missing(op, 'db_tickets', sa.Column('execute_after', sa.String(50), server_default='', comment='定时执行时间（空=立即）'))
    g.add_column_if_missing(op, 'db_tickets', sa.Column('database', sa.String(100), server_default='', comment='目标库（空=实例默认库）'))
    g.add_column_if_missing(op, 'db_tickets', sa.Column('backup_snapshot', sa.Text(), comment='受影响行备份（JSON）'))
    g.add_column_if_missing(op, 'db_tickets', sa.Column('rollback_statements', sa.Text(), comment='回滚语句（INSERT 集合）'))


def downgrade():
    g.drop_column_if_exists(op, 'db_tickets', 'rollback_statements')
    g.drop_column_if_exists(op, 'db_tickets', 'backup_snapshot')
    g.drop_column_if_exists(op, 'db_tickets', 'database')
    g.drop_column_if_exists(op, 'db_tickets', 'execute_after')
    g.drop_column_if_exists(op, 'db_instances', 'allowed_roles')
