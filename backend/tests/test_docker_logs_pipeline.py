# -*- coding: utf-8 -*-
"""Docker 日志链路与写入地址校验契约测试。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401
from app.api.monitoring.services import alloy_policy as ap
from app.api.monitoring.services import log_targets as lt
from app.api.monitoring.services.deployment import (
    _validate_write_url_format,
    probe_agent_write_urls,
    resolve_agent_write_urls,
)
from app.services.collector_config import render_alloy_config
from app.models.asset import Asset
from app.models.collector import CollectorAgent, CollectorPolicy
from fastapi import HTTPException


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
    monkeypatch.setattr(
        ap,
        "read_monitoring_config",
        lambda db: {
            "remote_write_url": "http://10.0.0.1:8480/insert/0/prometheus/api/v1/write",
            "vlogs_ingest_url": "http://10.0.0.1:9428/insert/loki/api/v1/push",
        },
    )
    monkeypatch.setattr(
        lt,
        "read_monitoring_config",
        lambda db: {
            "remote_write_url": "http://10.0.0.1:8480/insert/0/prometheus/api/v1/write",
            "vlogs_ingest_url": "http://10.0.0.1:9428/insert/loki/api/v1/push",
        },
    )


def _make_asset(db):
    a = Asset(
        instance_id="ins-docker-1",
        name="docker机",
        ip="10.0.0.9",
        hostname="docker-1",
        provider="on-premise",
        region="local",
    )
    db.add(a)
    db.commit()
    return a


def _make_agent(db, asset):
    ag = CollectorAgent(
        agent_id="agent-docker-1",
        asset_id=asset.instance_id,
        token_hash="x",
        status="online",
        current_version_id="",
        created_at="2026-06-25 00:00:00",
        updated_at="2026-06-25 00:00:00",
        last_seen_at="2026-06-25 00:01:00",
    )
    db.add(ag)
    db.commit()
    return ag


def test_render_uses_loki_source_docker():
    """Docker 日志必须走 loki.source.docker + job=container，不再 tail json.log。"""
    cfg = render_alloy_config(
        agent_id="agent-1",
        metrics={"node": False},
        logs={"system": False, "docker_containers": True},
        labels={"asset_id": "i-1", "host": "h-1", "instance": "10.0.0.1"},
        remote_write_url="http://vm.example.com/api/v1/write",
        vlogs_url="http://vl.example.com/insert/loki/api/v1/push",
    )
    assert 'loki.source.docker "docker_containers"' in cfg
    assert 'discovery.docker "docker_containers"' in cfg
    assert "targets = discovery.docker.docker_containers.targets" in cfg
    assert 'host = "unix:///var/run/docker.sock"' in cfg
    assert 'job = "container"' in cfg
    assert "/var/lib/docker/containers" not in cfg
    assert "loki.source.file" not in cfg


def test_render_system_logs_still_optional():
    """system 显式开启时仍可采文件日志（兼容旧策略），默认策略不再开启。"""
    cfg = render_alloy_config(
        agent_id="agent-2",
        metrics={"node": False},
        logs={"system": True, "docker_containers": False},
        labels={"asset_id": "i-2", "host": "h-2", "instance": "10.0.0.2"},
        remote_write_url="http://vm.example.com/api/v1/write",
        vlogs_url="http://vl.example.com/insert/loki/api/v1/push",
    )
    assert "loki.source.file" in cfg
    assert "loki.source.docker" not in cfg


def test_persist_log_targets_syncs_alloy_policy(db, monkeypatch):
    """启用日志采集时必须把 docker 采集块下发到 Alloy 策略。"""
    asset = _make_asset(db)
    agent = _make_agent(db, asset)
    # 避免真实写 deploy/monitoring 配置
    monkeypatch.setattr(ap, "render_alloy_config", render_alloy_config)

    result = lt._persist_log_targets(
        db,
        [{
            "asset_id": asset.instance_id,
            "name": asset.name,
            "ip": asset.ip,
            "enabled": True,
            "status": "up",
        }],
        current_user=_FakeUser(),
    )
    assert result["saved_count"] == 1
    assert result["log_sync"]["synced"] == 1

    pid = f"asset-alloy-{asset.instance_id}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == pid).first()
    assert policy is not None
    logs = json.loads(policy.logs_json)
    assert logs.get("docker_containers") is True
    assert logs.get("system") is False

    # 以是否成功下发为准
    from app.api.monitoring.services.alloy_policy import _log_collect_state
    state = _log_collect_state(db, asset, agent, target_enabled=True)
    assert state["logs_enabled"] is True
    assert state["last_log_at"]


def test_persist_log_targets_disable_stops_block(db):
    """目标禁用时停用对应采集块。"""
    asset = _make_asset(db)
    agent = _make_agent(db, asset)
    lt._persist_log_targets(
        db,
        [{
            "asset_id": asset.instance_id,
            "name": asset.name,
            "ip": asset.ip,
            "enabled": True,
            "status": "up",
        }],
        current_user=_FakeUser(),
    )
    result = lt._persist_log_targets(
        db,
        [{
            "asset_id": asset.instance_id,
            "name": asset.name,
            "ip": asset.ip,
            "enabled": False,
            "status": "disabled",
        }],
        current_user=_FakeUser(),
    )
    assert result["log_sync"]["synced"] == 1
    pid = f"asset-alloy-{asset.instance_id}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == pid).first()
    logs = json.loads(policy.logs_json)
    assert logs.get("docker_containers") is False

    from app.api.monitoring.services.alloy_policy import _log_collect_state
    state = _log_collect_state(db, asset, agent, target_enabled=False)
    assert state["logs_enabled"] is False


def test_resolve_agent_write_urls_defaults():
    remote, vlogs = resolve_agent_write_urls({
        "vm_url": "http://vm:8428",
        "vlogs_url": "http://vl:9428",
    })
    assert remote == "http://vm:8428/api/v1/write"
    assert vlogs == "http://vl:9428/insert/loki/api/v1/push"

    # 存量 ES 写入口在解析时改写为 Loki push（Alloy 只用 loki.write）
    remote, vlogs = resolve_agent_write_urls({
        "vm_url": "http://vm:8428",
        "vlogs_url": "http://vl:9428",
        "vlogs_ingest_url": "http://vl:9428/insert/elasticsearch",
    })
    assert vlogs == "http://vl:9428/insert/loki/api/v1/push"

    remote, vlogs = resolve_agent_write_urls({
        "vm_url": "http://vm:8428",
        "vlogs_url": "http://vl:9428",
        "vlogs_ingest_url": "http://vl:9428/insert/loki/api/v1/push",
    })
    assert vlogs == "http://vl:9428/insert/loki/api/v1/push"


def test_validate_write_url_format():
    _validate_write_url_format("", "日志写入")
    _validate_write_url_format("http://ok:9428/insert/elasticsearch", "日志写入")
    with pytest.raises(HTTPException) as exc:
        _validate_write_url_format("ftp://bad", "日志写入")
    assert "HTTP(S)" in str(exc.value.detail)


def test_probe_agent_write_urls_unreachable_hint(monkeypatch):
    import requests as _requests

    def _boom(*args, **kwargs):
        raise _requests.ConnectionError("Connection refused")

    monkeypatch.setattr(_requests, "get", _boom)
    result = probe_agent_write_urls({
        "vm_url": "http://127.0.0.1:1",
        "vlogs_url": "http://127.0.0.1:1",
    })
    assert result["remote_write"]["ok"] is False
    assert result["vlogs_ingest"]["ok"] is False
    assert result["remote_write"]["hint"]
    assert result["vlogs_ingest"]["hint"]
    assert "insert/loki/api/v1/push" in result["vlogs_ingest_url"]
