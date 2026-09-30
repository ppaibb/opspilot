import pytest
from datetime import datetime, timedelta
from app.services.alert_engine.engine import parse_duration, series_fingerprint, evaluate_once
from app.models.monitoring_shadow_alert import MonitoringShadowAlert

def test_parse_duration():
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("") == 300
    assert parse_duration("invalid") == 300

def test_series_fingerprint():
    fp1 = series_fingerprint("rule-1", {"instance": "A", "job": "B"})
    fp2 = series_fingerprint("rule-1", {"job": "B", "instance": "A"})
    assert fp1 == fp2
    fp3 = series_fingerprint("rule-2", {"instance": "A", "job": "B"})
    assert fp1 != fp3

class DummyRow:
    def __init__(self, fp):
        self.fingerprint = fp
        self.state = "pending"
        self.first_breach_at = datetime.utcnow() - timedelta(minutes=6)
        self.active_at = None
        self.resolved_at = None

def test_evaluate_once_mocked(monkeypatch):
    class MockConfigStore:
        @staticmethod
        def read_monitoring_config(db):
            return {"vm_url": "http://mock:8428"}
            
    class MockRules:
        @staticmethod
        def read_monitoring_rules(db):
            return [{"id": "rule-1", "expr": "up == 0", "duration": "5m", "enabled": True}], "mock_time"
            
    monkeypatch.setattr("app.services.alert_engine.engine.read_monitoring_config", MockConfigStore.read_monitoring_config)
    monkeypatch.setattr("app.services.alert_engine.engine.read_monitoring_rules", MockRules.read_monitoring_rules)
    
    mock_vm_results = [
        {"metric": {"instance": "host1"}, "value": [1600000000, "1"]}
    ]
    def mock_vm_query(cfg, expr):
        return mock_vm_results
        
    monkeypatch.setattr("app.services.alert_engine.engine.vm_query", mock_vm_query)
    
    class MockQuery:
        def __init__(self, data=None):
            self.data = data or []
        def filter(self, *args, **kwargs):
            return self
        def all(self):
            return self.data
            
    class MockSession:
        def __init__(self):
            self.added = []
            self.committed = False
            self.rows = []
        def query(self, model):
            return MockQuery(self.rows)
        def add(self, obj):
            self.added.append(obj)
            self.rows.append(obj)
        def commit(self):
            self.committed = True
        def rollback(self):
            pass

    # First run: should transition to pending
    db = MockSession()
    stats = evaluate_once(db)
    assert stats["pending"] == 1
    assert stats["firing"] == 0
    assert len(db.added) == 1
    row = db.added[0]
    assert row.state == "pending"
    assert row.first_breach_at is not None

    # Second run immediately: should stay pending
    db.rows = [row] # Mock existing row
    stats2 = evaluate_once(db)
    assert stats2["pending"] == 1
    assert stats2["firing"] == 0
    assert row.state == "pending"

    # Fast forward time to exceed duration (5m)
    row.first_breach_at = datetime.utcnow() - timedelta(minutes=6)
    stats3 = evaluate_once(db)
    assert stats3["firing"] == 1
    assert stats3["pending"] == 0
    assert row.state == "firing"
    assert row.active_at is not None

    # Resolve: no breaching series returned by VM
    mock_vm_results.clear()
    stats4 = evaluate_once(db)
    assert stats4["resolved"] == 1
    assert row.state == "resolved"
    assert row.resolved_at is not None
