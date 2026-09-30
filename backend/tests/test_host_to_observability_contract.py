"""添加主机 → 监控/日志链路契约。

覆盖 2026-09-24 全链路模拟发现的缺口：
- 无凭证新建主机应标 pending，而不是误报 SSH 失败；
- PromQL instance 正则不能用前缀 `.*`（192.168.1.1 会命中 192.168.1.10）；
- 日志作用域 IP 匹配要用边界，不能子串；
- LogsQL 作用域子句要覆盖 instance/host/hostname。
"""
from __future__ import annotations

import inspect
import re

from app.api.assets import create_asset
from app.api.monitoring.services.dashboard import (
    _matches_any_scope,
    build_logs_scope_clause,
    prom_instance_regex,
)


# ---------------------------- PromQL instance 正则 ----------------------------


def test_prom_instance_regex_matches_exact_and_port_suffix():
    pattern = prom_instance_regex("192.168.1.1")
    assert re.fullmatch(pattern, "192.168.1.1")
    assert re.fullmatch(pattern, "192.168.1.1:9100")
    assert not re.fullmatch(pattern, "192.168.1.10")
    assert not re.fullmatch(pattern, "192.168.1.10:9100")


def test_prom_instance_regex_supports_ip_or_hostname():
    pattern = prom_instance_regex("10.0.0.1", "web-server-01")
    assert re.fullmatch(pattern, "10.0.0.1")
    assert re.fullmatch(pattern, "web-server-01")
    assert re.fullmatch(pattern, "web-server-01:9100")
    assert not re.fullmatch(pattern, "10.0.0.10")
    assert not re.fullmatch(pattern, "web-server-010")


def test_prom_instance_regex_escapes_dots_without_backslash():
    pattern = prom_instance_regex("192.168.1.1")
    assert r"\." not in pattern
    assert "[.]" in pattern


def test_prom_instance_regex_empty_inputs():
    assert prom_instance_regex("", "  ") == ""


# ---------------------------- 日志/指标作用域匹配 ----------------------------


def test_scope_match_ip_does_not_prefix_hit():
    assert _matches_any_scope("192.168.1.10:9100", ["192.168.1.1"]) is False
    assert _matches_any_scope("192.168.1.1:9100", ["192.168.1.1"]) is True
    assert _matches_any_scope("host=192.168.1.1", ["192.168.1.1"]) is True


def test_scope_match_name_still_substring():
    assert _matches_any_scope("web-server-01", ["web"]) is True


def test_scope_match_empty_value():
    assert _matches_any_scope("", ["10.0.0.1"]) is False


# ---------------------------- LogsQL 作用域子句 ----------------------------


def test_logs_scope_clause_covers_host_fields():
    clause = build_logs_scope_clause(["10.0.0.1"], ["web-01"])
    assert "instance:10.0.0.1" in clause
    assert "host:10.0.0.1" in clause
    assert "hostname:10.0.0.1" in clause
    assert "instance:web-01" in clause
    assert " OR " in clause


def test_logs_scope_clause_quotes_unsafe_values():
    clause = build_logs_scope_clause(['evil" OR host:x'], [])
    assert 'evil\\"' in clause or 'evil"' in clause
    # 引号包裹后不应裸露成可注入的 OR 片段
    assert 'evil" OR host:x' not in clause


def test_logs_scope_clause_empty():
    assert build_logs_scope_clause([], []) == ""
    assert build_logs_scope_clause(["", " "], []) == ""


# ---------------------------- 无凭证新建 → pending ----------------------------


def test_create_requires_authentication_dependency():
    params = inspect.signature(create_asset).parameters
    assert "current_user" in params


def test_create_without_auth_marks_pending(monkeypatch):
    from types import SimpleNamespace

    import app.api.assets as assets_api

    created = {}

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class FakeDB:
        def query(self, *a, **k):
            return FakeQuery()

        def add(self, obj):
            created["obj"] = obj

        def flush(self):
            pass

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    monkeypatch.setattr(assets_api, "sync_host_group_membership", lambda *a, **k: None)
    monkeypatch.setattr(assets_api, "sync_credential_usage", lambda *a, **k: None)
    monkeypatch.setattr(assets_api, "test_ssh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应在无凭证时探测 SSH")))
    monkeypatch.setattr(assets_api, "set_collect_task_state", lambda *a, **k: None)
    monkeypatch.setattr(assets_api, "queue_collect_task", lambda *a, **k: True)

    payload = assets_api.AssetCreate(
        name="bare-host",
        ip="10.9.9.9",
        provider="on-premise",
        ssh_password="",
        credential_ref="",
    )
    result = assets_api.create_asset(
        payload,
        background_tasks=SimpleNamespace(add_task=lambda *a, **k: None),
        db=FakeDB(),
        current_user=SimpleNamespace(username="tester"),
        _=None,
    )

    assert result["type"] == "pending"
    assert "未录入或关联有效鉴权凭证" in result["status"][0]
    assert created["obj"].config_task_status in ("", "idle")


def test_provider_defaults_to_on_premise():
    from app.api.assets import AssetCreate

    asset = AssetCreate(name="x", ip="10.1.1.1")
    assert asset.provider == "on-premise"
    assert asset.region == "local"
