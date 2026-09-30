"""用户站内通知 API 测试。

使用 in-memory SQLite，直接调用 route 函数（跳过 FastAPI Depends），
monkeypatch get_current_user / get_db。
"""
from __future__ import annotations

import pytest
from unittest.mock import patch
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.user import User
from app.models.notification import UserNotification
from app.api.notifications import (
    list_notifications,
    unread_count,
    mark_read,
    mark_all_read,
    delete_notification,
    clear_notifications,
    create_notification,
    NotificationCreate,
    _sse_subscribers,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(db_engine):
    connection = db_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def user(db):
    u = User(
        id=1,
        username="testuser",
        name="测试用户",
        email="test@example.com",
        hashed_password="fake",
        role="admin",
        status="active",
    )
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def other_user(db):
    u = User(
        id=2,
        username="otheruser",
        name="其他用户",
        email="other@example.com",
        hashed_password="fake",
        role="user",
        status="active",
    )
    db.add(u)
    db.commit()
    return u


def _seed_notification(db, user_id: int, **kwargs) -> UserNotification:
    defaults = dict(
        user_id=user_id,
        type="info",
        category="system",
        title="测试通知",
        content="测试内容",
    )
    defaults.update(kwargs)
    n = UserNotification(**defaults)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestListNotifications:
    def test_empty_list(self, db, user):
        resp = list_notifications(is_read=None, limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 0
        assert resp.unread_count == 0
        assert resp.items == []

    def test_list_with_items(self, db, user):
        _seed_notification(db, user.id, title="通知A", type="success")
        _seed_notification(db, user.id, title="通知B", type="error")
        resp = list_notifications(is_read=None, limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 2
        assert resp.unread_count == 2
        assert len(resp.items) == 2
        # 默认按 created_at desc，最新在前
        assert resp.items[0].title == "通知B"

    def test_filter_unread(self, db, user):
        n = _seed_notification(db, user.id, title="已读通知")
        n.is_read = True
        db.commit()
        _seed_notification(db, user.id, title="未读通知")
        resp = list_notifications(is_read=False, limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 1
        assert resp.items[0].title == "未读通知"

    def test_filter_read(self, db, user):
        n = _seed_notification(db, user.id, title="已读")
        n.is_read = True
        db.commit()
        resp = list_notifications(is_read=True, limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 1
        assert resp.items[0].is_read is True

    def test_pagination(self, db, user):
        for i in range(5):
            _seed_notification(db, user.id, title=f"page-{i}")
        resp = list_notifications(is_read=None, limit=2, offset=0, db=db, current_user=user)
        assert len(resp.items) == 2
        resp2 = list_notifications(is_read=None, limit=2, offset=2, db=db, current_user=user)
        assert len(resp2.items) == 2
        # 确保分页不重复
        assert resp.items[0].id != resp2.items[0].id

    def test_user_isolation(self, db, user, other_user):
        _seed_notification(db, user.id, title="user1的通知")
        _seed_notification(db, other_user.id, title="user2的通知")
        resp = list_notifications(is_read=None, limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 1
        assert resp.items[0].title == "user1的通知"

    def test_filter_category(self, db, user):
        _seed_notification(db, user.id, title="证书过期", category="certificate")
        _seed_notification(db, user.id, title="工单待审", category="db")
        resp = list_notifications(is_read=None, category="db", limit=20, offset=0, db=db, current_user=user)
        assert resp.total == 1
        assert resp.items[0].title == "工单待审"

    def test_keyword_search_matches_title_and_content(self, db, user):
        _seed_notification(db, user.id, title="工单已通过", content="审批人 admin")
        _seed_notification(db, user.id, title="证书即将过期", content="还有 7 天")
        resp = list_notifications(is_read=None, q="工单", limit=20, offset=0, db=db, current_user=user)
        assert [i.title for i in resp.items] == ["工单已通过"]
        resp2 = list_notifications(is_read=None, q="admin", limit=20, offset=0, db=db, current_user=user)
        assert [i.title for i in resp2.items] == ["工单已通过"]

    def test_keyword_search_escapes_like_wildcards(self, db, user):
        _seed_notification(db, user.id, title="100% 完成")
        _seed_notification(db, user.id, title="普通通知")
        # 未转义时 "%" 会匹配全部；转义后只匹配标题里真有 "%" 的那条
        resp = list_notifications(is_read=None, q="%", limit=20, offset=0, db=db, current_user=user)
        assert [i.title for i in resp.items] == ["100% 完成"]

    def test_category_and_keyword_combined(self, db, user):
        _seed_notification(db, user.id, title="db 工单待审", category="db")
        _seed_notification(db, user.id, title="证书工单待审", category="certificate")
        resp = list_notifications(is_read=None, category="db", q="待审", limit=20, offset=0, db=db, current_user=user)
        assert [i.title for i in resp.items] == ["db 工单待审"]


class TestUnreadCount:
    def test_zero(self, db, user):
        resp = unread_count(db=db, current_user=user)
        # 可能有之前测试留下的未读，这里只验证返回结构
        assert hasattr(resp, "unread_count")
        assert resp.unread_count >= 0

    def test_counts_unread_only(self, db, user):
        _seed_notification(db, user.id, title="unread-1")
        n = _seed_notification(db, user.id, title="read-1")
        n.is_read = True
        db.commit()
        before = unread_count(db=db, current_user=user).unread_count
        _seed_notification(db, user.id, title="unread-2")
        after = unread_count(db=db, current_user=user).unread_count
        assert after == before + 1


class TestMarkRead:
    def test_mark_single(self, db, user):
        n = _seed_notification(db, user.id)
        assert n.is_read is False
        result = mark_read(notification_id=n.id, db=db, current_user=user)
        assert result["ok"] is True
        db.refresh(n)
        assert n.is_read is True

    def test_mark_already_read(self, db, user):
        n = _seed_notification(db, user.id)
        n.is_read = True
        db.commit()
        result = mark_read(notification_id=n.id, db=db, current_user=user)
        assert result["ok"] is True  # 幂等

    def test_mark_nonexistent(self, db, user):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            mark_read(notification_id="nonexistent-id", db=db, current_user=user)
        assert exc_info.value.status_code == 404

    def test_cannot_mark_others(self, db, user, other_user):
        n = _seed_notification(db, other_user.id)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            mark_read(notification_id=n.id, db=db, current_user=user)
        assert exc_info.value.status_code == 404


class TestMarkAllRead:
    def test_marks_all(self, db, user):
        _seed_notification(db, user.id, title="a")
        _seed_notification(db, user.id, title="b")
        _seed_notification(db, user.id, title="c")
        result = mark_all_read(db=db, current_user=user)
        assert result["ok"] is True
        assert result["updated"] >= 3
        resp = unread_count(db=db, current_user=user)
        # 所有该用户的未读应为 0
        remaining = (
            db.query(UserNotification)
            .filter(UserNotification.user_id == user.id, UserNotification.is_read.is_(False))
            .count()
        )
        assert remaining == 0


class TestDeleteNotification:
    def test_delete(self, db, user):
        n = _seed_notification(db, user.id)
        result = delete_notification(notification_id=n.id, db=db, current_user=user)
        assert result["ok"] is True
        assert db.query(UserNotification).filter(UserNotification.id == n.id).first() is None

    def test_delete_nonexistent(self, db, user):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            delete_notification(notification_id="nope", db=db, current_user=user)
        assert exc_info.value.status_code == 404

    def test_cannot_delete_others(self, db, user, other_user):
        n = _seed_notification(db, other_user.id)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            delete_notification(notification_id=n.id, db=db, current_user=user)
        assert exc_info.value.status_code == 404


class TestClearNotifications:
    def test_clear(self, db, user):
        _seed_notification(db, user.id, title="x")
        _seed_notification(db, user.id, title="y")
        result = clear_notifications(db=db, current_user=user)
        assert result["ok"] is True
        assert result["deleted"] >= 2
        remaining = (
            db.query(UserNotification)
            .filter(UserNotification.user_id == user.id)
            .count()
        )
        assert remaining == 0

    def test_clear_only_own(self, db, user, other_user):
        _seed_notification(db, user.id, title="mine")
        _seed_notification(db, other_user.id, title="theirs")
        clear_notifications(db=db, current_user=user)
        other_count = (
            db.query(UserNotification)
            .filter(UserNotification.user_id == other_user.id)
            .count()
        )
        assert other_count == 1  # other_user的通知不受影响


class TestCreateNotificationHelper:
    def test_create_helper(self, db, user):
        n = create_notification(
            db,
            data=NotificationCreate(
                user_id=user.id,
                type="success",
                category="certificate",
                title="证书续期完成",
                content="example.com 已续期",
                link="/assets/certificates",
                resource_id="cert_123",
            ),
        )
        assert n.id is not None
        assert n.type == "success"
        assert n.category == "certificate"
        assert n.title == "证书续期完成"
        assert n.is_read is False
        assert n.link == "/assets/certificates"
        assert n.resource_id == "cert_123"

    def test_create_defaults(self, db, user):
        n = create_notification(
            db,
            data=NotificationCreate(user_id=user.id, title="简单通知"),
        )
        assert n.type == "info"
        assert n.category == "system"
        assert n.content == ""
        assert n.link is None


class TestSSEPubSub:
    """SSE pub/sub 单元测试（不涉及 HTTP，纯 Queue 行为）。"""

    def test_subscribe_and_publish(self):
        from app.api.notifications import _subscribe, _unsubscribe, publish_notification

        q = _subscribe(user_id=999)
        assert 999 in _sse_subscribers

        payload = {"id": "test-1", "title": "hello"}
        publish_notification(999, payload)
        assert not q.empty()
        got = q.get_nowait()
        assert got["id"] == "test-1"

        _unsubscribe(999, q)
        assert 999 not in _sse_subscribers

    def test_publish_no_subscribers_no_error(self):
        from app.api.notifications import publish_notification
        # 不应该抛异常
        publish_notification(user_id=99999, payload={"x": 1})

    def test_multiple_subscribers(self):
        from app.api.notifications import _subscribe, _unsubscribe, publish_notification

        q1 = _subscribe(user_id=888)
        q2 = _subscribe(user_id=888)
        assert len(_sse_subscribers.get(888, set())) == 2

        publish_notification(888, {"id": "multi"})
        assert not q1.empty()
        assert not q2.empty()

        _unsubscribe(888, q1)
        _unsubscribe(888, q2)

    def test_verify_sse_ticket_valid(self, db, user):
        """合法票据（scope=notifications-sse）可通过校验。"""
        from jose import jwt
        from app.core.config import settings
        from app.api.notifications import SSE_TICKET_SCOPE, _verify_sse_ticket

        ticket = jwt.encode(
            {"sub": user.username, "scope": SSE_TICKET_SCOPE},
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        result = _verify_sse_ticket(ticket, db)
        assert result.id == user.id

    def test_verify_sse_ticket_rejects_access_token(self, db, user):
        """长期 access token 没有 notifications-sse scope，不能当票据用。"""
        from jose import jwt
        from app.core.config import settings
        from app.api.notifications import _verify_sse_ticket
        from fastapi import HTTPException

        token = jwt.encode({"sub": user.username}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        with pytest.raises(HTTPException) as exc_info:
            _verify_sse_ticket(token, db)
        assert exc_info.value.status_code == 401

    def test_verify_sse_ticket_invalid(self, db):
        from app.api.notifications import _verify_sse_ticket
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _verify_sse_ticket("invalid-token", db)
        assert exc_info.value.status_code == 401
