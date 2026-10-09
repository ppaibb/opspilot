"""add resource_group_members (polymorphic membership) and migrate host group_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-23 15:50:00.000000

方案 B:新建统一多态成员表 resource_group_members,并把现有 assets.group_id
的归属关系搬入(resource_type='host', resource_id=assets.instance_id)。

assets.group_id 列保留(主机双写,兼容既有前端);本迁移只新增成员表并回填,
不动业务表结构。downgrade 把成员表里 host 类型的归属回写 assets.group_id 后删表,
可逆。
"""
from alembic import op
from app import alembic_guards as g
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.create_table_if_missing(op, 
        'resource_group_members',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('group_id', sa.Integer(), nullable=False, comment="所属分组ID(resource_groups.id)"),
        sa.Column('resource_type', sa.String(length=20), nullable=False, comment="资源类型: host/vm/cluster/domain/cert"),
        sa.Column('resource_id', sa.String(length=120), nullable=False, comment="资源业务标识"),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('resource_type', 'resource_id', name='uq_rgm_resource'),
    )
    g.create_index_if_missing(op, 'ix_resource_group_members_group_id', 'resource_group_members', ['group_id'])
    g.create_index_if_missing(op, 'ix_resource_group_members_resource_type', 'resource_group_members', ['resource_type'])
    g.create_index_if_missing(op, 'ix_resource_group_members_resource_id', 'resource_group_members', ['resource_id'])

    # 把现有主机分组关系搬入成员表
    op.execute(
        """
        INSERT INTO resource_group_members (group_id, resource_type, resource_id, created_at, updated_at)
        SELECT a.group_id, 'host', a.instance_id, NOW(), NOW()
        FROM assets a
        WHERE a.group_id IS NOT NULL
          AND a.instance_id IS NOT NULL
          AND a.instance_id <> ''
        """
    )


def downgrade() -> None:
    # 回写 host 类型归属到 assets.group_id(保证可逆)
    op.execute(
        """
        UPDATE assets a
        SET group_id = m.group_id
        FROM resource_group_members m
        WHERE m.resource_type = 'host'
          AND m.resource_id = a.instance_id
        """
    )
    op.drop_index('ix_resource_group_members_resource_id', table_name='resource_group_members')
    op.drop_index('ix_resource_group_members_resource_type', table_name='resource_group_members')
    op.drop_index('ix_resource_group_members_group_id', table_name='resource_group_members')
    g.drop_table_if_exists(op, 'resource_group_members')
