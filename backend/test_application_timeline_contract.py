"""应用时间线聚合契约测试。

只覆盖纯函数型时间线转换器：
- 发布记录 -> 时间线事件
- 配置版本 -> 时间线事件（含凭证引用不回显明文）
- 运行实例最近控制 -> 时间线事件
- Alertmanager 时间字段归一化

不调用数据库和 Alertmanager，避免引入外部依赖。
"""
import unittest
from types import SimpleNamespace

from app.api.applications import (
    _config_revision_timeline_event,
    _normalize_alert_time,
    _release_timeline_event,
    _runtime_control_timeline_event,
)


class ApplicationTimelineContractTest(unittest.TestCase):
    def test_release_event_success(self):
        row = SimpleNamespace(
            release_id="REL-1",
            status="success",
            release_type="ansible",
            version="1.2.3",
            finished_at="2026-05-09 10:00:00",
            started_at="2026-05-09 09:59:00",
            summary="部署完成",
            target_names='["host-a","host-b"]',
            template_name="",
            operator="alice",
            component_id="COMP-1",
            component_name="api",
        )

        event = _release_timeline_event(row)

        self.assertEqual(event["category"], "release")
        self.assertEqual(event["severity"], "success")
        self.assertEqual(event["time"], "2026-05-09 10:00:00")
        self.assertIn("Ansible 发布", event["title"])
        self.assertIn("1.2.3", event["title"])
        self.assertIn("成功", event["title"])
        self.assertIn("host-a", event["summary"])
        self.assertEqual(event["operator"], "alice")
        self.assertEqual(event["entity_id"], "COMP-1")

    def test_release_event_failed_falls_back_to_started_at(self):
        row = SimpleNamespace(
            release_id="REL-2",
            status="failed",
            release_type="external",
            version="",
            finished_at="",
            started_at="2026-05-09 09:00:00",
            summary="",
            target_names="",
            template_name="",
            operator="",
            component_id="",
            component_name="",
        )

        event = _release_timeline_event(row)

        self.assertEqual(event["severity"], "error")
        self.assertEqual(event["time"], "2026-05-09 09:00:00")
        self.assertIn("外部发布", event["title"])
        self.assertIn("失败", event["title"])

    def test_config_credential_ref_event_does_not_leak_value(self):
        """凭证引用变更在时间线里只暴露 credential_ref，不允许出现明文值。"""
        row = SimpleNamespace(
            revision_id="CFGREV-1",
            config_id="CFG-1",
            change_type="update",
            value_type="credential_ref",
            old_value_snapshot="super-secret-old",  # 应该被忽略
            new_value_snapshot="super-secret-new",  # 应该被忽略
            old_credential_ref="cred-1",
            new_credential_ref="cred-2",
            key="DB_PASSWORD",
            env="prod",
            created_at="2026-05-09 11:00:00",
            operator="bob",
        )

        event = _config_revision_timeline_event(row)

        self.assertEqual(event["category"], "config")
        self.assertNotIn("super-secret-old", event["summary"])
        self.assertNotIn("super-secret-new", event["summary"])
        self.assertIn("cred-1", event["summary"])
        self.assertIn("cred-2", event["summary"])
        self.assertIn("DB_PASSWORD", event["title"])
        self.assertEqual(event["operator"], "bob")

    def test_config_plain_event_summarizes_diff(self):
        row = SimpleNamespace(
            revision_id="CFGREV-2",
            config_id="CFG-2",
            change_type="update",
            value_type="plain",
            old_value_snapshot="INFO",
            new_value_snapshot="DEBUG",
            old_credential_ref="",
            new_credential_ref="",
            key="LOG_LEVEL",
            env="prod",
            created_at="2026-05-09 11:00:00",
            operator="bob",
        )

        event = _config_revision_timeline_event(row)

        self.assertEqual(event["severity"], "info")
        self.assertIn("INFO", event["summary"])
        self.assertIn("DEBUG", event["summary"])

    def test_runtime_event_skipped_without_action(self):
        """从未执行过控制的实例不应生成时间线事件。"""
        component = SimpleNamespace(
            component_id="COMP-1",
            name="api",
            last_control_action="",
            last_control_at="",
            last_control_result="",
            last_control_duration_ms=0,
            last_control_output="",
        )

        self.assertIsNone(_runtime_control_timeline_event(component))

    def test_runtime_event_success(self):
        component = SimpleNamespace(
            component_id="COMP-1",
            name="api",
            last_control_action="restart",
            last_control_at="2026-05-09 12:00:00",
            last_control_result="success",
            last_control_duration_ms=1200,
            last_control_output="ok\nActive: active (running)",
        )

        event = _runtime_control_timeline_event(component)

        self.assertIsNotNone(event)
        self.assertEqual(event["severity"], "success")
        self.assertEqual(event["category"], "runtime_control")
        self.assertIn("重启", event["title"])
        self.assertIn("api", event["title"])
        self.assertIn("1200ms", event["summary"])
        self.assertIn("Active", event["summary"])

    def test_runtime_event_failed_marks_error(self):
        component = SimpleNamespace(
            component_id="COMP-2",
            name="worker",
            last_control_action="stop",
            last_control_at="2026-05-09 12:10:00",
            last_control_result="failed",
            last_control_duration_ms=300,
            last_control_output="Error: not found",
        )

        event = _runtime_control_timeline_event(component)

        self.assertEqual(event["severity"], "error")
        self.assertIn("停止", event["title"])

    def test_normalize_alert_time_strips_timezone_and_millis(self):
        self.assertEqual(_normalize_alert_time("2026-05-09T07:23:11.245Z"), "2026-05-09 07:23:11")
        self.assertEqual(_normalize_alert_time("2026-05-09T07:23:11+00:00"), "2026-05-09 07:23:11")
        self.assertEqual(_normalize_alert_time(""), "")
        self.assertEqual(_normalize_alert_time("-"), "")


if __name__ == "__main__":
    unittest.main()
