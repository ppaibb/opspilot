"""JWT 会话撤销（token_version）契约测试。

验证阶段2会话安全：
1. 正常登录签发的 token 携带 ver claim，且能通过 get_current_user 校验；
2. token_version 递增后（重置密码 / 禁用账号），旧 token 立即 401；
3. 没有 ver claim 的历史 token 视为版本 0，对老用户保持兼容；
4. ver claim 与当前版本不等的任何伪造 token 一律 401。

不使用 TestClient：直接调用路由函数（与 test_risk_ticket_contract.py 同风格）。
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.passwords import hash_password
from app.core.security import _check_token_version, get_current_user
from app.db.database import Base
from app.models.risk_ticket import RiskAckTicket
from app.models.user import User

from app.api import auth
from app.api import settings as settings_api

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine, tables=[User.__table__, RiskAckTicket.__table__])
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

PASSWORD = "original-pass-123"


def make_request():
    """run_login_flow / update_user_status 只读 headers/client，用 SimpleNamespace 模拟。"""
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


def login(db, username: str, password: str = PASSWORD) -> str:
    result = auth.run_login_flow(
        auth.LoginRequest(username=username, password=password),
        request=make_request(),
        db=db,
    )
    return result["access_token"]


class TokenRevocationContract(unittest.TestCase):
    """token_version 递增 → 旧 token 立即失效。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(User).delete()
        self.db.commit()
        self.user = User(
            username="alice",
            name="爱丽丝",
            role="运维",
            status="active",
            hashed_password=hash_password(PASSWORD),
        )
        self.admin = User(
            username="boss",
            name="管理员",
            role="管理员",
            status="active",
            hashed_password=hash_password(PASSWORD),
        )
        self.db.add_all([self.user, self.admin])
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()

    def test_login_token_carries_version_and_passes_auth(self):
        """正常登录 token 有效，且 ver claim 与用户当前版本一致。"""
        token = login(self.db, "alice")
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        self.assertEqual(payload.get("ver"), 0)
        # get_current_user 全链路校验通过并返回用户。
        current = get_current_user(token=token, db=self.db)
        self.assertEqual(current.id, self.user.id)

    def test_version_bump_invalidates_old_token(self):
        """token_version 递增后，旧 ver 的 token 直接 401。"""
        token = login(self.db, "alice")
        self.user.token_version = (self.user.token_version or 0) + 1
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token=token, db=self.db)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("登录状态已失效", ctx.exception.detail)

    def test_reset_password_invalidates_old_token(self):
        """管理员重置密码后，用户手里的旧 token 立即失效。"""
        token = login(self.db, "alice")
        settings_api.reset_user_password(
            self.user.id,
            settings_api.UserPasswordResetRequest(new_password="brand-new-pass-456"),
            db=self.db,
        )
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token=token, db=self.db)
        self.assertEqual(ctx.exception.status_code, 401)
        # 新密码能正常登录（版本号随新 token 签发）。
        new_token = login(self.db, "alice", "brand-new-pass-456")
        self.assertEqual(get_current_user(token=new_token, db=self.db).id, self.user.id)

    def _grant_disable_ticket(self) -> str:
        """给管理员签一张 users:disable 票（禁用是高危动作，需二次确认）。"""
        from app.models.risk_ticket import risk_ticket_now
        from datetime import datetime, timedelta

        ticket = RiskAckTicket(
            id="disable-ticket-1",
            user_id=self.admin.id,
            permission_key="action.settings.users.manage",
            action_id="disable",
            short_key=False,
            created_at=risk_ticket_now(),
            expires_at=(datetime.now() + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.db.add(ticket)
        self.db.commit()
        return ticket.id

    def test_disable_user_invalidates_old_token(self):
        """禁用账号（带 disable 票）后旧 token 401（既有禁用逻辑 + 版本递增双保险）。"""
        token = login(self.db, "alice")
        request = make_request()
        request.headers = {"x-risk-ticket": self._grant_disable_ticket()}
        settings_api.update_user_status(
            self.user.id,
            settings_api.UserStatusUpdate(status="disabled"),
            request,
            db=self.db,
            current_user=self.admin,
        )
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token=token, db=self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_enable_does_not_bump_version(self):
        """启用账号不递增版本号（禁用已经把版本顶上去，无需二次递增）。"""
        version_before = self.user.token_version or 0
        # 启用是恢复性操作：不需要 disable 票（headers 里没有任何票据）。
        settings_api.update_user_status(
            self.user.id,
            settings_api.UserStatusUpdate(status="active"),
            make_request(),
            db=self.db,
            current_user=self.admin,
        )
        self.db.refresh(self.user)
        self.assertEqual(self.user.token_version or 0, version_before)

    def test_legacy_token_without_ver_claim_still_works(self):
        """历史 token 没有 ver claim，按版本 0 处理，对老用户保持兼容。"""
        legacy = jwt.encode(
            {"sub": "alice", "role": "运维"},
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        current = get_current_user(token=legacy, db=self.db)
        self.assertEqual(current.id, self.user.id)

    def test_forged_ver_claim_is_rejected(self):
        """伪造更高版本的 ver claim 一律 401；脏值同样不放行。"""
        for fake_ver in ("1", 1, "abc", "0.0"):
            with self.subTest(fake_ver=fake_ver):
                forged = jwt.encode(
                    {"sub": "alice", "role": "运维", "ver": fake_ver},
                    settings.SECRET_KEY,
                    algorithm=settings.ALGORITHM,
                )
                with self.assertRaises(HTTPException) as ctx:
                    get_current_user(token=forged, db=self.db)
                self.assertEqual(ctx.exception.status_code, 401)

    def test_sse_ticket_cannot_be_used_as_access_token(self):
        """反向隔离：带 notifications-sse scope 的票据不能当 Bearer token 用。"""
        from app.core.security import SSE_TICKET_SCOPE

        ticket = jwt.encode(
            {"sub": "alice", "scope": SSE_TICKET_SCOPE, "ver": 0},
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token=ticket, db=self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_check_token_version_is_reusable_helper(self):
        """_check_token_version 是可复用的独立校验点（供 SSE 票据等路径复用）。"""
        _check_token_version({"ver": 0}, self.user)  # 不抛即通过
        with self.assertRaises(HTTPException) as ctx:
            _check_token_version({"ver": 3}, self.user)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_login_rejects_disabled_account(self):
        """禁用账号无法重新登录换取新 token（既有逻辑保持）。"""
        self.user.status = "disabled"
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            login(self.db, "alice")
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
