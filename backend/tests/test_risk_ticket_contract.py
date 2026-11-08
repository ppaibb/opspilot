"""高危操作确认票据（risk ticket）契约测试。

验证阶段1安全边界：require_risk_ack 恢复强制校验后，
1. challenge → X-Risk-Ticket → 业务路由 的合法全流程；
2. 缺票 / 重放 / 过期 / 错动作 / 他人票据 一律 403；
3. 短键只在唯一解析时签发，并立即规范化为全权限键；
4. 票据签发的参数校验与过期清理。

不使用 TestClient：直接调用路由函数与依赖闭包（等价且更轻，
参考 test_finance_api_e2e.py 的说明）。
"""
import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.risk_ticket import RiskAckTicket
from app.models.system_config import SystemConfig  # noqa: F401 保持与其他套件一致的建表集合
from app.models.user import User

from app.api import auth
from app.core import security

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(
    bind=engine,
    tables=[
        SystemConfig.__table__,
        User.__table__,
        RiskAckTicket.__table__,
    ],
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

ADMIN = User(id=1, username="admin", role="管理员", status="active")
COLLEAGUE = User(id=2, username="ops2", role="运维", status="active")
READ_ONLY = User(id=3, username="viewer", role="只读", status="active")

FULL_PERM = "action.assets.clusters.manage"
FULL_ACTION = "delete"


def challenge(body: dict, db, user=ADMIN):
    return asyncio.run(auth.risk_challenge(auth.RiskChallengeRequest(**body), current_user=user, db=db))


def guard_for(permission_key: str, action_id: str, resource_params: tuple[str, ...] = ()):
    return security.require_risk_ack(permission_key, action_id, resource_params=resource_params)


def call_guard(guard, ticket: str | None, user=ADMIN):
    request = SimpleNamespace(headers={"x-risk-ticket": ticket} if ticket else {})
    return guard(request=request, current_user=user, db=TestingSession())


class RiskChallengeIssuerContract(unittest.TestCase):
    """challenge 签发端的参数校验、等级计算与过期清理。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(RiskAckTicket).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_full_key_challenge_returns_high_ticket(self):
        result = challenge({"permission_key": FULL_PERM, "action_id": FULL_ACTION}, self.db)
        self.assertIn("ticket", result)
        self.assertIn("expires_at", result)
        self.assertEqual(result["risk_level"], "high")  # clusters delete 注册为 high
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == result["ticket"]).one()
        self.assertFalse(row.short_key)
        self.assertEqual(row.permission_key, FULL_PERM)
        self.assertEqual(row.action_id, FULL_ACTION)
        self.assertIsNone(row.consumed_at)

    def test_short_key_challenge_resolves_and_takes_max_level(self):
        result = challenge({"action_key": "clusters:delete"}, self.db)
        self.assertEqual(result["risk_level"], "high")
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == result["ticket"]).one()
        self.assertFalse(row.short_key)
        self.assertEqual(row.permission_key, FULL_PERM)
        self.assertEqual(row.action_id, "delete")

    def test_ambiguous_short_key_is_rejected(self):
        """assets:delete 同时命中证书/虚机/集群/域名，不能签发泛用票据。"""
        with self.assertRaises(HTTPException) as caught:
            challenge({"action_key": "assets:delete"}, self.db)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("不唯一", caught.exception.detail)

    def test_full_key_unregistered_action_still_issuable(self):
        # 未注册风险等级的动作仍可使用完整权限键拿票；泛化短键可能同时
        # 命中多个权限域，v2 会拒绝歧义输入而不是签发跨路由票据。
        result = challenge({"permission_key": FULL_PERM, "action_id": "logs"}, self.db)
        self.assertEqual(result["risk_level"], "low")

    def test_read_only_user_cannot_issue_full_permission_ticket(self):
        """风险确认不能成为绕过业务 RBAC 的票据铸造接口。"""
        with self.assertRaises(HTTPException) as caught:
            challenge(
                {
                    "permission_key": "action.settings.users.manage",
                    "action_id": "delete",
                },
                self.db,
                user=READ_ONLY,
            )
        self.assertEqual(caught.exception.status_code, 403)

    def test_read_only_user_cannot_issue_short_permission_ticket(self):
        with self.assertRaises(HTTPException) as caught:
            challenge({"action_key": "users:delete"}, self.db, user=READ_ONLY)
        self.assertEqual(caught.exception.status_code, 403)

    def test_short_key_second_round_registered_cluster_actions(self):
        """2026-10-08 二轮补登记的四个动作必须落在注册等级上。

        patch_labels / scale / create_namespace / exec_session 此前未登记 →
        回落 low → 前端不弹确认、审计风险列不可信。
        """
        self.assertEqual(challenge({"action_key": "clusters:patch_labels"}, self.db)["risk_level"], "medium")
        self.assertEqual(challenge({"action_key": "clusters:scale"}, self.db)["risk_level"], "medium")
        self.assertEqual(challenge({"action_key": "clusters:create_namespace"}, self.db)["risk_level"], "medium")
        self.assertEqual(challenge({"action_key": "clusters:exec_session"}, self.db)["risk_level"], "medium")

    def test_short_key_registered_cluster_actions_take_registered_level(self):
        """集群运维动作必须落在 ACTION_RISK_LEVELS 注册的等级上。

        2026-10-08 巡检前 cordon/drain/set-image 等整族动作未注册 → 回落 low →
        前端不弹确认、审计记 low，一次误点就把生产节点标记不可调度 / 驱逐全部 Pod。
        """
        self.assertEqual(challenge({"action_key": "clusters:cordon"}, self.db)["risk_level"], "high")
        self.assertEqual(challenge({"action_key": "clusters:drain"}, self.db)["risk_level"], "high")
        self.assertEqual(challenge({"action_key": "clusters:uncordon"}, self.db)["risk_level"], "medium")
        self.assertEqual(challenge({"action_key": "clusters:set-image"}, self.db)["risk_level"], "high")
        self.assertEqual(challenge({"action_key": "clusters:rollout-undo"}, self.db)["risk_level"], "high")

    def test_challenge_without_any_key_is_422(self):
        with self.assertRaises(HTTPException) as ctx:
            challenge({}, self.db)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_challenge_unknown_permission_key_is_422(self):
        with self.assertRaises(HTTPException) as ctx:
            challenge({"permission_key": "action.nope.nope.manage", "action_id": "delete"}, self.db)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_challenge_unknown_short_key_is_422(self):
        with self.assertRaises(HTTPException) as ctx:
            challenge({"action_key": "nonexist:delete"}, self.db)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_challenge_cleans_expired_tickets(self):
        stale = RiskAckTicket(
            id="stale-ticket", user_id=1, permission_key=FULL_PERM, action_id=FULL_ACTION,
            short_key=False, created_at="2000-01-01 00:00:00", expires_at="2000-01-01 00:00:00",
        )
        self.db.add(stale)
        self.db.commit()
        challenge({"permission_key": FULL_PERM, "action_id": FULL_ACTION}, self.db)
        self.assertIsNone(
            self.db.query(RiskAckTicket).filter(RiskAckTicket.id == "stale-ticket").first()
        )

    def test_risk_challenge_route_is_registered(self):
        paths = {route.path for route in auth.router.routes}
        self.assertIn("/risk-challenge", paths)


class RiskAckGuardContract(unittest.TestCase):
    """require_risk_ack 消费端：一次性、限时、限人、限动作。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(RiskAckTicket).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _fresh_ticket(self, body: dict) -> str:
        return challenge(body, self.db)["ticket"]

    def test_legal_flow_full_key_passes(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        user = call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        self.assertEqual(user.id, ADMIN.id)

    def test_missing_ticket_is_403(self):
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, FULL_ACTION), None)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("缺少风险确认票据", ctx.exception.detail)

    def test_replay_is_403(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_expired_ticket_is_403(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == ticket).one()
        row.expires_at = "2000-01-01 00:00:00"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_wrong_action_is_403(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, "edit"), ticket)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_wrong_permission_is_403(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for("action.assets.hosts.manage", FULL_ACTION), ticket)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_other_users_ticket_is_403(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket, user=COLLEAGUE)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_short_key_ticket_matches_full_key_route(self):
        ticket = self._fresh_ticket({"action_key": "clusters:delete"})
        user = call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        self.assertEqual(user.id, ADMIN.id)

    def test_short_key_ticket_wrong_action_rejected(self):
        ticket = self._fresh_ticket({"action_key": "clusters:delete"})
        with self.assertRaises(HTTPException) as ctx:
            call_guard(guard_for(FULL_PERM, "edit"), ticket)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_permission_revoked_after_issue_cannot_consume_ticket(self):
        """签票后撤权必须立即生效，票据本身不能冻结旧权限。"""
        ticket = self._fresh_ticket(
            {
                "permission_key": "action.settings.users.manage",
                "action_id": "delete",
            }
        )
        original_role = ADMIN.role
        ADMIN.role = "只读"
        try:
            with self.assertRaises(HTTPException) as caught:
                call_guard(
                    guard_for("action.settings.users.manage", "delete"),
                    ticket,
                    user=ADMIN,
                )
            self.assertEqual(caught.exception.status_code, 403)
            row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == ticket).one()
            self.assertIsNone(row.consumed_at)
        finally:
            ADMIN.role = original_role

    def test_full_key_ticket_consumed_marks_row(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        # 消费发生在独立请求 Session；清掉当前测试 Session 的 identity map，
        # 防止 SQLite 全套件运行时读到签发阶段缓存的 consumed_at=None。
        self.db.expire_all()
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == ticket).one()
        self.assertIsNotNone(row.consumed_at)

    def test_ticket_cannot_cross_resource_and_failed_match_does_not_consume(self):
        ticket = self._fresh_ticket({
            "permission_key": FULL_PERM,
            "action_id": FULL_ACTION,
            "resource_id": "cluster-a",
        })
        request = SimpleNamespace(headers={"x-risk-ticket": ticket})
        with self.assertRaises(HTTPException) as caught:
            security.consume_risk_ticket(
                request,
                TestingSession(),
                permission_key=FULL_PERM,
                action_id=FULL_ACTION,
                user=ADMIN,
                resource_id="cluster-b",
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.db.expire_all()
        self.assertIsNone(self.db.get(RiskAckTicket, ticket).consumed_at)

    def test_ticket_cannot_cross_method_route_or_request_digest(self):
        ticket = challenge({
            "permission_key": FULL_PERM,
            "action_id": FULL_ACTION,
            "resource_id": "cluster-a",
            "http_method": "DELETE",
            "route_template": "/api/clusters/{cluster_id}",
            "request_digest": "sha256:expected",
        }, self.db)["ticket"]
        request = SimpleNamespace(headers={"x-risk-ticket": ticket})
        with self.assertRaises(HTTPException):
            security.consume_risk_ticket(
                request,
                TestingSession(),
                permission_key=FULL_PERM,
                action_id=FULL_ACTION,
                user=ADMIN,
                resource_id="cluster-a",
                http_method="PATCH",
                route_template="/api/clusters/{cluster_id}",
                request_digest="sha256:expected",
            )
        self.db.expire_all()
        self.assertIsNone(self.db.get(RiskAckTicket, ticket).consumed_at)

    def test_exact_v2_ticket_passes_once(self):
        ticket = challenge({
            "permission_key": FULL_PERM,
            "action_id": FULL_ACTION,
            "resource_id": "cluster-a",
            "http_method": "DELETE",
            "route_template": "/api/clusters/{cluster_id}",
            "request_digest": "sha256:expected",
        }, self.db)["ticket"]
        request = SimpleNamespace(headers={"x-risk-ticket": ticket})
        security.consume_risk_ticket(
            request,
            TestingSession(),
            permission_key=FULL_PERM,
            action_id=FULL_ACTION,
            user=ADMIN,
            resource_id="cluster-a",
            http_method="DELETE",
            route_template="/api/clusters/{cluster_id}",
            request_digest="sha256:expected",
        )
        with self.assertRaises(HTTPException):
            security.consume_risk_ticket(
                request,
                TestingSession(),
                permission_key=FULL_PERM,
                action_id=FULL_ACTION,
                user=ADMIN,
                resource_id="cluster-a",
                http_method="DELETE",
                route_template="/api/clusters/{cluster_id}",
                request_digest="sha256:expected",
            )

    def test_route_guard_derives_method_template_and_path_resource(self):
        """路由依赖必须使用真实请求元数据，不能信任取票请求自行声明的目标。"""
        route_path = "/api/db/tickets/{ticket_id}/execute"
        ticket = challenge({
            "permission_key": FULL_PERM,
            "action_id": FULL_ACTION,
            "resource_id": "ticket_id=42",
            "http_method": "POST",
            "route_template": route_path,
        }, self.db)["ticket"]
        request = SimpleNamespace(
            headers={"x-risk-ticket": ticket},
            method="POST",
            path_params={"ticket_id": "42"},
            scope={"route": SimpleNamespace(path=route_path)},
        )
        user = guard_for(FULL_PERM, FULL_ACTION, ("ticket_id",))(
            request=request,
            current_user=ADMIN,
            db=TestingSession(),
        )
        self.assertEqual(user.id, ADMIN.id)


if __name__ == "__main__":
    unittest.main()
