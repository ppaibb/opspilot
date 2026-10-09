import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api.alert_center import (
    TestFireRequest,
    _read_rules_config,
    test_fire_webhook,
)
from app.api.monitoring import DEFAULT_ALERT_RULES
from app.main import app
from app.models.system_config import SystemConfig


class _EmptyQuery:
    """模拟没有任何 SystemConfig 记录的查询结果。"""

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


class _EmptyDb:
    """仅实现 _read_rules_config 所需的最小数据库接口。"""

    def query(self, model):
        self.model = model
        return _EmptyQuery()


class _OldEventQuery:
    """模拟数据库里只有历史无关 Webhook 事件。"""

    def __init__(self):
        self.filtered_by_fingerprint = False

    def filter(self, *_args, **_kwargs):
        self.filtered_by_fingerprint = True
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        if self.filtered_by_fingerprint:
            return None
        return SimpleNamespace(
            event_id="old-event",
            received_at="2026-04-21 10:47:23",
            summary="历史告警",
            alert_count=1,
        )


class _WebhookEventDb:
    """仅实现 test_fire_webhook 查询最近事件所需的最小数据库接口。"""

    def query(self, _model):
        return _OldEventQuery()


class AlertCenterRulesContractTest(unittest.TestCase):
    def test_rules_status_uses_default_rules_when_config_is_missing(self):
        """告警中心规则状态页必须和告警规则页看到同一组默认规则。"""
        db = _EmptyDb()

        rules = _read_rules_config(db)

        self.assertEqual(SystemConfig, db.model)
        self.assertEqual(len(DEFAULT_ALERT_RULES), len(rules))
        self.assertEqual(DEFAULT_ALERT_RULES[0]["name"], rules[0]["name"])

    def test_silence_requires_alert_rule_manage_permission(self):
        """创建告警静默属于写操作，不能只靠规则查看权限放行。"""
        route = next(
            item for item in app.routes
            if getattr(item, "path", "") == "/api/monitoring/alerts/silence"
        )
        permission_dependencies = [
            dependency.call
            for dependency in route.dependant.dependencies
            if getattr(dependency.call, "__name__", "") == "_permission_guard"
        ]

        self.assertTrue(permission_dependencies)
        closure_values = [
            cell.cell_contents
            for cell in permission_dependencies[0].__closure__ or []
        ]
        self.assertIn(
            ("module.monitoring.rules", "action.monitoring.rules.manage"),
            closure_values,
        )

    def test_test_fire_does_not_mark_unrelated_old_event_as_received(self):
        """测试触发只应把本次 fingerprint 对应事件认定为已接收。"""
        fake_response = SimpleNamespace(status_code=200)

        with patch(
            "app.api.alert_center._read_notifications_config",
            return_value=[
                {
                    "id": "channel-1",
                    "webhook_url": "http://127.0.0.1:8001/api/monitoring/webhooks/alertmanager/test",
                }
            ],
        ), patch("app.api.alert_center.secrets.token_hex", return_value="abcd"), patch(
            "app.api.alert_center.http_requests.post",
            return_value=fake_response,
        ):
            result = test_fire_webhook(TestFireRequest(channel_id="channel-1"), _WebhookEventDb())

        self.assertTrue(result["sent"])
        self.assertFalse(result["received"])
        self.assertIsNone(result["event"])

    def test_alert_settings_endpoints(self):
        """测试全局告警设置的 GET 和 PUT 接口。"""
        from app.api.alert_center import get_alert_settings, save_alert_settings, AlertSettingsModel
        db = _EmptyDb()
        
        # Test GET
        data = get_alert_settings(db)
        self.assertEqual(data["global_routing_mode"], "fallback")

        # Test PUT
        payload = AlertSettingsModel(
            default_channel_ids=["channel-wechat-default"],
            global_routing_mode="all",
            send_resolved_global=False
        )
        
        class _MockDb:
            def __init__(self):
                self.added = []
            def query(self, model):
                return _EmptyQuery()
            def add(self, obj):
                self.added.append(obj)
            def commit(self):
                pass
        
        db_mock = _MockDb()
        data_put = save_alert_settings(payload, db_mock)
        self.assertEqual(data_put.global_routing_mode, "all")
        self.assertFalse(data_put.send_resolved_global)

    def test_silence_management_endpoints(self):
        """测试静默规则列表接口。"""
        from app.api.alert_center import get_silence_rules
        db = _EmptyDb()

        data = get_silence_rules(db)
        self.assertIn("silences", data)

    def test_active_alerts_sync_logic(self):
        """测试活动告警的同步和状态。"""
        from app.models.monitoring_active_alert import MonitoringActiveAlert
        from app.api.alert_center import get_active_alerts
        
        # 准备一个 Mock 数据库
        class _MockDb:
            def __init__(self):
                self.records = []
                self.deleted = []
            def query(self, model):
                self.model = model
                class _Query:
                    def __init__(self, parent):
                        self.parent = parent
                    def filter(self, *args, **kwargs):
                        return self
                    def first(self):
                        return None
                    def order_by(self, *args, **kwargs):
                        return self
                    def all(self):
                        # 返回 Mock 的活动告警记录
                        return [
                            MonitoringActiveAlert(
                                fingerprint="fp1",
                                name="OpsPilotNodeDiskFull",
                                instance="192.168.3.99",
                                job="opspilot-node",
                                state="pending",
                                severity="critical",
                                summary="主机磁盘空间不足",
                                description="可用空间不足 10%",
                                labels='{"alertname": "OpsPilotNodeDiskFull", "instance": "192.168.3.99"}'
                            )
                        ]
                return _Query(self)
            def add(self, obj):
                self.records.append(obj)
            def commit(self):
                pass
            def rollback(self):
                pass

        with patch("app.api.alert_center._fetch_alerts") as mock_fetch, \
             patch("app.api.alert_center._read_monitoring_config", return_value={}):
            db = _MockDb()
            res = get_active_alerts(db)
            
            # 验证 _fetch_alerts 被触发调用
            mock_fetch.assert_called_once()
            
            # 验证返回结构包含我们 Mock 的活跃告警以及其 pending 状态
            self.assertEqual(res["total"], 1)
            alert = res["alerts"][0]
            self.assertEqual(alert["name"], "OpsPilotNodeDiskFull")
            self.assertEqual(alert["status"], "pending")
            self.assertEqual(alert["severity"], "critical")

    def test_alert_dispatcher_noise_reduction(self):
        """测试告警发送防抖与持续提醒间隔等降噪逻辑。"""
        from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
        from app.models.monitoring_active_alert import MonitoringActiveAlert
        from datetime import datetime, timedelta

        # 我们模拟一个发送通道和规则
        channels = [{
            "id": "c-1",
            "name": "WeChat",
            "type": "webhook",
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            "enabled": True,
            "send_resolved": True
        }]
        rules = [{
            "id": "r-1",
            "name": "TestAlert",
            "channel_ids": ["c-1"],
            "recovery_debounce_seconds": 60,
            "reminder_interval_minutes": 30,
            "max_reminders_cap": 2
        }]
        silences = []
        alert_settings = {
            "default_channel_ids": ["c-1"],
            "global_routing_mode": "fallback",
            "send_resolved_global": True,
            "recovery_debounce_seconds": 999,
            "reminder_interval_minutes": 999,
            "max_reminders_cap": 999
        }

        # 模拟数据库操作
        records = {}
        sent_notifications = []

        # Patch send_alert_to_channel to capture messages sent
        def mock_send(channel, alert_data):
            sent_notifications.append((channel["id"], alert_data))
            return True, "success"

        # Patch SessionLocal to return a mock DB
        class MockQuery:
            def __init__(self, fp=None):
                self.fp = fp
            def filter(self, *args, **kwargs):
                return self
            def first(self):
                if self.fp in records:
                    return records[self.fp]
                return None

        class MockDbSession:
            def query(self, model):
                class QueryBuilder:
                    def filter(self, criterion):
                        # Simple extraction of fingerprint from criterion
                        # e.g., MonitoringActiveAlert.fingerprint == fp
                        fp = criterion.right.value
                        return MockQuery(fp)
                return QueryBuilder()
            def add(self, obj):
                records[obj.fingerprint] = obj
            def commit(self):
                pass
            def delete(self, obj):
                if obj.fingerprint in records:
                    del records[obj.fingerprint]
            def close(self):
                pass

        with patch("app.db.database.SessionLocal", return_value=MockDbSession()), \
             patch("app.api.monitoring.services.alert_dispatcher.send_alert_to_channel", side_effect=mock_send):
            
            now = datetime.now()
            alerts = [{
                "status": "firing",
                "labels": {
                    "alertname": "TestAlert",
                    "instance": "127.0.0.1",
                    "severity": "warning",
                    "opspilot_rule_id": "r-1"
                },
                "annotations": {
                    "summary": "This is a test alert",
                    "description": "Details"
                },
                "startsAt": now.isoformat()
            }]

            # 1. 首次触发
            dispatch_active_alerts(alerts, rules, channels, silences, now, alert_settings)
            self.assertEqual(len(sent_notifications), 1)
            self.assertEqual(sent_notifications[0][1]["name"], "TestAlert")
            
            # Check DB record exists
            fp = list(records.keys())[0]
            db_alert = records[fp]
            self.assertEqual(db_alert.notification_count, 1)
            self.assertEqual(db_alert.state, "firing")

            # 2. 5分钟后再次触发（小于重复提醒间隔30分钟）
            sent_notifications.clear()
            dispatch_active_alerts(alerts, rules, channels, silences, now + timedelta(minutes=5), alert_settings)
            self.assertEqual(len(sent_notifications), 0) # 被拦截

            # 3. 31分钟后再次触发（大于30分钟，应该发送第二次通知）
            sent_notifications.clear()
            dispatch_active_alerts(alerts, rules, channels, silences, now + timedelta(minutes=31), alert_settings)
            self.assertEqual(len(sent_notifications), 1)
            self.assertEqual(db_alert.notification_count, 2)

            # 4. 62分钟后再次触发（超出 max_reminders_cap=2 限制）
            sent_notifications.clear()
            dispatch_active_alerts(alerts, rules, channels, silences, now + timedelta(minutes=62), alert_settings)
            self.assertEqual(len(sent_notifications), 0) # 达到最大次数被拦截

            # 5. 收到恢复通知（防抖），状态变更为 resolving
            resolved_alerts = [{
                "status": "resolved",
                "labels": {
                    "alertname": "TestAlert",
                    "instance": "127.0.0.1",
                    "severity": "warning",
                    "opspilot_rule_id": "r-1"
                },
                "annotations": {
                    "summary": "This is a test alert",
                    "description": "Details"
                },
                "startsAt": now.isoformat()
            }]
            sent_notifications.clear()
            
            # Patch schedule_coroutine 以避免真正运行恢复防抖后台协程
            with patch("app.api.monitoring.services.alert_dispatcher.schedule_coroutine") as mock_task:
                dispatch_active_alerts(resolved_alerts, rules, channels, silences, now + timedelta(minutes=70), alert_settings)
                mock_task.assert_called_once()
                self.assertEqual(db_alert.state, "resolving")
                self.assertIsNotNone(db_alert.recovery_candidate_at)
                self.assertEqual(len(sent_notifications), 0) # 不会立刻发送

            # 6. 在防抖期内再次 firing
            sent_notifications.clear()
            dispatch_active_alerts(alerts, rules, channels, silences, now + timedelta(minutes=70, seconds=30), alert_settings)
            self.assertEqual(db_alert.state, "firing")
            self.assertIsNone(db_alert.recovery_candidate_at)
            self.assertEqual(len(sent_notifications), 0) # 无抖动通知


if __name__ == "__main__":
    unittest.main()

