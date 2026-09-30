"""Alloy import.http 远程配置接入契约测试。"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.collector import get_agent_module
from app.db.database import Base
from app.models.collector import CollectorAgent, CollectorConfigVersion
from app.services.collector_bootstrap import render_alloy_bootstrap_config, render_alloy_install_script
from app.services.collector_config import render_alloy_module_config, wrap_alloy_config_as_module


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class AlloyRemoteConfigContractTest(unittest.TestCase):
    """验证新接入链路不再依赖 opspilot-alloy-sync。"""

    def test_bootstrap_uses_import_http_and_env_token(self):
        config = render_alloy_bootstrap_config(base_url="http://opspilot.local", poll_frequency="60s")

        self.assertIn('import.http "opspilot"', config)
        self.assertIn('"http://opspilot.local/api/monitoring/collector/agent/module"', config)
        self.assertIn('sys.env("OPSPILOT_COLLECTOR_TOKEN")', config)
        self.assertIn('opspilot.main "default" {}', config)

    def test_install_script_does_not_create_sync_service(self):
        script = render_alloy_install_script()

        self.assertIn("/etc/alloy/config.alloy", script)
        self.assertIn("OPSPILOT_POLL_FREQUENCY", script)
        self.assertIn("systemctl restart alloy", script)
        self.assertNotIn("cat >/usr/local/bin/opspilot-alloy-sync", script)
        self.assertNotIn("opspilot-alloy-sync.service <<", script)

    def test_remote_config_is_wrapped_as_module(self):
        module = render_alloy_module_config(
            agent_id="agent-1",
            metrics={"node": True},
            logs={"system": True},
            labels={"asset_id": "i-1", "host": "host-1", "instance": "10.0.0.1"},
            remote_write_url="http://vm.example.com/api/v1/write",
            vlogs_url="http://vl.example.com/insert/loki/api/v1/push",
        )

        self.assertTrue(module.startswith('declare "main" {'))
        self.assertIn('prometheus.remote_write "opspilot"', module)
        self.assertIn('loki.write "victorialogs"', module)
        self.assertEqual(module, wrap_alloy_config_as_module(module))

    def test_remote_write_and_loki_auth_are_rendered(self):
        module = render_alloy_module_config(
            agent_id="agent-auth",
            metrics={"node": True},
            logs={"system": True},
            labels={"asset_id": "i-auth", "host": "host-auth", "instance": "10.0.0.3"},
            remote_write_url="https://vm.example.com/api/v1/write",
            vlogs_url="https://vl.example.com/insert/loki/api/v1/push",
            remote_write_auth={"type": "basic", "username": "u1", "password": "p1"},
            vlogs_auth={"type": "bearer", "token": "tok-1"},
        )

        self.assertIn("basic_auth", module)
        self.assertIn('username = "u1"', module)
        self.assertIn('password = "p1"', module)
        self.assertIn('bearer_token = "tok-1"', module)

    def test_metrics_and_logs_can_be_disabled_independently(self):
        logs_only = render_alloy_module_config(
            agent_id="agent-logs",
            metrics={"node": False},
            logs={"system": True, "docker_containers": False},
            labels={"asset_id": "i-logs", "host": "host-logs", "instance": "10.0.0.4"},
            remote_write_url="",
            vlogs_url="https://vl.example.com/insert/loki/api/v1/push",
        )
        metrics_only = render_alloy_module_config(
            agent_id="agent-metrics",
            metrics={"node": True},
            logs={"system": False, "docker_containers": False},
            labels={"asset_id": "i-metrics", "host": "host-metrics", "instance": "10.0.0.5"},
            remote_write_url="https://vm.example.com/api/v1/write",
            vlogs_url="",
        )

        self.assertIn('loki.write "victorialogs"', logs_only)
        self.assertNotIn('prometheus.scrape "node"', logs_only)
        self.assertIn('prometheus.scrape "node"', metrics_only)
        self.assertNotIn('loki.write "victorialogs"', metrics_only)

    def test_agent_module_endpoint_updates_last_seen(self):
        db = _make_session()
        agent = CollectorAgent(
            agent_id="agent-remote",
            asset_id="i-remote",
            hostname="host",
            ip="10.0.0.2",
            os_type="linux",
            labels_json="{}",
            status="offline",
            current_version_id="cfg-remote",
            token_hash="x",
            created_at="",
            updated_at="",
        )
        db.add(agent)
        db.add(
            CollectorConfigVersion(
                version_id="cfg-remote",
                policy_id="pol-remote",
                version=1,
                config_hash="hash",
                alloy_config='prometheus.remote_write "opspilot" {\n  endpoint { url = "http://vm/api/v1/write" }\n}\n',
                status="published",
                created_by="tester",
                created_at="",
            )
        )
        db.commit()
        db.refresh(agent)

        response = get_agent_module(agent=agent, db=db)
        body = response.body.decode("utf-8")

        self.assertIn('declare "main"', body)
        self.assertEqual("online", agent.status)
        self.assertTrue(agent.last_seen_at)


if __name__ == "__main__":
    unittest.main()
