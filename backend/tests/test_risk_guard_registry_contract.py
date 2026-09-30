"""高危动作守卫注册表契约测试（权威动作键对照表）。

前后端按同一张对照表改键：后端守卫 (permission_key, action_id) 必须与前端
短键逐字符一致，否则票据换不出来 / 消费不匹配，路由被永久 403 或守卫形同
虚设。本测试把对照表钉死：

1. 注册表逐行断言：路由依赖里存在带对应 (权限键, 动作) 元数据的
   require_risk_ack 守卫（元数据由 require_risk_ack 标注在守卫函数上）；
2. 前端短键 → 全键对 反解析：resolve_action_key_pairs(短键) 必须能换出
   注册表里的 (权限键, 动作)——这正是前端 risk-challenge 走的路径；
3. clusters 全量动作集合核对（既有守卫，只核对不改）；
4. users status 路由不得再挂静态 disable 守卫（改为函数体内按目标状态
   动态校验：禁用要票、启用不要）。
"""
from __future__ import annotations

import importlib
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.rbac import PERMISSION_DEFAULTS
from app.core.security import resolve_action_key_pairs
from app.db.database import Base
from app.models.risk_ticket import RiskAckTicket
from app.models.user import User
from app.services.application_time import now_dt

from app.api import settings as settings_api

# ── 权威动作键对照表（与前端 agent 共用；不一致即测试红）─────────────────────
# (路由模块, HTTP 方法, 路由 path（相对 router prefix）, 前端短键, 权限键, 动作, 状态)
GUARD_REGISTRY: list[tuple[str, str, str, str, str, str, str]] = [
    ("app.api.assets", "DELETE", "/{asset_id}", "hosts:delete",
     "action.assets.hosts.manage", "delete", "新增"),
    ("app.api.assets", "POST", "/batch-delete", "hosts:batch_delete",
     "action.assets.hosts.manage", "batch_delete", "既有"),
    ("app.api.certificates", "GET", "/{cert_id}/download", "certificates:download",
     "action.assets.certificates.download", "download", "既有"),
    ("app.api.certificates", "POST", "/{cert_id}/revoke", "certificates:revoke",
     "action.assets.certificates.manage", "revoke", "既有"),
    ("app.api.certificates", "DELETE", "/{cert_id}", "certificates:delete",
     "action.assets.certificates.manage", "delete", "既有"),
    ("app.api.certificates", "POST", "/batch-delete", "certificates:delete",
     "action.assets.certificates.manage", "delete", "既有"),
    ("app.api.credentials", "DELETE", "/{cred_id}", "credentials:delete",
     "action.assets.credentials.manage", "delete", "新增"),
    ("app.api.credentials", "POST", "/batch-delete", "credentials:delete",
     "action.assets.credentials.manage", "delete", "新增"),
    ("app.api.domains", "DELETE", "/{domain_id}", "domains:delete",
     "action.assets.domains.manage", "delete", "既有"),
    ("app.api.domains", "POST", "/batch-delete", "domains:delete",
     "action.assets.domains.manage", "delete", "新增"),
    ("app.api.domains", "DELETE", "/{domain_id}/records/{record_id}", "domains:delete",
     "action.assets.domains.manage", "delete", "新增"),
    ("app.api.vms", "POST", "/{vm_id}/power", "vms:poweroff",
     "action.assets.vms.power", "poweroff", "既有"),
    ("app.api.vms", "DELETE", "/{vm_id}", "vms:delete",
     "action.assets.vms.manage", "delete", "既有"),
    ("app.api.vms", "POST", "/batch-delete", "vms:delete",
     "action.assets.vms.manage", "delete", "新增"),
    ("app.api.wireguard", "DELETE", "/networks/{network_id}", "wireguard:delete",
     "action.wireguard.manage", "delete", "新增"),
    ("app.api.wireguard", "DELETE", "/peers/{peer_id}", "wireguard:delete",
     "action.wireguard.manage", "delete", "新增"),
    ("app.api.finance", "POST", "/accounts", "finance:create",
     "action.finance.accounts.manage", "create", "新增"),
    ("app.api.finance", "DELETE", "/accounts/{account_id}", "finance:delete",
     "action.finance.accounts.manage", "delete", "新增"),
    ("app.api.applications", "DELETE", "/{app_id}", "apps:delete",
     "action.apps.manage", "delete", "新增"),
    ("app.api.applications", "POST", "/{app_id}/configs/{config_id}/rollback", "config:rollback",
     "action.apps.config.manage", "rollback", "新增"),
    ("app.api.settings", "POST", "/users/{user_id}/reset-password", "users:reset-password",
     "action.settings.users.manage", "reset-password", "新增"),
]

# clusters 既有守卫的合法动作全集（action.assets.clusters.manage）。
CLUSTERS_EXPECTED_ACTIONS = {
    "delete", "edit", "apply", "drain", "restart", "rollout-undo", "pause",
    "trigger_cronjob", "create_namespace", "patch_labels", "patch_taints",
    "cordon", "uncordon", "set-image", "suspend_cronjob", "resize_pvc",
}


