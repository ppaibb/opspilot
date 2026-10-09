# Alembic 全量迁移验证驱动：重建临时库 → upgrade head → 报告首个错误与 schema 差异。
# 用法（backend 目录）：
#   .venv/Scripts/python.exe ../scripts/verify_alembic_full_upgrade.py [--keep-db]
# 依赖 app.core.config 的 DATABASE_URL（连到哪台 PG，就在哪台建 opspilot_mig_check 临时库）。
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
TEMP_DB = "opspilot_mig_check"


def _admin_engine() -> tuple[str, sa.Engine]:
    """从应用配置推导管理员连接（连到同实例的 postgres 库）。"""
    # Settings 从 CWD 读 backend/.env，必须先切目录再导入 app 配置
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))
    from app.core.config import settings as cfg

    prefix, rest = cfg.DATABASE_URL.split("@", 1)
    server = rest.rsplit("/", 1)[0]
    return f"{prefix}@{server}/postgres", sa.create_engine(
        f"{prefix}@{server}/postgres", isolation_level="AUTOCOMMIT"
    )


def recreate_temp_db(admin_eng: sa.Engine) -> None:
    with admin_eng.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {TEMP_DB}"))
        conn.execute(sa.text(f"CREATE DATABASE {TEMP_DB}"))


def run_upgrade(temp_url: str) -> tuple[int, str]:
    """在临时库上执行 alembic upgrade head，返回 (退出码, 合并输出)。"""
    proc = subprocess.run(
        [str(BACKEND_DIR / ".venv/Scripts/python.exe"), "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**__import__("os").environ, "DATABASE_URL": temp_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode, (proc.stdout + "\n" + proc.stderr)


def main() -> int:
    keep = "--keep-db" in sys.argv
    admin_url, admin_eng = _admin_engine()
    temp_url = admin_url.replace("/postgres", f"/{TEMP_DB}")
    recreate_temp_db(admin_eng)
    code, output = run_upgrade(temp_url)
    tail = "\n".join(output.strip().splitlines()[-6:])
    print(tail)
    if code != 0:
        print(f"\n[FAIL] upgrade head 退出码 {code}")
        return 1
    print("\n[OK] 空库全量 upgrade head 通过")

    # 二次校验：upgrade 出来的 schema 必须与当前 ORM 模型一致（表/列级别）
    sys.path.insert(0, str(BACKEND_DIR))
    os.chdir(BACKEND_DIR)  # 确保 app 配置可导入（Settings 读 backend/.env）
    from app.db.database import Base
    from app.db import models  # noqa: F401  灌满 metadata

    insp = sa.inspect(sa.create_engine(temp_url))
    db_tables = set(insp.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables.keys())
    problems: list[str] = []
    for missing in sorted(model_tables - db_tables):
        problems.append(f"缺表: {missing}")
    for table in sorted(model_tables & db_tables):
        db_cols = {c["name"] for c in insp.get_columns(table)}
        for col in Base.metadata.tables[table].columns:
            if col.name not in db_cols:
                problems.append(f"缺列: {table}.{col.name}")
    if problems:
        print(f"\n[FAIL] upgrade 后 schema 与模型不一致（{len(problems)} 处）:")
        for p in problems[:30]:
            print("  -", p)
        return 1
    print(f"[OK] schema 与模型一致（{len(model_tables)} 张表逐列比对通过）")

    if not keep:
        insp.engine.dispose()  # 释放比对连接，否则临时库删不掉
        with admin_eng.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {TEMP_DB}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
