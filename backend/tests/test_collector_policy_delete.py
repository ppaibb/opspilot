"""Collector 策略删除接口测试(SQLite 内存库,直接调用路由函数)。

验证 delete_policy:
- 删除存在且无占用的策略 -> 连同其配置版本/发布记录级联清理,写审计日志
- 策略仍被 Agent 使用(current_version_id 命中) -> 409 拦截,不删除
- 策略不存在 -> 404
不触及告警/通知发送链路。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.api import collector as col
from app.models.collector import (
    CollectorAgent,
    CollectorPolicy,
    CollectorConfigVersion,
    CollectorRelease,
)
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


def _make_policy(db, policy_id="pol-1"):
    p = CollectorPolicy(
        policy_id=policy_id,
        name="测试策略",
        scope_type="global",
        scope_ref="",
        labels_json="{}",
        enabled="true",
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
    )
    db.add(p)
    db.commit()
    return p


def _make_version(db, policy_id, version_id="ver-1"):
    v = CollectorConfigVersion(
        version_id=version_id,
        policy_id=policy_id,
        version=1,
        config_hash="h",
        alloy_config="{}",
    )
    db.add(v)
    db.commit()
    return v


def _make_agent(db, version_id="", agent_id="agent-x"):
    row = CollectorAgent(
        agent_id=agent_id,
        asset_id="ins-1",
        hostname="h1",
        ip="10.0.0.1",
        os_type="linux",
        labels_json="{}",
        status="online",
        current_version_id=version_id,
        last_seen_at="2026-06-25 00:00:00",
        last_error="",
        token_hash="x",
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
    )
    db.add(row)
    db.commit()
    return row


def test_delete_policy_ok_cascades(db):
    _make_policy(db, "pol-del")
    _make_version(db, "pol-del", "ver-del")
    db.add(CollectorRelease(release_id="rel-1", version_id="ver-del", target_type="agent", target_ref="agent-x", status="done"))
    db.commit()

    result = col.delete_policy("pol-del", db=db, current_user=_FakeUser())
    assert result["ok"] is True
    assert result["policy_id"] == "pol-del"
    assert result["removed_versions"] == 1
    assert db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == "pol-del").count() == 0
    assert db.query(CollectorConfigVersion).filter(CollectorConfigVersion.policy_id == "pol-del").count() == 0
    assert db.query(CollectorRelease).filter(CollectorRelease.version_id == "ver-del").count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == "删除采集策略").count() == 1


def test_delete_policy_blocked_when_in_use(db):
    _make_policy(db, "pol-busy")
    _make_version(db, "pol-busy", "ver-busy")
    _make_agent(db, version_id="ver-busy", agent_id="agent-busy")

    with pytest.raises(HTTPException) as exc:
        col.delete_policy("pol-busy", db=db, current_user=_FakeUser())
    assert exc.value.status_code == 409
    assert "agents" in exc.value.detail
    # 拦截后策略与版本都还在
    assert db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == "pol-busy").count() == 1
    assert db.query(CollectorConfigVersion).filter(CollectorConfigVersion.policy_id == "pol-busy").count() == 1


def test_delete_policy_not_found(db):
    with pytest.raises(HTTPException) as exc:
        col.delete_policy("nope", db=db, current_user=_FakeUser())
    assert exc.value.status_code == 404
