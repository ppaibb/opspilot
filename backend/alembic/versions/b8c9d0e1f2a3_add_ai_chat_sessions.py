"""add ai chat session and message tables

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-25 23:05:00.000000

新增 AI 平台会话持久化两张表(任务2 会话保存/新增/删除):
- ai_chat_sessions: 会话(归属用户)
- ai_chat_messages: 会话内消息(user/assistant)
完全新增、可逆;降级直接 drop 两张表。
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_chat_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False, server_default='新会话'),
        sa.Column('agent_id', sa.String(length=50), nullable=False, server_default='ops'),
        sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.String(length=30), nullable=False, server_default=''),
        sa.Column('updated_at', sa.String(length=30), nullable=False, server_default=''),
    )
    op.create_index('ix_ai_chat_sessions_session_id', 'ai_chat_sessions', ['session_id'], unique=True)
    op.create_index('ix_ai_chat_sessions_user_id', 'ai_chat_sessions', ['user_id'], unique=False)

    op.create_table(
        'ai_chat_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='user'),
        sa.Column('content', sa.Text(), nullable=False, server_default=''),
        sa.Column('meta_json', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.String(length=30), nullable=False, server_default=''),
    )
    op.create_index('ix_ai_chat_messages_session_id', 'ai_chat_messages', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_ai_chat_messages_session_id', table_name='ai_chat_messages')
    op.drop_table('ai_chat_messages')
    op.drop_index('ix_ai_chat_sessions_user_id', table_name='ai_chat_sessions')
    op.drop_index('ix_ai_chat_sessions_session_id', table_name='ai_chat_sessions')
    op.drop_table('ai_chat_sessions')
