# 2026-10-03 全站 GUI 巡检问题修复的契约测试。
# 覆盖：告警重复通知按年龄升级/停发、K8s 核心组件 external 归类、
#       VLogs hits ISO 时间戳归一、审计日志 UTC→本地时间序列化。
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


class TestReminderEscalation(unittest.TestCase):
    """长时间未恢复的告警：提醒间隔升级 + 超时停发。"""

    def _dispatch_once(self, records, sent, rules, channels, settings, now, state="firing"):
        from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts

        alerts = [{
            "status": state,
            "labels": {
                "alertname": "TestAlert",
                "instance": "127.0.0.1",
                "severity": "warning",
                "opspilot_rule_id": "r-1",
            },
            "annotations": {"summary": "s", "description": "d"},
            "startsAt": now.isoformat(),
        }]
        dispatch_active_alerts(alerts, rules, channels, [], now, settings)

    def _make_db(self, records):
        class MockQuery:
            def __init__(self, fp=None):
                self.fp = fp
            def filter(self, *args, **kwargs):
                return self
            def first(self):
                return records.get(self.fp)

        class MockDbSession:
            def query(self, model):
                class QueryBuilder:
                    def filter(self, criterion):
                        return MockQuery(criterion.right.value)
                return QueryBuilder()
            def add(self, obj):
                records[obj.fingerprint] = obj
            def commit(self):
                pass
            def rollback(self):
                pass
            def close(self):
                pass
        return MockDbSession()

    RULES = [{
        "id": "r-1", "name": "TestAlert", "channel_ids": ["c-1"],
        "recovery_debounce_seconds": 60,
        "reminder_interval_minutes": 30,
        "max_reminders_cap": 0,
    }]
    CHANNELS = [{
        "id": "c-1", "name": "WeChat", "type": "webhook",
        "webhook_url": "https://example.invalid/hook", "enabled": True, "send_resolved": True,
    }]

    def _fingerprint(self) -> str:
        from app.api.monitoring.services.dashboard import _calculate_fingerprint

        labels = {
            "alertname": "TestAlert", "instance": "127.0.0.1",
            "severity": "warning", "opspilot_rule_id": "r-1",
        }
        return _calculate_fingerprint("TestAlert", "127.0.0.1", labels)

    def test_escalation_after_2h_and_8h(self):
        from app.models.monitoring_active_alert import MonitoringActiveAlert

        records, sent = {}, []
        settings = {
            "default_channel_ids": ["c-1"], "global_routing_mode": "fallback",
            "recovery_debounce_seconds": 60, "reminder_interval_minutes": 30,
            "max_reminders_cap": 0, "reminder_max_age_hours": 168,
        }
        now = datetime.now()
        fp = self._fingerprint()
        db_alert = MonitoringActiveAlert(
            fingerprint=fp, name="TestAlert", instance="127.0.0.1", state="firing",
            first_seen_at=now - timedelta(hours=3), last_notified_at=now,
            notification_count=1,
        )
        records[fp] = db_alert

        with patch("app.db.database.SessionLocal", return_value=self._make_db(records)), \
             patch("app.api.monitoring.services.alert_dispatcher.send_alert_to_channel",
                   side_effect=lambda ch, data: sent.append(data) or (True, "ok")):
            # 3 小时龄 + 距上次通知 31 分钟：< 2h 升级后间隔应为 120 分钟 → 拦截
            self._dispatch_once(records, sent, self.RULES, self.CHANNELS, settings, now + timedelta(minutes=31))
            self.assertEqual(len(sent), 0)
            # 距上次通知 121 分钟 → 升级间隔 120 分钟已到 → 发送
            self._dispatch_once(records, sent, self.RULES, self.CHANNELS, settings, now + timedelta(minutes=121))
            self.assertEqual(len(sent), 1)
            self.assertEqual(db_alert.notification_count, 2)

    def test_stop_after_max_age_hours(self):
        from app.models.monitoring_active_alert import MonitoringActiveAlert

        records, sent = {}, []
        settings = {
            "default_channel_ids": ["c-1"], "global_routing_mode": "fallback",
            "recovery_debounce_seconds": 60, "reminder_interval_minutes": 30,
            "max_reminders_cap": 0, "reminder_max_age_hours": 168,
        }
        now = datetime.now()
        fp = self._fingerprint()
        db_alert = MonitoringActiveAlert(
            fingerprint=fp, name="TestAlert", instance="127.0.0.1", state="firing",
            # 持续 firing 200 小时，远超 168 小时上限
            first_seen_at=now - timedelta(hours=200), last_notified_at=now - timedelta(hours=1),
            notification_count=5,
        )
        records[fp] = db_alert

        with patch("app.db.database.SessionLocal", return_value=self._make_db(records)), \
             patch("app.api.monitoring.services.alert_dispatcher.send_alert_to_channel",
                   side_effect=lambda ch, data: sent.append(data) or (True, "ok")):
            # 已超龄：即使距上次通知已满 1 小时也不再重复提醒，计数不变
            self._dispatch_once(records, sent, self.RULES, self.CHANNELS, settings, now)
            self.assertEqual(len(sent), 0)
            self.assertEqual(db_alert.notification_count, 5)
            self.assertEqual(db_alert.state, "firing")  # 告警保留在中心，不删不改状态


