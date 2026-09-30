"""应用发布异步任务契约测试。"""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.application_routes.releases import _execute_release_job_with_session, recover_stale_running_releases
from app.schemas.application import ApplicationReleasePayload
from app.db.database import Base
from app.models.application import Application, ApplicationComponent, ApplicationRelease, ApplicationResource
from app.models.audit import AuditLog
from app.models.asset import Asset


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class ApplicationReleaseAsyncContractTest(unittest.TestCase):
    """验证发布任务可以由后台执行器更新发布记录状态。"""

    def setUp(self):
        self.db = _make_session()
        self.user = SimpleNamespace(id=1, username="tester", name="Tester", role="管理员", permission_overrides="")
        self.request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        self.app = Application(app_id="APP-1", name="订单系统", environment="prod", status="active")
        self.asset = Asset(
            instance_id="host-1",
            name="host-1",
            ip="10.0.0.1",
            provider="manual",
            type="healthy",
            status="online",
            ssh_user="root",
            ssh_port=22,
            credential_ref="cred-1",
        )
        self.component = ApplicationComponent(
            component_id="COMP-1",
            app_id="APP-1",
            name="api",
            deployment_type="kubernetes",
            control_ref="default/deployment/api",
            resource_refs='["host:host-1"]',
            env="prod",
            status="active",
        )
        self.release = ApplicationRelease(
            release_id="REL-ASYNC",
            app_id="APP-1",
            component_id="COMP-1",
            component_name="api",
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            artifact_ref="/opt/api.yaml",
            status="running",
            summary="发布任务已提交",
            started_at=datetime(2026, 5, 13, 10, 0, 0),
        )
        self.db.add_all([
            self.app,
            self.asset,
            self.component,
            self.release,
            ApplicationResource(app_id="APP-1", resource_type="host", resource_id="host-1", resource_name="host-1"),
        ])
        self.db.commit()

    def test_job_updates_release_to_success(self):
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id="COMP-1",
            artifact_ref="/opt/api.yaml",
        )
        result = SimpleNamespace(ok=True, stdout="applied", stderr="", duration_ms=123, code=0)

        with patch("app.api.application_routes.releases._execute_runtime_ssh_task", return_value=result):
            _execute_release_job_with_session(
                release_id="REL-ASYNC",
                payload=payload,
                request=self.request,
                db=self.db,
                current_user=self.user,
            )

        self.db.refresh(self.release)
        self.assertEqual("success", self.release.status)
        self.assertEqual(123, self.release.duration_ms)
        self.assertIn("applied", self.release.output)

    def test_ssh_release_audit_does_not_store_full_command(self):
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id="COMP-1",
            artifact_ref="/opt/api.yaml",
        )
        result = SimpleNamespace(ok=True, stdout="applied", stderr="", duration_ms=123, code=0)

        with patch("app.api.application_routes.runtime_executor.run_ssh_command", return_value=(True, "ok", result.stdout, "", 0)):
            with patch("app.api.application_routes.runtime_executor.get_asset_credential_content", return_value=""):
                _execute_release_job_with_session(
                    release_id="REL-ASYNC",
                    payload=payload,
                    request=self.request,
                    db=self.db,
                    current_user=self.user,
                )

        audit = self.db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        self.assertIsNotNone(audit)
        self.assertIn("release:REL-ASYNC:kubernetes", audit.resource)
        self.assertNotIn("kubectl", audit.resource)
        self.assertNotIn("/opt/api.yaml", audit.resource)

    def test_recover_stale_running_releases_marks_old_rows_failed(self):
        self.release.status = "success"
        stale = ApplicationRelease(
            release_id="REL-STALE",
            app_id="APP-1",
            version="2.0.1",
            env="prod",
            release_type="docker",
            status="running",
            started_at=datetime(2026, 5, 13, 9, 0, 0),
        )
        fresh = ApplicationRelease(
            release_id="REL-FRESH",
            app_id="APP-1",
            version="2.0.2",
            env="prod",
            release_type="docker",
            status="running",
            started_at=datetime(2999, 1, 1, 0, 0, 0),
        )
        self.db.add_all([stale, fresh])
        self.db.commit()

        with patch("app.api.application_routes.releases.SessionLocal", return_value=self.db):
            changed = recover_stale_running_releases(stale_minutes=30)

        stale = self.db.query(ApplicationRelease).filter(ApplicationRelease.release_id == "REL-STALE").first()
        fresh = self.db.query(ApplicationRelease).filter(ApplicationRelease.release_id == "REL-FRESH").first()
        self.assertEqual(1, changed)
        self.assertEqual("failed", stale.status)
        self.assertIn("中断", stale.summary)
        self.assertEqual("running", fresh.status)


if __name__ == "__main__":
    unittest.main()
