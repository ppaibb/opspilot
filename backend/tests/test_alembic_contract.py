"""Alembic 迁移基线契约测试。"""
from __future__ import annotations

import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


class AlembicContractTest(unittest.TestCase):
    """验证迁移脚手架和基线迁移文件存在且绑定 ORM metadata。"""

    def test_alembic_scaffold_exists(self) -> None:
        """生产数据库必须具备可审计的 Alembic 迁移入口。"""
        required_files = [
            BACKEND_DIR / "alembic.ini",
            BACKEND_DIR / "alembic" / "env.py",
            BACKEND_DIR / "alembic" / "script.py.mako",
            BACKEND_DIR / "alembic" / "versions" / "0001_initial_schema.py",
        ]

        missing_files = [str(path.relative_to(BACKEND_DIR)) for path in required_files if not path.exists()]
        self.assertEqual([], missing_files)

    def test_env_loads_application_metadata(self) -> None:
        """Alembic env 必须导入统一模型注册表，避免遗漏表结构。"""
        env_path = BACKEND_DIR / "alembic" / "env.py"

        self.assertTrue(env_path.exists(), "alembic/env.py must exist")
        env_py = env_path.read_text(encoding="utf-8")

        self.assertIn("from app.db.database import Base", env_py)
        self.assertIn("from app.db import models", env_py)
        self.assertIn("target_metadata = Base.metadata", env_py)

    def test_initial_revision_uses_metadata_baseline(self) -> None:
        """首个迁移基线必须基于当前 ORM metadata 建表。"""
        revision_path = BACKEND_DIR / "alembic" / "versions" / "0001_initial_schema.py"

        self.assertTrue(revision_path.exists(), "initial Alembic revision must exist")
        revision = revision_path.read_text(encoding="utf-8")

        self.assertIn("revision = \"0001_initial_schema\"", revision)
        self.assertIn("Base.metadata.create_all", revision)
        self.assertIn("Base.metadata.sorted_tables", revision)


if __name__ == "__main__":
    unittest.main()
