"""应用健康快照契约测试。

覆盖：
- `_compute_application_health_snapshot` 只读聚合的分支：未纳管 / 健康 / 有离线实例；
- `run_application_health_snapshot_cycle` 会给每个应用写一条快照；
- 过期快照会被清理。

不调用 Alertmanager（默认 include_alerts=False 跳过网络）。
"""
import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.applications import (
    APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS,
    _compute_application_health_snapshot,
    run_application_health_snapshot_cycle,
)
from app.db.database import Base
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationHealthSnapshot,
    ApplicationResource,
)
from app.models.asset import Asset


def _make_engine():
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})


def _make_session_factory(engine):
    # app.api.applications 会连带导入 deployment_task 模型，其 JSONB 列是 PostgreSQL 专属类型，
    # 在 SQLite 上编译不了，全量 create_all 会失败；这里只建其余表（本套件用不到该表）。
    Base.metadata.create_all(
        bind=engine,
        tables=[t for name, t in Base.metadata.tables.items() if name != "application_deployment_tasks"],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class ComputeSnapshotTest(unittest.TestCase):
    def setUp(self):
        engine = _make_engine()
        self.Session = _make_session_factory(engine)
        self.db = self.Session()

    def test_without_resources_marks_unknown(self):
        app = Application(app_id="APP-1", name="应用1", environment="prod", status="active")
        self.db.add(app)
        self.db.commit()

        snapshot = _compute_application_health_snapshot(self.db, app, include_alerts=False)
        self.assertEqual(snapshot["health_status"], "unknown")
        self.assertEqual(snapshot["component_total"], 0)
        self.assertEqual(snapshot["host_total"], 0)

    def test_healthy_when_component_and_host_ok(self):
        app = Application(app_id="APP-2", name="应用2", environment="prod", status="active")
        self.db.add(app)
        asset = Asset(
            instance_id="i-ok",
            name="host-ok",
            ip="10.0.0.1",
            provider="manual",
            status="online",
            type="healthy",
            ssh_user="root",
            ssh_port=22,
        )
        self.db.add(asset)
        self.db.add(ApplicationResource(
            app_id="APP-2", resource_type="host", resource_id="i-ok", resource_name="host-ok",
        ))
        self.db.add(ApplicationComponent(
            component_id="COMP-1",
            app_id="APP-2",
            name="api",
            component_type="backend",
            deployment_type="systemd",
            control_method="systemd",
            control_ref="api.service",
            env="prod",
            status="active",
        ))
        self.db.commit()

        snapshot = _compute_application_health_snapshot(self.db, app, include_alerts=False)
        self.assertEqual(snapshot["health_status"], "healthy")
        self.assertEqual(snapshot["component_total"], 1)
        self.assertEqual(snapshot["component_active"], 1)
        self.assertEqual(snapshot["host_total"], 1)
        self.assertEqual(snapshot["host_healthy"], 1)
        self.assertEqual(snapshot["active_alert_count"], 0)

    def test_offline_component_marks_degraded(self):
        app = Application(app_id="APP-3", name="应用3", environment="prod", status="active")
        self.db.add(app)
        self.db.add(ApplicationComponent(
            component_id="COMP-2",
            app_id="APP-3",
            name="worker",
            component_type="backend",
            deployment_type="docker",
            control_method="docker",
            control_ref="worker",
            env="prod",
            status="offline",
        ))
        self.db.commit()

        snapshot = _compute_application_health_snapshot(self.db, app, include_alerts=False)
        self.assertEqual(snapshot["health_status"], "degraded")
        self.assertIn(
            "1 个运行实例离线",
            "；".join(snapshot["health_reasons"]),
        )


class SnapshotCycleTest(unittest.TestCase):
    def setUp(self):
        engine = _make_engine()
        self.Session = _make_session_factory(engine)

        import app.api.applications as apps_module
        self._orig_session_local = apps_module.SessionLocal if hasattr(apps_module, "SessionLocal") else None

        # Patch SessionLocal 在巡检器里使用的工厂。
        import app.db.database as db_module
        self._orig_db_session = db_module.SessionLocal
        db_module.SessionLocal = self.Session

        self.db = self.Session()
        self.db.add(Application(app_id="APP-HEALTH", name="巡检", environment="prod", status="active"))
        self.db.add(Application(app_id="APP-OFFLINE", name="下线应用", environment="prod", status="offline"))
        self.db.commit()

    def tearDown(self):
        import app.db.database as db_module
        db_module.SessionLocal = self._orig_db_session

    def test_cycle_writes_one_snapshot_per_app(self):
        written = run_application_health_snapshot_cycle()
        self.assertEqual(written, 2)
        rows = self.db.query(ApplicationHealthSnapshot).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.app_id for r in rows}, {"APP-HEALTH", "APP-OFFLINE"})

    def test_cycle_removes_expired_snapshots(self):
        # 造一条老快照。
        expired = datetime.now() - timedelta(hours=APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS + 24)
        self.db.add(ApplicationHealthSnapshot(
            app_id="APP-HEALTH",
            snapshot_time=expired,
            health_status="healthy",
            component_total=0,
            component_active=0,
            host_total=0,
            host_healthy=0,
            active_alert_count=0,
            domain_expiring_count=0,
            certificate_expiring_count=0,
        ))
        self.db.commit()

        run_application_health_snapshot_cycle()

        remaining = self.db.query(ApplicationHealthSnapshot).all()
        self.assertNotIn(expired, [r.snapshot_time for r in remaining])
        # 新快照仍写入。
        self.assertTrue(any(r.snapshot_time != expired for r in remaining))


if __name__ == "__main__":
    unittest.main()