class TestCoreComponentClassification(unittest.TestCase):
    """K8s 核心组件健康归类：kubeadm 命名命中、k3s 式缺位标 external。"""

    @staticmethod
    def _pod(name, phase="Running", ready=True):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(
                phase=phase,
                conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
            ),
        )

    def test_kubeadm_static_pod_names_match(self):
        from app.api.clusters.cluster_mgmt import _classify_core_components

        pods = [
            self._pod("kube-apiserver-k8s-01"),
            self._pod("etcd-k8s-01"),
            self._pod("kube-scheduler-k8s-01"),
            self._pod("kube-controller-manager-k8s-01"),
            self._pod("kube-proxy-abc12"),
            self._pod("coredns-5d78c9869d-x2y4z"),
        ]
        result = _classify_core_components(pods)
        by_component = {}
        for c in result:
            by_component.setdefault(c["component"], []).append(c["status"])
        self.assertEqual(by_component["apiserver"], ["healthy"])
        self.assertEqual(by_component["etcd"], ["healthy"])
        self.assertEqual(by_component["scheduler"], ["healthy"])
        self.assertEqual(by_component["controller-manager"], ["healthy"])
        self.assertEqual(by_component["kube-proxy"], ["healthy"])
        self.assertEqual(by_component["coredns"], ["healthy"])
        self.assertNotIn("not_found", [c["status"] for c in result])
        self.assertNotIn("external", [c["status"] for c in result])

    def test_k3s_style_missing_control_plane_marks_external(self):
        from app.api.clusters.cluster_mgmt import _classify_core_components

        # k3s：只有 coredns 以 Pod 运行，控制面内嵌
        result = _classify_core_components([self._pod("coredns-5d78c9869d-x2y4z")])
        by_component = {c["component"]: c["status"] for c in result}
        self.assertEqual(by_component["coredns"], "healthy")
        for comp in ("apiserver", "etcd", "scheduler", "controller-manager", "kube-proxy"):
            self.assertEqual(by_component[comp], "external", f"{comp} 应标 external 而非 not_found")

    def test_unhealthy_pod_detected(self):
        from app.api.clusters.cluster_mgmt import _classify_core_components

        result = _classify_core_components([self._pod("coredns-bad", phase="Pending", ready=False)])
        coredns = next(c for c in result if c["component"] == "coredns")
        self.assertEqual(coredns["status"], "unhealthy")


