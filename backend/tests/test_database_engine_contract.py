"""数据库引擎配置契约测试。"""
from __future__ import annotations

import unittest

from app.db import database


class DatabaseEngineContractTest(unittest.TestCase):
    """验证不同数据库类型会得到正确的 SQLAlchemy 引擎参数。"""

    def test_sqlite_keeps_local_thread_compatibility(self) -> None:
        """SQLite 保留本地多线程访问兼容参数，不启用生产连接池参数。"""
        self.assertTrue(hasattr(database, "build_engine_kwargs"))

        kwargs = database.build_engine_kwargs("sqlite:///./opspilot.db")

        self.assertEqual({"check_same_thread": False}, kwargs["connect_args"])
        self.assertNotIn("pool_pre_ping", kwargs)
        self.assertNotIn("pool_size", kwargs)
        self.assertNotIn("max_overflow", kwargs)

    def test_postgresql_uses_health_checked_pool(self) -> None:
        """PostgreSQL 使用连接健康检查和连接池，不携带 SQLite 专属参数。"""
        self.assertTrue(hasattr(database, "build_engine_kwargs"))

        kwargs = database.build_engine_kwargs(
            "postgresql+psycopg://opspilot:secret@db:5432/opspilot",
        )

        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertGreaterEqual(kwargs["pool_size"], 1)
        self.assertGreaterEqual(kwargs["max_overflow"], 0)
        self.assertGreaterEqual(kwargs["pool_recycle"], 300)
        self.assertNotIn("connect_args", kwargs)


if __name__ == "__main__":
    unittest.main()
