"""默认 Alloy 策略可编辑性测试（SQLite 内存库，直接调用服务函数）。

验证 _ensure_asset_alloy_policy 的"基线只做初始化、保留人工编辑"语义：
- 首次调用：写入基线默认（node 指标 + system/docker 日志），发布到 Agent
- 用户在采集管理页改了策略内容后再次触发同步（装机/应用归属变更/重同步）：
  保留用户编辑过的 metrics/logs，不被基线默认冲掉
- 序列化暴露 is_default 标记
不触及告警/通知发送链路。
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.api.monitoring.services import alloy_policy as ap
from app.api import collector as col
from app.models.asset import Asset
from app.models.collector import CollectorAgent, CollectorPolicy


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
def _patch_config(monkeypatch):
    # 跳过真实 system_configs 读取，直接给齐写入地址，避免触发外部依赖。
    monkeypatch.setattr(
        ap,
        "read_monitoring_config",
        lambda db: {
            "remote_write_url": "http://10.0.0.1:8480/insert/0/prometheus/api/v1/write",
            "vlogs_ingest_url": "http://10.0.0.1:9428/insert/jsonline",
        },
    )


def _make_asset(db):
    a = Asset(
        instance_id="ins-test-1",
        name="测试机",
        ip="10.0.0.2",
        hostname="test-1",
        provider="on-premise",
        region="local",
    )
    db.add(a)
    db.commit()
    return a


def _make_agent(db, asset):
    ag = CollectorAgent(
        agent_id="agent-test-1",
        asset_id=asset.instance_id,
        token_hash="x",
        status="online",
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
    )
    db.add(ag)
    db.commit()
    return ag


def test_first_create_seeds_baseline(db):
    asset = _make_asset(db)
    agent = _make_agent(db, asset)

    ver = ap._ensure_asset_alloy_policy(db, asset, agent, _FakeUser())
    assert ver is not None

    pid = f"asset-alloy-{asset.instance_id}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == pid).first()
    assert policy is not None
    metrics = json.loads(policy.metrics_json)
    logs = json.loads(policy.logs_json)
    assert metrics.get("node") is True
    # 产品决策：只采 Docker，不采 syslog
    assert logs.get("system") is False
    assert logs.get("docker_containers") is True
    # 序列化暴露 is_default
    assert col._serialize_policy(policy)["is_default"] is True


def test_user_edit_preserved_on_resync(db):
    asset = _make_asset(db)
    agent = _make_agent(db, asset)
    ap._ensure_asset_alloy_policy(db, asset, agent, _FakeUser())

    pid = f"asset-alloy-{asset.instance_id}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == pid).first()

    # 模拟用户在采集管理页编辑：关掉 docker 日志、加一个自定义 exporter
    edited_metrics = {"node": True, "scrape_targets": [{"job": "mysql", "targets": ["10.0.0.2:9104"]}]}
    edited_logs = {"system": True, "docker_containers": False}
    policy.metrics_json = json.dumps(edited_metrics, ensure_ascii=False, sort_keys=True)
    policy.logs_json = json.dumps(edited_logs, ensure_ascii=False, sort_keys=True)
    db.commit()

    # 再次触发同步（等价于装机/应用归属变更/重同步）
    ap._ensure_asset_alloy_policy(db, asset, agent, _FakeUser())

    db.refresh(policy)
    metrics = json.loads(policy.metrics_json)
    logs = json.loads(policy.logs_json)
    # 用户编辑保留，未被基线默认冲掉
    assert metrics.get("scrape_targets") == [{"job": "mysql", "targets": ["10.0.0.2:9104"]}]
    assert logs.get("docker_containers") is False


def test_logs_override_forces_docker_only(db):
    """日志目标启停用 logs_override 强制覆盖，只采 Docker。"""
    asset = _make_asset(db)
    agent = _make_agent(db, asset)
    ap._ensure_asset_alloy_policy(db, asset, agent, _FakeUser())

    pid = f"asset-alloy-{asset.instance_id}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == pid).first()

    # 用户曾打开 system；启停日志时仍被强制成只采 Docker
    policy.logs_json = json.dumps({"system": True, "docker_containers": False}, ensure_ascii=False)
    db.commit()

    ap._ensure_asset_alloy_policy(
        db, asset, agent, _FakeUser(),
        logs_override={"system": False, "docker_containers": True},
    )
    db.refresh(policy)
    logs = json.loads(policy.logs_json)
    assert logs.get("system") is False
    assert logs.get("docker_containers") is True

    ap._ensure_asset_alloy_policy(
        db, asset, agent, _FakeUser(),
        logs_override={"system": False, "docker_containers": False},
    )
    db.refresh(policy)
    logs = json.loads(policy.logs_json)
    assert logs.get("docker_containers") is False