class TestNormalizeHitsTimestamp(unittest.TestCase):
    """VLogs /hits 时间戳归一：ISO 字符串（当前实测格式）与各数量级数字。"""

    def test_iso_string_with_z(self):
        from app.api.monitoring.routes import _normalize_hits_ts_seconds

        expected = int(datetime(2026, 10, 3, 15, 20, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(_normalize_hits_ts_seconds("2026-10-03T15:20:00Z"), expected)

    def test_iso_string_naive_treated_as_utc(self):
        from app.api.monitoring.routes import _normalize_hits_ts_seconds

        expected = int(datetime(2026, 10, 3, 15, 20, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(_normalize_hits_ts_seconds("2026-10-03T15:20:00"), expected)

    def test_numeric_scales(self):
        from app.api.monitoring.routes import _normalize_hits_ts_seconds

        base = int(datetime(2026, 10, 3, 15, 20, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(_normalize_hits_ts_seconds(base), base)                # 秒
        self.assertEqual(_normalize_hits_ts_seconds(base * 1000), base)         # 毫秒
        self.assertEqual(_normalize_hits_ts_seconds(base * 1_000_000), base)    # 微秒
        self.assertEqual(_normalize_hits_ts_seconds(base * 1_000_000_000), base)  # 纳秒

    def test_garbage_returns_zero(self):
        from app.api.monitoring.routes import _normalize_hits_ts_seconds

        self.assertEqual(_normalize_hits_ts_seconds(None), 0)
        self.assertEqual(_normalize_hits_ts_seconds(""), 0)
        self.assertEqual(_normalize_hits_ts_seconds("not-a-time"), 0)


class TestAuditTimeSerialization(unittest.TestCase):
    """审计日志 time 字段：存储 naive UTC，序列化必须转本地时区显示。"""

    def test_get_audits_converts_utc_to_local(self):
        from app.api.settings import get_audits
        from app.models.audit import AuditLog

        stored = datetime(2026, 10, 3, 15, 14, 50)  # naive UTC（utcnow 落库）
        log = AuditLog(
            id=1, time=stored, user="admin", action="操作 监控",
            resource="/api/monitoring/logs/query", ip="127.0.0.1",
            result="success", risk_level="low", risk_reason="",
        )

        class FakeQuery:
            def order_by(self, *a, **k):
                return self
            def offset(self, *a, **k):
                return self
            def limit(self, *a, **k):
                return self
            def all(self):
                return [log]

        class FakeDb:
            def query(self, model):
                return FakeQuery()

        rows = get_audits(limit=10, offset=0, db=FakeDb(), _=None)
        offset_seconds = datetime.now().astimezone().utcoffset().total_seconds()
        expected = (stored + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rows[0]["time"], expected)
        # 在 UTC+8 机器上不得再出现"显示值比存储 UTC 还小 8 小时"的旧 bug
        self.assertNotEqual(rows[0]["time"], "2026-10-03 07:14:50")


if __name__ == "__main__":
    unittest.main()


class TestRollbackInsertsDialect(unittest.TestCase):
    """回滚 SQL 标识符引号按方言：PG 用双引号，MySQL 用反引号（2026-10-04 发现）。"""

    def test_postgres_uses_double_quotes(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts(
            "smoke_t", ["id", "note"], [{"id": 1, "note": "a'b"}],
            db_type="postgresql",
        )
        self.assertIn('"id"', sql)
        self.assertIn('"note"', sql)
        self.assertNotIn("`", sql)
        self.assertIn("'a''b'", sql)

    def test_mysql_uses_backticks(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts(
            "smoke_t", ["id"], [{"id": 1}], db_type="mysql",
        )
        self.assertIn("`id`", sql)

    def test_default_is_mysql_legacy(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts("t", ["id"], [{"id": 1}])
        self.assertIn("`id`", sql)


class TestRollbackUpdateForm(unittest.TestCase):
    """UPDATE 工单的回滚应为 UPDATE 形式（裸 INSERT 会撞主键，2026-10-04 实测发现）。"""

    def test_update_source_generates_update_rollback(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts(
            "smoke_t", ["id", "note"], [{"id": 1, "note": "old"}],
            db_type="postgresql",
            source_stmt="UPDATE smoke_t SET note='new' WHERE id = 1",
        )
        self.assertTrue(sql.startswith("UPDATE smoke_t SET"))
        self.assertIn('"note" = ', sql)
        self.assertIn("WHERE id = 1", sql)
        self.assertNotIn("INSERT", sql)

    def test_delete_source_keeps_insert_rollback(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts(
            "smoke_t", ["id"], [{"id": 2}], db_type="mysql",
            source_stmt="DELETE FROM smoke_t WHERE id = 2",
        )
        self.assertIn("INSERT INTO smoke_t", sql)

    def test_update_without_where_falls_back_to_insert(self):
        from app.services.db_ops import build_rollback_inserts

        sql = build_rollback_inserts(
            "smoke_t", ["id"], [{"id": 3}], db_type="mysql",
            source_stmt="UPDATE smoke_t SET x=1",
        )
        self.assertIn("INSERT INTO smoke_t", sql)
