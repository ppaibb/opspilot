"""建立当前 ORM 模型的初始 schema 基线。"""
from __future__ import annotations

from alembic import op

from app.db.database import Base
from app.db import models  # noqa: F401

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建当前版本所有已注册 ORM 表。"""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """按依赖反序删除初始 schema 中的表。"""
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        table.drop(bind=bind, checkfirst=True)
