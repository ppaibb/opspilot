# Alembic 迁移幂等守卫。
#
# 背景：0001_initial_schema 不是静态快照，而是按"当前模型" create_all 建全量表。
# 因此空库从 base 全量 upgrade 时，后续迁移里对同一张表/列/索引的 DDL 全部是重复操作
# （曾撞 DuplicateColumn: assets.hostname）；存量库（结构同样由 create_all 维护）
# stamp 后跑尾部迁移同理。这里把常用 DDL 包成"存在即跳过"的幂等版本，
# 迁移文件统一 `from app import alembic_guards as g` 后调用。
# 放在 app/ 包下而不是 alembic/ 目录，避免目录名与已安装的 alembic 包冲突。
from __future__ import annotations

import sqlalchemy as sa


def _existing_columns(op, table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        return {c["name"] for c in inspector.get_columns(table_name)}
    except Exception:
        # 表不存在等场景：返回空集，让"加列"类操作继续执行并抛出真实错误
        return set()


def add_column_if_missing(op, table_name: str, column: sa.Column) -> None:
    """列已存在时跳过（op.add_column 的幂等版）。"""
    if column.name not in _existing_columns(op, table_name):
        op.add_column(table_name, column)


def drop_column_if_exists(op, table_name: str, column_name: str) -> None:
    """列不存在时跳过（op.drop_column 的幂等版）。"""
    if column_name in _existing_columns(op, table_name):
        op.drop_column(table_name, column_name)


def create_table_if_missing(op, table_name: str, *columns, **kwargs) -> None:
    """表已存在时跳过（op.create_table 的幂等版）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        op.create_table(table_name, *columns, **kwargs)


def drop_table_if_exists(op, table_name: str, **kwargs) -> None:
    """表不存在时跳过（op.drop_table 的幂等版）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name in inspector.get_table_names():
        op.drop_table(table_name, **kwargs)


def create_index_if_missing(op, index_name: str, table_name: str, *columns, **kwargs) -> None:
    """索引已存在时跳过（op.create_index 的幂等版）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing = {i["name"] for i in inspector.get_indexes(table_name)}
    except Exception:
        existing = set()
    if index_name not in existing:
        op.create_index(index_name, table_name, *columns, **kwargs)


def drop_index_if_exists(op, index_name: str, table_name: str, **kwargs) -> None:
    """索引不存在时跳过（op.drop_index 的幂等版）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing = {i["name"] for i in inspector.get_indexes(table_name)}
    except Exception:
        existing = set()
    if index_name in existing:
        op.drop_index(index_name, table_name=table_name, **kwargs)
