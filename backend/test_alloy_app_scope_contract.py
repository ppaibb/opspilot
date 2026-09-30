"""Alloy 策略注入应用归属标签的契约测试。

覆盖三件事：
1. `_resolve_app_scope_for_asset` 的三种返回分支：0 / 1 / 多应用；
2. `_ensure_asset_alloy_policy` 会把 `app_id` 写进策略 labels；
3. `resync_alloy_policy_for_asset_ids` 做 best-effort，监控配置缺失、资产不存在等都不会抛。
"""
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.monitoring import (
    _asset_policy_labels,
    _ensure_asset_alloy_policy,
    _resolve_app_scope_for_asset,
    resync_alloy_policy_for_asset_ids,
)
from app.core.crypto import encrypt_secret
from app.db.database import Base
from app.models.application import (
    Application,
    ApplicationResource,
)
from app.models.asset import Asset
from app.models.collector import (
    CollectorAgent,
    CollectorPolicy,
)
from app.models.system_config import SystemConfig


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _make_asset(db, *, instance_id: str, name: str = "host", ip: str = "10.0.0.1") -> Asset:
    asset = Asset(
        instance_id=instance_id,
        name=name,
        ip=ip,
        provider="manual",
        region="",
        status="online",
        type="healthy",
        ssh_user="root",
        ssh_port=22,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _bind(db, *, app_id: str, resource_id: str) -> None:
    db.add(
        ApplicationResource(
            app_id=app_id,
            resource_type="host",
            resource_id=resource_id,
            resource_name=resource_id,
        )
    )
    db.commit()


def _seed_apps(db, *ids: str) -> None:
    for app_id in ids:
        db.add(Application(
            app_id=app_id,
            name=app_id,
            environment="prod",
            status="active",
        ))
    db.commit()


class AppScopeResolutionTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.asset = _make_asset(self.db, instance_id="i-1")

    def test_no_binding_returns_empty(self):
        self.assertEqual(_resolve_app_scope_for_asset(self.db, self.asset), "")

    def test_single_binding_returns_app_id(self):
        _seed_apps(self.db, "APP-1")
        _bind(self.db, app_id="APP-1", resource_id="i-1")
        self.assertEqual(_resolve_app_scope_for_asset(self.db, self.asset), "APP-1")

    def test_multi_binding_returns_empty(self):
        """多应用共享主机时，标签保持空以避免指标归属错位。"""
        _seed_apps(self.db, "APP-1", "APP-2")
        _bind(self.db, app_id="APP-1", resource_id="i-1")
        _bind(self.db, app_id="APP-2", resource_id="i-1")
        self.assertEqual(_resolve_app_scope_for_asset(self.db, self.asset), "")


class EnsureAssetAlloyPolicyTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.asset = _make_asset(self.db, instance_id="i-2")
        self.user = SimpleNamespace(id=1, username="u1", name="U", role="管理员", permission_overrides="")
        self.agent = CollectorAgent(
            agent_id="agent-1",
            asset_id="i-2",
            hostname="agent-host",
            ip="10.0.0.2",
            os_type="linux",
            labels_json="{}",
            status="online",
            token_hash="x",
            created_at="2026-05-09 00:00:00",
            updated_at="2026-05-09 00:00:00",
        )
        self.db.add(self.agent)
        # 最小化监控配置：vm_url 和 vlogs_ingest_url 都配上，避免 render 抛错。
        self.db.add(SystemConfig(
            config_key="victoriametrics_config",
            config_value=json.dumps({
                "vm_url": "http://vm.example.com",
                "vlogs_ingest_url": "http://vl.example.com/insert/loki/api/v1/push",
            }),
            updated_at="2026-05-09 00:00:00",
        ))
        self.db.commit()

    def test_single_app_binding_adds_app_id_label(self):
        _seed_apps(self.db, "APP-77")
        _bind(self.db, app_id="APP-77", resource_id="i-2")

        _ensure_asset_alloy_policy(
            db=self.db,
            asset=self.asset,
            agent=self.agent,
            current_user=self.user,
        )

        policy = (
            self.db.query(CollectorPolicy)
            .filter(CollectorPolicy.scope_ref == self.agent.agent_id)
            .first()
        )
        self.assertIsNotNone(policy)
        labels = json.loads(policy.labels_json or "{}")
        self.assertEqual(labels.get("app_id"), "APP-77")
        # 基础 label 不应被覆盖。
        self.assertEqual(labels.get("asset_id"), "i-2")

    def test_no_app_binding_does_not_set_app_id_label(self):
        _ensure_asset_alloy_policy(
            db=self.db,
            asset=self.asset,
            agent=self.agent,
            current_user=self.user,
        )
        policy = (
            self.db.query(CollectorPolicy)
            .filter(CollectorPolicy.scope_ref == self.agent.agent_id)
            .first()
        )
        labels = json.loads(policy.labels_json or "{}")
        self.assertNotIn("app_id", labels)

    def test_default_policy_contains_metrics_and_logs(self):
        version = _ensure_asset_alloy_policy(
            db=self.db,
            asset=self.asset,
            agent=self.agent,
            current_user=self.user,
        )

        self.assertIn('prometheus.scrape "node"', version.alloy_config)
        self.assertIn('loki.write "victorialogs"', version.alloy_config)

    def test_policy_passes_monitoring_auth_to_alloy(self):
        row = self.db.query(SystemConfig).filter(SystemConfig.config_key == "victoriametrics_config").first()
        row.config_value = json.dumps({
            "vm_url": "https://vm.example.com",
            "vlogs_ingest_url": "https://vl.example.com/insert/loki/api/v1/push",
            "auth_type": "basic",
            "username": "grafana",
            "secret": encrypt_secret("cloud-token"),
        })
        self.db.commit()

        version = _ensure_asset_alloy_policy(
            db=self.db,
            asset=self.asset,
            agent=self.agent,
            current_user=self.user,
        )

        self.assertIn("basic_auth", version.alloy_config)
        self.assertIn('username = "grafana"', version.alloy_config)
        self.assertIn('password = "cloud-token"', version.alloy_config)


class ResyncBestEffortTest(unittest.TestCase):
    """验证 resync_alloy_policy_for_asset_ids 在边界条件下不抛异常。"""

    def setUp(self):
        self.db = _make_session()
        self.user = SimpleNamespace(id=1, username="u1", name="U", role="管理员", permission_overrides="")

    def test_empty_ids_is_noop(self):
        resync_alloy_policy_for_asset_ids(db=self.db, asset_ids=[], current_user=self.user)
        # 不抛异常即视为成功。

    def test_non_existent_asset_is_silently_skipped(self):
        resync_alloy_policy_for_asset_ids(db=self.db, asset_ids=["does-not-exist"], current_user=self.user)

    def test_asset_without_agent_is_silently_skipped(self):
        _make_asset(self.db, instance_id="i-3")
        # 没有 CollectorAgent，也没有 monitoring 配置，任一失败都应该被吞掉。
        resync_alloy_policy_for_asset_ids(db=self.db, asset_ids=["i-3"], current_user=self.user)


class BaseLabelsTest(unittest.TestCase):
    def test_base_labels_include_minimum_identity(self):
        asset = SimpleNamespace(
            instance_id="i-8",
            name="mall-api-1",
            hostname="",
            ip="192.168.10.8",
            provider="aliyun",
            region="cn-shenzhen",
        )
        labels = _asset_policy_labels(asset)
        self.assertEqual(labels["asset_id"], "i-8")
        self.assertEqual(labels["host"], "mall-api-1")
        self.assertEqual(labels["instance"], "192.168.10.8")
        self.assertEqual(labels["provider"], "aliyun")
        self.assertEqual(labels["region"], "cn-shenzhen")

    def test_host_label_prefers_real_hostname(self):
        """host 标签必须优先用真实 hostname，没有时才回退资产名。"""
        asset = SimpleNamespace(
            instance_id="i-9",
            name="mall-api-1",
            hostname="  mall-api-node-9  ",
            ip="192.168.10.9",
            provider="",
            region="",
        )
        labels = _asset_policy_labels(asset)
        self.assertEqual(labels["host"], "mall-api-node-9")
        self.assertNotIn("provider", labels)
        self.assertNotIn("region", labels)


if __name__ == "__main__":
    unittest.main()
