"""Access/refresh token 与登录来源安全契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.database import Base
from app.models.user import User


@pytest.fixture()
def db_session():
    """为 refresh 生命周期创建隔离 SQLite 会话。"""
    from app.models.refresh_token import RefreshToken  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def active_user(db_session):
    user = User(
        username="alice",
        name="Alice",
        email="alice@example.com",
        role="user",
        status="active",
        token_version=0,
        hashed_password="unused",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_access_token_is_60_minutes_and_rejects_other_algorithm(monkeypatch):
    import jwt

    from app.core.tokens import TokenValidationError, create_access_token, decode_access_token

    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 60)
    token = create_access_token({"sub": "alice", "ver": 0})
    payload = decode_access_token(token)
    lifetime = datetime.fromtimestamp(payload["exp"], timezone.utc) - datetime.fromtimestamp(
        payload["iat"], timezone.utc
    )
    assert 3599 <= lifetime.total_seconds() <= 3600

    confused = jwt.encode(
        {"sub": "alice", "ver": 0, "iat": payload["iat"], "exp": payload["exp"]},
        settings.SECRET_KEY,
        algorithm="HS512",
    )
    with pytest.raises(TokenValidationError):
        decode_access_token(confused)


def test_refresh_rotation_replay_revokes_whole_family(db_session, active_user):
    from app.services.refresh_tokens import (
        RefreshTokenRejected,
        issue_refresh_token,
        rotate_refresh_token,
    )

    first = issue_refresh_token(db_session, active_user)
    second = rotate_refresh_token(db_session, first, active_user)

    with pytest.raises(RefreshTokenRejected, match="重放"):
        rotate_refresh_token(db_session, first, active_user)
    with pytest.raises(RefreshTokenRejected, match="撤销"):
        rotate_refresh_token(db_session, second, active_user)


@pytest.mark.parametrize("mutation", ["disabled", "version"])
def test_refresh_invalid_after_disable_or_password_reset(db_session, active_user, mutation):
    from app.services.refresh_tokens import (
        RefreshTokenRejected,
        issue_refresh_token,
        rotate_refresh_token,
    )

    raw = issue_refresh_token(db_session, active_user)
    if mutation == "disabled":
        active_user.status = "disabled"
    else:
        active_user.token_version += 1
    db_session.commit()

    with pytest.raises(RefreshTokenRejected):
        rotate_refresh_token(db_session, raw, active_user)


def test_forwarded_ip_only_trusted_from_configured_proxy(monkeypatch):
    from app.api.auth import _request_source_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    spoofed = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.9"},
        client=SimpleNamespace(host="198.51.100.7"),
    )
    proxied = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2"},
        client=SimpleNamespace(host="10.0.0.2"),
    )
    assert _request_source_ip(spoofed) == "198.51.100.7"
    assert _request_source_ip(proxied) == "203.0.113.9"


def test_unknown_user_still_executes_fake_password_hash():
    from app.api import auth

    class _Query:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class _Db:
        def query(self, *_args):
            return _Query()

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    with patch.object(auth, "verify_password", return_value=False) as verify:
        with pytest.raises(HTTPException) as exc:
            auth.run_login_flow(
                auth.LoginRequest(username="missing", password="guess"),
                request=request,
                db=_Db(),
            )
    assert exc.value.status_code == 401
    verify.assert_called_once_with("guess", auth.DUMMY_PASSWORD_HASH)
