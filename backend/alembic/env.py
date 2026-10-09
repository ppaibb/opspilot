"""Alembic 运行环境配置。"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.core.config import settings
from app.db.database import Base
from app.db import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 读取这里的 metadata 做自动生成和迁移基线。
target_metadata = Base.metadata


def _database_url_for_alembic() -> str:
    """返回转义后的数据库连接串，避免 URL 中的百分号被 ConfigParser 误解析。"""
    return settings.DATABASE_URL.replace("%", "%%")


def _ensure_version_table(connection) -> None:
    """预建 alembic_version 表，version_num 放宽到 VARCHAR(64)。

    alembic 默认按 VARCHAR(32) 建版本表，而 head 修订号
    '20260922_security_hardening_merge' 有 33 个字符——存量库的版本表是早期建的
    没事，全新库 upgrade 到 head 时会撞 StringDataRightTruncation。
    alembic 见表已存在就不会再建，这里预建宽列版本即可。
    """
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(64) NOT NULL,"
        " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))


def run_migrations_offline() -> None:
    """离线模式生成 SQL，不建立真实数据库连接。"""
    context.configure(
        url=_database_url_for_alembic(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式直接连接数据库执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url_for_alembic()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_version_table(connection)
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
