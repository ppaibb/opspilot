"""Bootstrap、审计、Webhook 与 SQL 回滚安全边界。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.passwords import hash_password
from app.db.database import Base
from app.models.system_config import SystemConfig
from app.models.user import User


def _session_factory(*tables):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=list(tables))
    return sessionmaker(bind=engine), engine


def test_bootstrap_never_changes_existing_user_role_or_status(monkeypatch):
    from app.db import bootstrap

    factory, engine = _session_factory(User.__table__)
    db = factory()
    original_hash = hash_password("Original@123")
    db.add(User(username="admin", role="只读", status="disabled", hashed_password=original_hash))
    db.commit()
    db.close()

    monkeypatch.setattr(settings, "INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "INIT_ADMIN_PASSWORD", "Different@123")
    monkeypatch.setattr(settings, "INIT_ADMIN_ROLE", "管理员")
    monkeypatch.setattr(bootstrap, "SessionLocal", factory)
    bootstrap.ensure_bootstrap_admin()

    verify_db = factory()
    user = verify_db.query(User).filter(User.username == "admin").one()
    assert user.role == "只读"
    assert user.status == "disabled"
    assert user.hashed_password == original_hash
    verify_db.close()
    engine.dispose()


def test_bootstrap_rejects_weak_initial_password(monkeypatch):
    from app.db import bootstrap

    factory, engine = _session_factory(User.__table__)
    monkeypatch.setattr(settings, "INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "INIT_ADMIN_PASSWORD", "password123")
    monkeypatch.setattr(bootstrap, "SessionLocal", factory)
    with pytest.raises(ValueError, match="初始化管理员密码"):
        bootstrap.ensure_bootstrap_admin()
    engine.dispose()


def test_audit_storage_failure_never_overrides_business_response():
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from app import main

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/example",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "server": ("test", 80),
        "query_string": b"",
    })

    class _BrokenDb:
        def add(self, _row):
            return None

        def commit(self):
            raise RuntimeError("audit db down")

        def rollback(self):
            return None

        def close(self):
            return None

    async def run():
        with patch.object(main, "SessionLocal", return_value=_BrokenDb()):
            return await main.audit_write_operations(
                request,
                lambda _request: _async_response(JSONResponse({"ok": True}, status_code=201)),
            )

    response = asyncio.run(run())
    assert response.status_code == 201


async def _async_response(response):
    return response


def test_risk_reason_is_cleaned_and_bounded():
    from app.main import sanitize_risk_reason

    raw = " deploy\r\nX-Forged: yes\x00" + "x" * 900
    result = sanitize_risk_reason(raw)
    assert "\r" not in result and "\n" not in result and "\x00" not in result
    assert len(result) <= 500


def test_log_error_endpoint_requires_authenticated_user():
    from app.core.security import get_current_user
    from app.main import app

    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/log_error")
    dependencies = {item.call for item in route.dependant.dependencies}
    assert get_current_user in dependencies


def test_webhook_token_uses_constant_time_compare(monkeypatch):
    from app.api import monitoring_webhooks

    request = SimpleNamespace(
        body=lambda: _async_response(b"{}"),
        headers={"content-type": "application/json"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    db = SimpleNamespace()
    monkeypatch.setattr(monitoring_webhooks, "_ensure_receiver_token", lambda _db: ("expected", ""))
    called = []
    monkeypatch.setattr(
        monitoring_webhooks.secrets,
        "compare_digest",
        lambda left, right: called.append((left, right)) or False,
    )
    async def run():
        return await monitoring_webhooks.receive_alertmanager_webhook("wrong", request, db)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 404
    assert called == [("wrong", "expected")]


def test_postgresql_rollback_uses_boolean_literals():
    from app.services.db_ops import build_rollback_inserts

    sql = build_rollback_inserts(
        "flags",
        ["enabled", "deleted"],
        [{"enabled": True, "deleted": False}],
        db_type="postgresql",
    )
    assert "TRUE" in sql
    assert "FALSE" in sql
    assert "VALUES (1, 0)" not in sql


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/api/wireguard/networks/1/server-config", "high"),
        ("/api/wireguard/peers/2/config", "high"),
        ("/api/db/tickets/3/rollback", "high"),
        ("/api/assets/terminal/files/host-1/download", "medium"),
    ],
)
def test_sensitive_get_routes_are_in_audit_risk_registry(path, expected):
    from app.main import resolve_request_risk_level

    assert resolve_request_risk_level("GET", path) == expected
