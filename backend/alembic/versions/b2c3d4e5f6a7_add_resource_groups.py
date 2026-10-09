"""add resource_groups and asset.group_id

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23 09:45:00.000000

"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.create_table_if_missing(op, 
        'resource_groups',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=True, comment="分组色值(前端标记用)"),
        sa.Column('description', sa.String(length=255), nullable=True, comment="分组说明"),
        sa.Column('sort_order', sa.Integer(), nullable=True, comment="排序，越小越靠前"),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    g.create_index_if_missing(op, 'ix_resource_groups_name', 'resource_groups', ['name'], unique=True)

    g.add_column_if_missing(op, 'assets', sa.Column('group_id', sa.Integer(), nullable=True, comment="所属资源分组ID(resource_groups.id)，NULL 表示未分组"))
    g.create_index_if_missing(op, 'ix_assets_group_id', 'assets', ['group_id'])


def downgrade() -> None:
    op.drop_index('ix_assets_group_id', table_name='assets')
    g.drop_column_if_exists(op, 'assets', 'group_id')
    op.drop_index('ix_resource_groups_name', table_name='resource_groups')
    g.drop_table_if_exists(op, 'resource_groups')
