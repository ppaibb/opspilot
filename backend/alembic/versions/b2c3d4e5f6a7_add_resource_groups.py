"""add resource_groups and asset.group_id

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23 09:45:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'resource_groups',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=True, comment="分组色值(前端标记用)"),
        sa.Column('description', sa.String(length=255), nullable=True, comment="分组说明"),
        sa.Column('sort_order', sa.Integer(), nullable=True, comment="排序，越小越靠前"),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_resource_groups_name', 'resource_groups', ['name'], unique=True)

    op.add_column('assets', sa.Column('group_id', sa.Integer(), nullable=True, comment="所属资源分组ID(resource_groups.id)，NULL 表示未分组"))
    op.create_index('ix_assets_group_id', 'assets', ['group_id'])


def downgrade() -> None:
    op.drop_index('ix_assets_group_id', table_name='assets')
    op.drop_column('assets', 'group_id')
    op.drop_index('ix_resource_groups_name', table_name='resource_groups')
    op.drop_table('resource_groups')