def _risk_guards(route: APIRoute) -> set[tuple[str, str]]:
    """收集路由上全部 require_risk_ack 守卫的 (权限键, 动作) 元数据。

    同时覆盖装饰器 dependencies 与端点函数参数两种挂法。
    """
    calls = []
    for dep in getattr(route, "dependencies", None) or []:
        call = getattr(dep, "call", None)
        if call is not None:
            calls.append(call)
    for sub in getattr(route.dependant, "dependencies", None) or []:
        calls.append(sub.call)
    return {
        (call.risk_permission_key, call.risk_action_id)
        for call in calls
        if getattr(call, "risk_permission_key", None) is not None
    }


def _find_route(module_name: str, method: str, path: str) -> APIRoute:
    router = importlib.import_module(module_name).router
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"路由未找到: {module_name} {method} {path}")


class GuardRegistryContract(unittest.TestCase):
    """对照表逐行核对：守卫存在、键真实、短键可换票。"""

    def test_registry_route_has_exact_guard(self):
        for module_name, method, path, short_key, perm, action, status in GUARD_REGISTRY:
            with self.subTest(route=f"{method} {path}", status=status):
                route = _find_route(module_name, method, path)
                guards = _risk_guards(route)
                self.assertIn(
                    (perm, action), guards,
                    f"{method} {path} 缺少守卫 require_risk_ack({perm!r}, {action!r})，"
                    f"实际: {sorted(guards)}",
                )

    def test_registry_permission_keys_are_real(self):
        for _, _, _, _, perm, _, _ in GUARD_REGISTRY:
            with self.subTest(perm=perm):
                self.assertIn(perm, PERMISSION_DEFAULTS)

    def test_frontend_short_key_resolves_to_registered_pair(self):
        """前端短键走 risk-challenge 的解析路径必须换出对照表的全键对。"""
        for _, _, _, short_key, perm, action, _ in GUARD_REGISTRY:
            with self.subTest(short_key=short_key):
                pairs = resolve_action_key_pairs(short_key)
                self.assertIn(
                    (perm, action), pairs,
                    f"短键 {short_key!r} 换不出 {(perm, action)}，前后端键不一致",
                )

    def test_clusters_actions_cover_expected_set(self):
        """clusters 既有守卫只核对不改：动作全集与对照表一致。"""
        from app.api import clusters as clusters_api

        actions: set[str] = set()
        for route in clusters_api.router.routes:
            if not isinstance(route, APIRoute):
                continue
            for perm, action in _risk_guards(route):
                self.assertEqual(perm, "action.assets.clusters.manage")
                actions.add(action)
        self.assertEqual(actions, CLUSTERS_EXPECTED_ACTIONS)


class UserStatusDynamicTicketContract(unittest.TestCase):
    """users status 路由：禁用要 disable 票，启用不要（动态校验）。"""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(
            bind=engine,
            tables=[User.__table__, RiskAckTicket.__table__],
        )
        self.db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
        self.target = User(
            username="alice", name="爱丽丝", email="alice@example.com",
            hashed_password="fake", role="运维", status="active",
        )
        self.admin = User(
            username="boss", name="管理员", email="boss@example.com",
            hashed_password="fake", role="管理员", status="active",
        )
        self.db.add_all([self.target, self.admin])
        self.db.commit()
        self.db.refresh(self.target)
        self.db.refresh(self.admin)

    def tearDown(self):
        self.db.close()

    def _request(self, ticket: str | None = None):
        headers = {"x-risk-ticket": ticket} if ticket else {}
        return SimpleNamespace(headers=headers)

    def _grant_ticket(self) -> str:
        ticket = RiskAckTicket(
            id="disable-ack-1",
            user_id=self.admin.id,
            permission_key="action.settings.users.manage",
            action_id="disable",
            short_key=False,
            created_at=now_dt().strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(now_dt() + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.db.add(ticket)
        self.db.commit()
        return ticket.id

    def test_disable_without_ticket_is_403(self):
        with self.assertRaises(HTTPException) as ctx:
            settings_api.update_user_status(
                self.target.id,
                settings_api.UserStatusUpdate(status="disabled"),
                self._request(),
                db=self.db,
                current_user=self.admin,
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.db.refresh(self.target)
        self.assertEqual(self.target.status, "active")

    def test_disable_with_ticket_succeeds_and_consumes_ticket(self):
        ticket_id = self._grant_ticket()
        result = settings_api.update_user_status(
            self.target.id,
            settings_api.UserStatusUpdate(status="disabled"),
            self._request(ticket_id),
            db=self.db,
            current_user=self.admin,
        )
        self.assertEqual(result["status"], "disabled")
        self.db.refresh(self.target)
        self.assertEqual(self.target.status, "disabled")
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == ticket_id).one()
        self.assertIsNotNone(row.consumed_at)

    def test_enable_without_ticket_succeeds(self):
        """启用是恢复性操作：无票据直接成功（旧版静态守卫会索要 disable 票）。"""
        self.target.status = "disabled"
        self.db.commit()
        result = settings_api.update_user_status(
            self.target.id,
            settings_api.UserStatusUpdate(status="active"),
            self._request(),
            db=self.db,
            current_user=self.admin,
        )
        self.assertEqual(result["status"], "active")

    def test_status_route_has_no_static_disable_guard(self):
        """路由依赖上不得再有静态 require_risk_ack（动态校验的契约前提）。"""
        route = _find_route("app.api.settings", "PUT", "/users/{user_id}/status")
        self.assertEqual(_risk_guards(route), set())


if __name__ == "__main__":
    unittest.main()
