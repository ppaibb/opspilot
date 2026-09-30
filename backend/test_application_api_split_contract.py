"""应用中心 API 路由拆分契约测试。"""
from __future__ import annotations

from pathlib import Path
import unittest


class ApplicationApiSplitContractTest(unittest.TestCase):
    """验证应用中心路由入口保持轻量，具体业务路由已拆到子模块。"""

    def test_api_entrypoint_is_not_a_monolith(self) -> None:
        root = Path(__file__).resolve().parent
        entrypoint = root / "app" / "api" / "applications.py"

        self.assertLessEqual(
            len(entrypoint.read_text(encoding="utf-8").splitlines()),
            220,
            "app/api/applications.py should only aggregate sub-routers and compatibility exports",
        )

    def test_router_still_imports_from_old_path(self) -> None:
        from app.api.applications import router

        self.assertTrue(router.routes)


if __name__ == "__main__":
    unittest.main()
