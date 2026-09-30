"""Collector Agent 删除接口测试(SQLite 内存库,直接调用路由函数)。

验证 delete_agent 能删除存在的 agent、对不存在的返回 404,并写入审计日志。
不触及告警/通知发送链路。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.api import collector as col
from app.models.collector import CollectorAgent
from app.models.audit import AuditLog


class _FakeUser:
    username = "tester"
    id = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _make_agent(db, agent_id="agent-x"):
    row = CollectorAgent(
        agent_id=agent_id,
        asset_id="ins-1",
        hostname="h1",
        ip="10.0.0.1",
        os_type="linux",
        labels_json="{}",
        status="online",
        current_version_id="",
        last_seen_at="2026-06-25 00:00:00",
        last_error="",
        token_hash="x",
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
    )
    db.add(row)
    db.commit()
    return row


def test_delete_agent_ok(db):
    _make_agent(db, "agent-del")
    result = col.delete_agent("agent-del", db=db, current_user=_FakeUser())
    assert result == {"ok": True, "agent_id": "agent-del"}
    assert db.query(CollectorAgent).filter(CollectorAgent.agent_id == "agent-del").count() == 0
    # 审计日志写入
    assert db.query(AuditLog).filter(AuditLog.action == "删除 Collector Agent").count() == 1


def test_delete_agent_not_found(db):
    with pytest.raises(Exception):
        col.delete_agent("nope", db=db, current_user=_FakeUser())
