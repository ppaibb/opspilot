"""高危操作确认票据（risk ticket）契约测试。

验证阶段1安全边界：require_risk_ack 恢复强制校验后，
1. challenge → X-Risk-Ticket → 业务路由 的合法全流程；
2. 缺票 / 重放 / 过期 / 错动作 / 他人票据 一律 403；
3. 短键（前端别名）签发的票据能在全键路由上消费；
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

FULL_PERM = "action.assets.clusters.manage"
FULL_ACTION = "delete"


def challenge(body: dict, db, user=ADMIN):
    return asyncio.run(auth.risk_challenge(auth.RiskChallengeRequest(**body), current_user=user, db=db))


def guard_for(permission_key: str, action_id: str):
    return security.require_risk_ack(permission_key, action_id)


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
        self.assertTrue(row.short_key)
        self.assertEqual(row.permission_key, "clusters")
        self.assertEqual(row.action_id, "delete")

    def test_short_key_unregistered_action_still_issuable(self):
        # 未注册风险等级的动作也必须能拿票，否则对应路由会被永久 403。
        # logs（查看日志，只读）挂了 require_risk_ack 链但未在 ACTION_RISK_LEVELS
        # 注册等级。（patch_labels 2026-10-08 已登记 medium，不能再当未注册用例。）
        result = challenge({"action_key": "clusters:logs"}, self.db)
        self.assertEqual(result["risk_level"], "low")

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

    def test_full_key_ticket_consumed_marks_row(self):
        ticket = self._fresh_ticket({"permission_key": FULL_PERM, "action_id": FULL_ACTION})
        call_guard(guard_for(FULL_PERM, FULL_ACTION), ticket)
        row = self.db.query(RiskAckTicket).filter(RiskAckTicket.id == ticket).one()
        self.assertIsNotNone(row.consumed_at)


if __name__ == "__main__":
    unittest.main()
