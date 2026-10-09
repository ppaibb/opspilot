"""监控路由契约测试。"""
from __future__ import annotations

import unittest

from app.api.monitoring.routes import router


class MonitoringRoutesContractTest(unittest.TestCase):
    """验证前端依赖的监控查询路由被正确注册。"""

    def _registered_methods(self, path: str) -> set[str]:
        """返回指定路径在 FastAPI Router 中注册的方法集合。"""
        methods: set[str] = set()
        for route in router.routes:
            if getattr(route, "path", "") == path:
                methods.update(getattr(route, "methods", set()) or set())
        return methods

    def test_alert_rules_support_get_and_put(self) -> None:
        """告警规则页面进入时需要 GET，保存时需要 PUT。"""
        methods = self._registered_methods("/rules")

        self.assertIn("GET", methods)
        self.assertIn("PUT", methods)

    def test_monitoring_targets_support_get(self) -> None:
        """主机可观测性接入清单需要 GET，供统一采集视图读取。"""
        methods = self._registered_methods("/targets")

        self.assertIn("GET", methods)


if __name__ == "__main__":
    unittest.main()
