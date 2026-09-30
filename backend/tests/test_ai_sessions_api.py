"""AI 会话持久化 API 逻辑测试(SQLite 内存库,直接调用路由函数)。

验证会话的新增 / 列表 / 读取 / 重命名 / 删除,以及追加消息时计数与标题维护、
跨用户越权防护。不经过 FastAPI 依赖注入,直接给路由函数传 db/payload。
本测试不触及告警/通知发送链路,无需 monkeypatch。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.api import ai_agent as ai


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_session_create_list_get(db):
    user = _FakeUser(1)
    created = ai.create_chat_session(
        ai.ChatSessionCreateRequest(title="排查告警", agent_id="ops"),
        db=db, current_user=user,
    )
    assert created.session_id
    assert created.title == "排查告警"
    assert created.message_count == 0

    listed = ai.list_chat_sessions(db=db, current_user=user)
    assert len(listed.sessions) == 1
    assert listed.sessions[0].session_id == created.session_id

    detail = ai.get_chat_session(created.session_id, db=db, current_user=user)
    assert detail.session_id == created.session_id
    assert detail.messages == []


def test_append_message_updates_count_and_title(db):
    user = _FakeUser(1)
    created = ai.create_chat_session(
        ai.ChatSessionCreateRequest(),  # 默认标题"新会话"
        db=db, current_user=user,
    )
    assert created.title == "新会话"

    ai.append_chat_message(
        created.session_id,
        ai.ChatMessageAppendRequest(role="user", content="帮我看看 nginx 502 的告警"),
        db=db, current_user=user,
    )
    ai.append_chat_message(
        created.session_id,
        ai.ChatMessageAppendRequest(role="assistant", content="已定位到上游超时"),
        db=db, current_user=user,
    )

    detail = ai.get_chat_session(created.session_id, db=db, current_user=user)
    assert detail.message_count == 2
    assert len(detail.messages) == 2
    # 首条用户消息成为标题
    assert detail.title == "帮我看看 nginx 502 的告警"
    assert detail.messages[0].role == "user"
    assert detail.messages[1].role == "assistant"


def test_rename_and_delete(db):
    user = _FakeUser(1)
    created = ai.create_chat_session(ai.ChatSessionCreateRequest(), db=db, current_user=user)
    ai.append_chat_message(
        created.session_id,
        ai.ChatMessageAppendRequest(role="user", content="hi"),
        db=db, current_user=user,
    )

    renamed = ai.rename_chat_session(
        created.session_id,
        ai.ChatSessionUpdateRequest(title="自定义标题"),
        db=db, current_user=user,
    )
    assert renamed.title == "自定义标题"

    result = ai.delete_chat_session(created.session_id, db=db, current_user=user)
    assert result["ok"] is True

    # 删除后列表为空,消息也被清掉
    assert ai.list_chat_sessions(db=db, current_user=user).sessions == []
    from app.models.ai_chat import AIChatMessage
    assert db.query(AIChatMessage).filter(AIChatMessage.session_id == created.session_id).count() == 0


def test_cross_user_isolation(db):
    owner = _FakeUser(1)
    intruder = _FakeUser(2)
    created = ai.create_chat_session(ai.ChatSessionCreateRequest(), db=db, current_user=owner)

    # 另一个用户看不到、读不到、删不掉
    assert ai.list_chat_sessions(db=db, current_user=intruder).sessions == []
    with pytest.raises(Exception):
        ai.get_chat_session(created.session_id, db=db, current_user=intruder)
    with pytest.raises(Exception):
        ai.delete_chat_session(created.session_id, db=db, current_user=intruder)


def test_invalid_role_rejected(db):
    user = _FakeUser(1)
    created = ai.create_chat_session(ai.ChatSessionCreateRequest(), db=db, current_user=user)
    with pytest.raises(Exception):
        ai.append_chat_message(
            created.session_id,
            ai.ChatMessageAppendRequest(role="system", content="x"),
            db=db, current_user=user,
        )
