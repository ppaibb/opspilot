"""应用时间线服务拆分契约测试。"""
from __future__ import annotations

import unittest


class ApplicationTimelineServiceSplitContractTest(unittest.TestCase):
    """验证时间线聚合逻辑已从路由文件拆到 service 层。"""

    def test_timeline_service_exports_converters(self) -> None:
        from app.services.application_timeline import normalize_alert_time, runtime_control_timeline_event

        self.assertEqual("2026-05-09 07:23:11", normalize_alert_time("2026-05-09T07:23:11.245Z"))
        self.assertIsNone(runtime_control_timeline_event(type("Component", (), {})()))

    def test_old_api_module_keeps_timeline_converter_compatibility(self) -> None:
        from app.api.applications import _normalize_alert_time

        self.assertEqual("", _normalize_alert_time("-"))


if __name__ == "__main__":
    unittest.main()
