"""应用中心列表批量查询与分页契约测试。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.application_routes.catalog import list_applications
from app.api.application_routes.configs import list_application_configs
from app.api.application_routes.releases import list_application_releases
from app.api.application_routes.shared import _serialize_application, _serialize_applications_bulk
from app.db.database import Base
from app.models.application import Application, ApplicationComponent, ApplicationConfig, ApplicationLink, ApplicationRelease


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class ApplicationListPaginationContractTest(unittest.TestCase):
    """验证列表接口分页兼容旧数组响应，批量序列化不改变字段含义。"""

    def setUp(self):
        self.db = _make_session()
        self.user = SimpleNamespace(id=1, username="tester", name="Tester", role="管理员", permission_overrides="")
        now = datetime(2026, 5, 14, 11, 30, 0)
        apps = [
            Application(app_id=f"APP-{index}", name=f"应用{index}", status="active", environment="prod", created_at=now, updated_at=now)
            for index in range(4)
        ]
        self.db.add_all(apps)
        self.db.flush()
        self.db.add_all([
            ApplicationComponent(component_id="CMP-1", app_id="APP-1", name="api", status="active", deployment_type="systemd"),
            ApplicationLink(app_id="APP-1", title="入口", url="/app", created_at=now),
        ])
        self.db.add_all([
            ApplicationRelease(release_id=f"REL-{index}", app_id="APP-1", version=f"1.0.{index}", env="prod", release_type="external", status="success", started_at=now)
            for index in range(3)
        ])
        self.db.add_all([
            ApplicationConfig(config_id=f"CFG-{index}", app_id="APP-1", env="prod", key=f"KEY_{index}", value="value", value_type="plain", created_at=now, updated_at=now)
            for index in range(3)
        ])
        self.db.commit()

    def test_bulk_serializer_matches_single_serializer_for_list_fields(self):
        app = self.db.query(Application).filter(Application.app_id == "APP-1").first()
        single = _serialize_application(app, self.db)
        bulk = _serialize_applications_bulk([app], self.db)[0]

        for key in ("app_id", "name", "resource_count", "component_count", "link_count", "health_status", "created_at"):
            self.assertEqual(single[key], bulk[key])

    def test_application_list_supports_limit_offset_array_response(self):
        rows = list_applications(limit=2, offset=1, db=self.db, current_user=self.user)

        self.assertEqual(2, len(rows))
        self.assertEqual(["APP-2", "APP-1"], [item["app_id"] for item in rows])

    def test_release_and_config_lists_support_limit_offset(self):
        releases = list_application_releases(limit=2, offset=1, db=self.db, current_user=self.user)
        configs = list_application_configs("APP-1", limit=2, offset=1, db=self.db, current_user=self.user)

        self.assertEqual(["REL-1", "REL-0"], [item["release_id"] for item in releases])
        self.assertEqual(["CFG-1", "CFG-0"], [item["config_id"] for item in configs])


if __name__ == "__main__":
    unittest.main()
