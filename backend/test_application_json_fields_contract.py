"""应用中心 JSON 列表字段兼容契约测试。"""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.application_routes.catalog import list_applications
from app.api.application_routes.permissions import (
    _application_action_allowed,
    _application_action_role_allowed,
    _application_visible_to_user,
)
from app.core.json_fields import dump_json_list, normalize_json_list
from app.db.database import Base
from app.models.application import Application, ApplicationComponent, ApplicationRelease
from app.services.application_releases import serialize_release


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class ApplicationJsonFieldsContractTest(unittest.TestCase):
    """验证应用中心列表型 JSON 字段支持老字符串和新 list 两种形态。"""

    def setUp(self):
        self.db = _make_session()

    def tearDown(self):
        self.db.close()

    def test_json_list_helpers_accept_legacy_string_and_list(self):
        self.assertEqual(["admin", "ops"], normalize_json_list('["admin", "ops", "admin"]'))
        self.assertEqual(["admin", "ops"], normalize_json_list(["admin", "ops", "admin"]))
        self.assertEqual("[]", dump_json_list(""))

    def test_orm_fields_read_as_lists_after_persistence(self):
        app = Application(
            app_id="APP-JSON",
            name="JSON 应用",
            tags=["ops", "prod", "ops"],
            allowed_roles='["admin"]',
        )
        component = ApplicationComponent(
            component_id="CMP-JSON",
            app_id="APP-JSON",
            name="api",
            resource_refs=["host:host-1", "host:host-1"],
        )
        release = ApplicationRelease(
            release_id="REL-JSON",
            app_id="APP-JSON",
            version="1.0.0",
            target_resource_ids=["host-1"],
            target_names='["主机1"]',
        )
        self.db.add_all([app, component, release])
        self.db.commit()
        self.db.expire_all()

        saved_app = self.db.query(Application).filter(Application.app_id == "APP-JSON").first()
        saved_component = self.db.query(ApplicationComponent).filter(ApplicationComponent.component_id == "CMP-JSON").first()
        saved_release = self.db.query(ApplicationRelease).filter(ApplicationRelease.release_id == "REL-JSON").first()

        self.assertEqual(["ops", "prod"], saved_app.tags)
        self.assertEqual(["admin"], saved_app.allowed_roles)
        self.assertEqual(["host:host-1"], saved_component.resource_refs)
        self.assertEqual(["host-1"], serialize_release(saved_release)["target_resource_ids"])
        self.assertEqual(["主机1"], serialize_release(saved_release)["target_names"])

    def test_permissions_and_list_query_keep_legacy_empty_string_visible(self):
        app = Application(app_id="APP-OLD", name="旧应用", allowed_roles=["developer"])
        self.db.add(app)
        self.db.commit()
        self.db.execute(text("UPDATE applications SET allowed_roles = '' WHERE app_id = 'APP-OLD'"))
        self.db.commit()
        self.db.expire_all()

        row = self.db.query(Application).filter(Application.app_id == "APP-OLD").first()
        user = SimpleNamespace(id=1, username="dev", name="Dev", role="developer", permission_overrides="")

        self.assertTrue(_application_action_role_allowed(row, "developer", "view"))
        rows = list_applications(limit=10, offset=0, db=self.db, current_user=user)
        self.assertEqual(["APP-OLD"], [item["app_id"] for item in rows])

    def test_application_scope_limits_non_admin_app_managers(self):
        """应用目录管理权限不能绕过单个应用的可见和动作范围。"""
        app = Application(
            app_id="APP-SCOPE",
            name="权限范围应用",
            allowed_roles=["开发"],
            deploy_roles=["运维"],
            control_roles=[],
        )
        self.db.add(app)
        self.db.commit()
        self.db.refresh(app)

        developer = SimpleNamespace(id=1, username="dev", name="Dev", role="开发", permission_overrides="")
        operator = SimpleNamespace(id=2, username="ops", name="Ops", role="运维", permission_overrides="")
        admin = SimpleNamespace(id=3, username="admin", name="Admin", role="管理员", permission_overrides="")

        self.assertTrue(_application_visible_to_user(app, developer))
        self.assertFalse(_application_visible_to_user(app, operator))
        self.assertTrue(_application_visible_to_user(app, admin))

        self.assertFalse(_application_action_allowed(app, developer, "deploy"))
        self.assertTrue(_application_action_allowed(app, developer, "control"))
        self.assertTrue(_application_action_allowed(app, operator, "deploy"))
        self.assertTrue(_application_action_allowed(app, admin, "deploy"))

    def test_application_list_respects_visible_scope_for_app_managers(self):
        """列表接口按应用可见范围过滤，管理员保留全局可见能力。"""
        self.db.add_all([
            Application(app_id="APP-DEV", name="开发应用", allowed_roles=["开发"]),
            Application(app_id="APP-OPS", name="运维应用", allowed_roles=["运维"]),
            Application(app_id="APP-OPEN", name="公开应用", allowed_roles=[]),
        ])
        self.db.commit()

        developer = SimpleNamespace(id=1, username="dev", name="Dev", role="开发", permission_overrides="")
        admin = SimpleNamespace(id=2, username="admin", name="Admin", role="管理员", permission_overrides="")

        developer_rows = list_applications(limit=10, offset=0, db=self.db, current_user=developer)
        admin_rows = list_applications(limit=10, offset=0, db=self.db, current_user=admin)

        self.assertEqual(["APP-OPEN", "APP-DEV"], [item["app_id"] for item in developer_rows])
        self.assertEqual(["APP-OPEN", "APP-OPS", "APP-DEV"], [item["app_id"] for item in admin_rows])


if __name__ == "__main__":
    unittest.main()
