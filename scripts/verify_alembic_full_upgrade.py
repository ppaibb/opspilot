"""验证 Alembic 的空库、存量库和重复升级契约。"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
LEGACY_FIXTURE = BACKEND_DIR / "tests" / "schema" / "legacy_schema.sql"
DATABASES = {
    "empty": "opspilot_mig_check_empty",
    "legacy": "opspilot_mig_check_legacy",
}


def _admin_engine() -> tuple[str, sa.Engine]:
    """从应用配置推导同一 PostgreSQL 实例的管理员连接。"""
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))
    from app.core.config import settings as cfg

    prefix, rest = cfg.DATABASE_URL.split("@", 1)
    server = rest.rsplit("/", 1)[0]
    admin_url = f"{prefix}@{server}/postgres"
    return admin_url, sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")


def _database_url(admin_url: str, name: str) -> str:
    return admin_url.rsplit("/", 1)[0] + f"/{name}"


def recreate_database(admin_engine: sa.Engine, name: str) -> None:
    """重建白名单内的迁移验证库。"""
    if name not in DATABASES.values():
        raise ValueError(f"拒绝操作非验证数据库: {name}")
    with admin_engine.connect() as connection:
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))


def drop_database(admin_engine: sa.Engine, name: str) -> None:
    """清理白名单内的迁移验证库。"""
    if name not in DATABASES.values():
        raise ValueError(f"拒绝操作非验证数据库: {name}")
    with admin_engine.connect() as connection:
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def run_upgrade(database_url: str) -> str:
    """在指定临时库执行 upgrade head，失败时携带 Alembic 输出。"""
    venv_python = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    command = [str(venv_python if venv_python.exists() else sys.executable), "-m", "alembic", "upgrade", "head"]
    process = subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = f"{process.stdout}\n{process.stderr}".strip()
    if process.returncode:
        raise RuntimeError(f"upgrade head 失败（{process.returncode}）\n{output}")
    return output


def apply_legacy_fixture(database_url: str) -> None:
    """将当前 schema 还原为受支持的历史结构。"""
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(LEGACY_FIXTURE.read_text(encoding="utf-8"))
    finally:
        engine.dispose()


def compare_schema(database_url: str) -> list[str]:
    """逐表比较 ORM 与数据库的列、索引和外键。"""
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))
    from app.db import models  # noqa: F401
    from app.db.database import Base

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    problems: list[str] = []
    database_tables = set(inspector.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)
    for table_name in sorted(model_tables - database_tables):
        problems.append(f"缺表: {table_name}")

    for table_name in sorted(model_tables & database_tables):
        table = Base.metadata.tables[table_name]
        database_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in database_columns:
                problems.append(f"缺列: {table_name}.{column.name}")

        database_indexes = {
            (item["name"], tuple(item["column_names"]), bool(item.get("unique")))
            for item in inspector.get_indexes(table_name)
        }
        for index in table.indexes:
            signature = (index.name, tuple(column.name for column in index.columns), bool(index.unique))
            if signature not in database_indexes:
                problems.append(f"缺索引: {table_name}.{index.name}")

        database_foreign_keys = {
            (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
            for item in inspector.get_foreign_keys(table_name)
        }
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            signature = (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
            )
            if signature not in database_foreign_keys:
                problems.append(f"缺外键: {table_name}.{constraint.name or signature}")
    engine.dispose()
    return problems


def verify_path(admin_url: str, admin_engine: sa.Engine, name: str, legacy: bool) -> None:
    """验证单条升级路径，并确认第二次 upgrade 幂等。"""
    database_name = DATABASES[name]
    database_url = _database_url(admin_url, database_name)
    recreate_database(admin_engine, database_name)
    run_upgrade(database_url)
    if legacy:
        apply_legacy_fixture(database_url)
        run_upgrade(database_url)
    run_upgrade(database_url)
    problems = compare_schema(database_url)
    if problems:
        raise RuntimeError(f"{name} schema 与 ORM 不一致：\n" + "\n".join(problems[:50]))
    print(f"[OK] {name}: upgrade、重复 upgrade、ORM schema 对比通过")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-db", action="store_true", help="保留两个临时验证库")
    arguments = parser.parse_args()
    admin_url, admin_engine = _admin_engine()
    try:
        verify_path(admin_url, admin_engine, "empty", legacy=False)
        verify_path(admin_url, admin_engine, "legacy", legacy=True)
    finally:
        if not arguments.keep_db:
            for database_name in DATABASES.values():
                drop_database(admin_engine, database_name)
        admin_engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
