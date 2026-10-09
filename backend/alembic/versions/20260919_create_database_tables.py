"""create database management tables (db_instances / db_tickets / db_query_logs)

Revision ID: 20260919_database
Revises: 20260919_wireguard
Create Date: 2026-09-19

"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = '20260919_database'
down_revision = '20260919_wireguard'
branch_labels = None
depends_on = None


def upgrade():
    g.create_table_if_missing(op, 
        'db_instances',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False, comment='实例名称'),
        sa.Column('db_type', sa.String(20), server_default='mysql', comment='mysql/postgresql'),
        sa.Column('host', sa.String(200), nullable=False, comment='主机地址'),
        sa.Column('port', sa.Integer(), server_default='3306', comment='端口'),
        sa.Column('database', sa.String(100), server_default='', comment='默认库名'),
        sa.Column('username', sa.String(100), nullable=False, comment='连接用户'),
        sa.Column('password', sa.Text(), comment='连接密码（加密）'),
        sa.Column('remark', sa.String(500), server_default='', comment='备注'),
        sa.Column('status', sa.String(20), server_default='unknown', comment='连通状态'),
        sa.Column('status_message', sa.String(500), server_default='', comment='最近连通信息'),
        sa.Column('created_at', sa.String(50), comment='创建时间'),
        comment='数据库实例（数据源）',
    )

    g.create_table_if_missing(op, 
        'db_tickets',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('instance_id', sa.Integer(), nullable=False, comment='目标实例'),
        sa.Column('title', sa.String(200), server_default='', comment='工单标题'),
        sa.Column('sql_text', sa.Text(), nullable=False, comment='SQL 全文'),
        sa.Column('ticket_type', sa.String(20), server_default='change', comment='change/query'),
        sa.Column('status', sa.String(20), server_default='pending', comment='工单状态机'),
        sa.Column('audit_result', sa.JSON(), comment='规则引擎审核明细'),
        sa.Column('audit_passed', sa.Integer(), server_default='1', comment='审核通过: 1/0'),
        sa.Column('exec_result', sa.JSON(), comment='逐条执行结果'),
        sa.Column('submitter', sa.String(100), server_default='', comment='提交人'),
        sa.Column('reviewer', sa.String(100), server_default='', comment='审批人'),
        sa.Column('executor', sa.String(100), server_default='', comment='执行人'),
        sa.Column('review_note', sa.String(500), server_default='', comment='审批意见'),
        sa.Column('created_at', sa.String(50), comment='提交时间'),
        sa.Column('executed_at', sa.String(50), server_default='', comment='执行完成时间'),
        comment='SQL 工单',
    )
    g.create_index_if_missing(op, 'idx_db_tickets_status', 'db_tickets', ['status'])
    g.create_index_if_missing(op, 'idx_db_tickets_instance', 'db_tickets', ['instance_id'])

    g.create_table_if_missing(op, 
        'db_query_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('instance_id', sa.Integer(), nullable=False, comment='实例'),
        sa.Column('username', sa.String(100), server_default='', comment='查询人'),
        sa.Column('sql_text', sa.Text(), comment='查询语句'),
        sa.Column('row_count', sa.Integer(), server_default='0', comment='返回行数'),
        sa.Column('duration_ms', sa.Integer(), server_default='0', comment='耗时'),
        sa.Column('ok', sa.Integer(), server_default='1', comment='成功: 1/0'),
        sa.Column('error', sa.String(500), server_default='', comment='失败原因'),
        sa.Column('created_at', sa.String(50), comment='查询时间'),
        comment='自助查询审计日志',
    )
    g.create_index_if_missing(op, 'idx_db_query_logs_instance', 'db_query_logs', ['instance_id'])


def downgrade():
    g.drop_table_if_exists(op, 'db_query_logs')
    g.drop_table_if_exists(op, 'db_tickets')
    g.drop_table_if_exists(op, 'db_instances')
