"""证书到期告警扫描器测试 — 验证事件构造正确,且全程不触发真实发送。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import app.services.alert_engine.cert_expiry_alert as cea


def _fake_cert(domain: str, days_from_now: int, renew_before_days: int = 30):
    not_after = (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d %H:%M:%S")
    return SimpleNamespace(
        cert_id=f"cert-{domain}",
        name=domain,
        primary_domain=domain,
        status="issued",
        renew_before_days=renew_before_days,
        not_after=not_after,
    )


class _FakeQuery:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeDB:
    def __init__(self, items):
        self._items = items

    def query(self, _model):
        return _FakeQuery(self._items)


def test_cert_events_firing_for_expiring(monkeypatch):
    # 兜底:任何真实发送都不允许被调用
    import app.api.monitoring.services.alert_dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, "send_alert_to_channel", lambda *a, **k: pytest_fail())

    certs = [
        _fake_cert("expiring.example.com", days_from_now=5),    # 临近到期 -> warning firing
        _fake_cert("expired.example.com", days_from_now=-3),    # 已过期 -> critical firing
        _fake_cert("healthy.example.com", days_from_now=200),   # 正常 -> resolved
    ]
    events = cea._build_cert_events(_FakeDB(certs))
    by_instance = {e["labels"]["instance"]: e for e in events}

    assert by_instance["expiring.example.com"]["status"] == "firing"
    assert by_instance["expiring.example.com"]["labels"]["severity"] == "warning"
    assert by_instance["expired.example.com"]["status"] == "firing"
    assert by_instance["expired.example.com"]["labels"]["severity"] == "critical"
    assert by_instance["healthy.example.com"]["status"] == "resolved"
    # 规则标识贯穿,便于前端按 opspilot_rule_id 归类
    for e in events:
        assert e["labels"].get("opspilot_rule_id") == cea.CERT_EXPIRY_RULE_ID


def pytest_fail():  # pragma: no cover
    raise AssertionError("send_alert_to_channel 不应在事件构造阶段被调用")
