"""应用健康服务拆分契约测试。"""
from __future__ import annotations

import unittest


class ApplicationHealthServiceSplitContractTest(unittest.TestCase):
    """验证健康聚合逻辑已经从路由文件拆到 service 层。"""

    def test_health_service_exports_snapshot_functions(self) -> None:
        from app.services.application_health import (
            APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS,
            build_application_health,
            compute_application_health_snapshot,
            run_application_health_snapshot_cycle,
        )

        health = build_application_health(
            app_status="active",
            component_statuses=["active"],
            host_health_values=["healthy"],
        )
        self.assertEqual("healthy", health["health_status"])
        self.assertGreater(APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS, 0)
        self.assertTrue(callable(compute_application_health_snapshot))
        self.assertTrue(callable(run_application_health_snapshot_cycle))

    def test_old_api_module_keeps_health_function_compatibility(self) -> None:
        from app.api.applications import _build_application_health

        health = _build_application_health("offline", [], [])
        self.assertEqual("offline", health["health_status"])


if __name__ == "__main__":
    unittest.main()
