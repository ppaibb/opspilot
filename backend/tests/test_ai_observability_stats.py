"""AI 观测大盘真实数据统计测试(SQLite 内存库,直接调用路由函数)。

验证 get_ai_observability_stats 完全基于真实表(ai_chat_sessions / ai_chat_messages)
计算指标,不再返回任何 mock/硬编码数据。
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


def test_stats_empty_db_all_zero(db):
    """空库时所有指标为 0,趋势为长度 7 的全 0 列表。"""
    stats = ai.get_ai_observability_stats(db=db)
    assert stats.total_sessions == 0
    assert stats.total_messages == 0
    assert stats.today_messages == 0
    assert stats.active_users == 0
    assert stats.user_messages == 0
    assert stats.assistant_messages == 0
    assert stats.chat_trend_7d == [0, 0, 0, 0, 0, 0, 0]


def test_stats_reflect_real_sessions_and_messages(db):
    """统计应如实反映会话/消息表,且区分用户与助手消息。"""
    u1 = _FakeUser(1)
    u2 = _FakeUser(2)

    s1 = ai.create_chat_session(ai.ChatSessionCreateRequest(), db=db, current_user=u1)
    s2 = ai.create_chat_session(ai.ChatSessionCreateRequest(), db=db, current_user=u2)

    ai.append_chat_message(
        s1.session_id, ai.ChatMessageAppendRequest(role="user", content="问题1"),
        db=db, current_user=u1,
    )
    ai.append_chat_message(
        s1.session_id, ai.ChatMessageAppendRequest(role="assistant", content="回复1"),
        db=db, current_user=u1,
    )
    ai.append_chat_message(
        s2.session_id, ai.ChatMessageAppendRequest(role="user", content="问题2"),
        db=db, current_user=u2,
    )

    stats = ai.get_ai_observability_stats(db=db)
    assert stats.total_sessions == 2
    assert stats.total_messages == 3
    assert stats.user_messages == 2
    assert stats.assistant_messages == 1
    assert stats.active_users == 2
    # 消息均为当日创建
    assert stats.today_messages == 3
    # 趋势末位(今天)应为今日消息数
    assert len(stats.chat_trend_7d) == 7
    assert stats.chat_trend_7d[-1] == 3
