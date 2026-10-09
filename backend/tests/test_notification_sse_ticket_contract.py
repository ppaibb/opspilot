"""通知 SSE 一次性票据契约测试。

验证阶段2 SSE 鉴权改造：
1. active 用户可领 60 秒票据（scope=notifications-sse），禁用用户领票 403；
2. 带合法票据可建立 SSE（读到首帧即算通过），无票 / 过期票 / 伪 scope 票 /
   伪造用户票一律 401；
3. 票据与长期 access token 双向隔离：access token 不能当票据用；
4. 领票后用户被改密（token_version 递增）票据立即失效。

不使用 TestClient：直接调用路由函数（与 test_risk_ticket_contract.py 同风格）。
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.database import Base
from app.models.user import User

from app.api import notifications

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine, tables=[User.__table__])
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

ALICE = "alice"


def make_token(claims: dict, ttl_seconds: int = 60) -> str:
    """按指定 claims 手工签发一个 JWT（含过期时间）。"""
    payload = {**claims, "exp": datetime.utcnow() + timedelta(seconds=ttl_seconds)}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


class SseTicketIssuerContract(unittest.TestCase):
    """票据签发端：只有 active 用户能领，票据 claims 正确。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(User).delete()
        self.db.commit()
        self.user = User(username=ALICE, name="爱丽丝", role="运维", status="active", hashed_password="x")
        self.jailed = User(username="jailed", name="被禁用户", role="运维", status="disabled", hashed_password="x")
        self.db.add_all([self.user, self.jailed])
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()

    def test_active_user_gets_60s_ticket(self):
        result = notifications.issue_stream_ticket(db=self.db, current_user=self.user)
        self.assertEqual(result["expires_in"], 60)
        payload = jwt.decode(result["ticket"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        self.assertEqual(payload["sub"], ALICE)
        self.assertEqual(payload["scope"], "notifications-sse")
        self.assertEqual(payload["ver"], self.user.token_version or 0)

    def test_disabled_user_cannot_get_ticket(self):
        with self.assertRaises(HTTPException) as ctx:
            notifications.issue_stream_ticket(db=self.db, current_user=self.jailed)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_ticket_route_registered(self):
        routes = {(route.path, tuple(sorted(getattr(route, "methods", []) or []))) for route in notifications.router.routes}
        self.assertIn(("/stream/ticket", ("POST",)), routes)
        self.assertIn(("/stream", ("GET",)), routes)


class SseTicketVerificationContract(unittest.TestCase):
    """票据校验：伪票 / 过期票 / 伪 scope / 陌生用户 / 改密后旧票 全部 401。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(User).delete()
        self.db.commit()
        self.user = User(username=ALICE, name="爱丽丝", role="运维", status="active", hashed_password="x")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()

    def _valid_ticket(self) -> str:
        return make_token({"sub": ALICE, "scope": "notifications-sse", "ver": 0})

    def test_valid_ticket_passes(self):
        user = notifications._verify_sse_ticket(self._valid_ticket(), self.db)
        self.assertEqual(user.id, self.user.id)

    def test_expired_ticket_is_401(self):
        expired = make_token({"sub": ALICE, "scope": "notifications-sse", "ver": 0}, ttl_seconds=-10)
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(expired, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_scope_ticket_is_401(self):
        forged = make_token({"sub": ALICE, "scope": "chat", "ver": 0})
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(forged, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_access_token_without_scope_is_401(self):
        """长期 access token 不能当 SSE 票据用（双向隔离）。"""
        access_like = make_token({"sub": ALICE, "role": "运维"})
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(access_like, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_unknown_user_ticket_is_401(self):
        ghost = make_token({"sub": "ghost", "scope": "notifications-sse", "ver": 0})
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(ghost, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_garbage_ticket_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket("not-a-jwt", self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_ticket_dies_after_password_reset(self):
        """领票后用户 token_version 递增（改密/禁用），票据立即失效。"""
        ticket = self._valid_ticket()
        self.user.token_version = (self.user.token_version or 0) + 1
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(ticket, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_disabled_user_ticket_is_401(self):
        self.user.status = "disabled"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            notifications._verify_sse_ticket(self._valid_ticket(), self.db)
        self.assertEqual(ctx.exception.status_code, 401)


class SseStreamEndpointContract(unittest.TestCase):
    """stream 端点：合法票据读到首帧；无票 / 伪票 401。

    stream 端点内部用 app.db.database.SessionLocal 开短会话解码票据，
    这里 patch 成测试库的 sessionmaker，避免依赖进程级真实引擎。
    """

    def setUp(self):
        self.db = TestingSession()
        self.db.query(User).delete()
        self.db.commit()
        self.user = User(username=ALICE, name="爱丽丝", role="运维", status="active", hashed_password="x")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)
        patcher = patch("app.db.database.SessionLocal", TestingSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        notifications._sse_subscribers.pop(self.user.id, None)
        self.db.close()

    def test_stream_with_valid_ticket_yields_first_frame(self):
        ticket = make_token({"sub": ALICE, "scope": "notifications-sse", "ver": 0})

        async def scenario():
            response = await notifications.notification_stream(SimpleNamespace(), ticket=ticket)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.media_type, "text/event-stream")
            first_chunk = await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
            return first_chunk

        first_chunk = asyncio.run(scenario())
        self.assertEqual(first_chunk, ": connected\n\n")
        # aclose 触发生成器 finally，订阅应已清理。
        self.assertNotIn(self.user.id, notifications._sse_subscribers)

    def test_stream_without_ticket_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(notifications.notification_stream(SimpleNamespace(), ticket=""))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_stream_with_access_token_is_401(self):
        access_like = make_token({"sub": ALICE, "role": "运维"})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(notifications.notification_stream(SimpleNamespace(), ticket=access_like))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_legacy_token_query_param_is_gone(self):
        """stream 端点不再接受 token 参数，契约只认 ticket。"""
        stream_route = next(
            route for route in notifications.router.routes
            if getattr(route, "path", "") == "/stream"
        )
        param_names = {field.name for field in stream_route.dependant.query_params}
        self.assertIn("ticket", param_names)
        self.assertNotIn("token", param_names)


if __name__ == "__main__":
    unittest.main()
