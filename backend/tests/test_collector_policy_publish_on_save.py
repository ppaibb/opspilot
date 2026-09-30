"""采集策略"保存即下发"测试(SQLite 内存库,直接调用路由函数)。

验证 A 能力:create/update policy 带 publish=True 时,生成新配置版本并按策略
自身 scope 下发到生效 Agent(agent.current_version_id 更新),version 状态置为
published,并返回 release。
不触及告警/通知发送链路。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.api import collector as col
from app.api.collector import CollectorPolicyRequest
from app.models.collector import CollectorAgent, CollectorConfigVersion


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


@pytest.fixture(autouse=True)
def _patch_cfg(monkeypatch):
    monkeypatch.setattr(
        col,
        "read_monitoring_config",
        lambda db: {
            "remote_write_url": "http://10.0.0.1:8480/insert/0/prometheus/api/v1/write",
            "vlogs_ingest_url": "http://10.0.0.1:9428/insert/jsonline",
        },
    )


def _make_agent(db, agent_id, version_id=None):
    ag = CollectorAgent(
        agent_id=agent_id,
        asset_id=f"asset-{agent_id}",
        token_hash="x",
        status="online",
        current_version_id=version_id,
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
    )
    db.add(ag)
    db.commit()
    return ag


def test_create_with_publish_downloads_to_agent(db):
    agent = _make_agent(db, "agent-aaa")
    payload = CollectorPolicyRequest(
        name="agent 策略",
        scope_type="agent",
        scope_ref="agent-aaa",
        metrics={"node": True},
        logs={"system": True},
        publish=True,
    )
    result = col.create_policy(payload=payload, db=db, current_user=_FakeUser())

    assert "release" in result
    version_id = result["version"]["version_id"]
    db.refresh(agent)
    # 已下发到该 Agent
    assert agent.current_version_id == version_id
    # 版本置为 published
    ver = db.query(CollectorConfigVersion).filter(CollectorConfigVersion.version_id == version_id).first()
    assert ver.status == "published"


def test_create_without_publish_stays_draft(db):
    agent = _make_agent(db, "agent-bbb")
    payload = CollectorPolicyRequest(
        name="草稿策略",
        scope_type="agent",
        scope_ref="agent-bbb",
        metrics={"node": True},
        logs={"system": True},
        publish=False,
    )
    result = col.create_policy(payload=payload, db=db, current_user=_FakeUser())

    assert "release" not in result
    db.refresh(agent)
    # 未发布,Agent 不指向新版本(初始为空)
    assert not agent.current_version_id
    version_id = result["version"]["version_id"]
    ver = db.query(CollectorConfigVersion).filter(CollectorConfigVersion.version_id == version_id).first()
    assert ver.status == "draft"
