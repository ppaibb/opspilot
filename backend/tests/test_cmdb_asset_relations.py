"""CMDB 资产间派生边：证书/域名/主机/凭据/应用-凭据 + impact neighbors。"""

from __future__ import annotations

import json
import unittest

from app.api import cmdb
from app.models.application import Application, ApplicationConfig, ApplicationResource
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord
from app.models.certificate_deployment import CertificateDeployment
from app.models.credential import Credential


class FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def count(self):
        return len(self._rows)


class FilteringQuery(FakeQuery):
    """对 Application / ApplicationResource 的 == / contains / in_ 做真实过滤，
    让应用过滤测试能跑在 FakeDB 上。"""

    def filter(self, *args, **kwargs):
        rows = self._rows
        for clause in args:
            left = getattr(clause, "left", None)
            right = getattr(clause, "right", None)
            if left is None or right is None:
                continue
            key = getattr(left, "key", None)
            val = getattr(right, "value", None)
            if key is None:
                continue
            if isinstance(val, (list, tuple, set)):
                rows = [r for r in rows if getattr(r, key, None) in val]
            elif isinstance(val, str) and val.startswith("%") and val.endswith("%"):
                rows = [r for r in rows if val.strip("%") in (getattr(r, key, "") or "")]
            else:
                rows = [r for r in rows if getattr(r, key, None) == val]
        return FilteringQuery(rows)


class FakeDB:
    def __init__(self, mapping):
        self._mapping = mapping

    def query(self, *entities):
        key = entities[0]
        name = getattr(key, "__name__", str(key))
        rows = self._mapping.get(name, [])
        if name in ("Application", "ApplicationResource"):
            return FilteringQuery(rows)
        return FakeQuery(rows)


def _cred(cred_id, name=None):
    return Credential(cred_id=cred_id, name=name or cred_id, type="Token", status="active")


def _domain(db_id, domain, root_domain, account_id=""):
    return DomainRecord(
        id=db_id,
        domain=domain,
        root_domain=root_domain,
        account_id=account_id,
        status="active",
        record_value="1.2.3.4",
    )


def _cert(cert_id, primary, san="", account_id=""):
    return CertificateRecord(
        cert_id=cert_id,
        name=cert_id,
        primary_domain=primary,
        san_domains=san,
        account_id=account_id,
        status="issued",
    )


def _host(instance_id, credential_ref=None):
    return Asset(
        instance_id=instance_id,
        name=instance_id,
        ip="10.0.0.1",
        type="healthy",
        group_id=None,
        credential_ref=credential_ref,
    )


def _deploy(cert_id, target_type="host", results=None):
    return CertificateDeployment(
        cert_id=cert_id,
        target_type=target_type,
        results_json=json.dumps(results if results is not None else []),
    )


def _base_db(**overrides):
    """0 应用 + 1 证书 + 2 域名 + 3 主机 + 3 凭据的基准场景。"""
    mapping = {
        "Application": [],
        "ApplicationResource": [],
        "ApplicationConfig": [],
        "Asset": [
            _host("h1", "cred-ssh"),
            _host("h2", "cred-ssh"),
            _host("h3"),
        ],
        "VirtualMachine": [],
        "DomainRecord": [
            _domain(1, "example.com", "example.com", "cred-dns"),
            _domain(2, "api.example.com", "example.com", "cred-dns"),
        ],
        "CertificateRecord": [
            _cert("cert-1", "example.com", "api.example.com", "cred-acme"),
        ],
        "Credential": [_cred("cred-acme"), _cred("cred-dns"), _cred("cred-ssh")],
        "CertificateDeployment": [
            _deploy("cert-1", results=[
                {"asset_id": "h1", "success": True, "message": "ok"},
                {"asset_id": "h2", "success": True, "message": "ok"},
            ]),
        ],
    }
    mapping.update(overrides)
    return FakeDB(mapping)


def _graph(db, environment="", owner=""):
    return cmdb.get_cmdb_graph(environment=environment, owner=owner, db=db, _=None, __=None)


def _impact(db, type_, id_):
    return cmdb.get_cmdb_impact(type=type_, id=id_, db=db, _=None, __=None)


def _edge_set(result):
    return {(e["source"], e["target"], e["kind"]) for e in result["edges"]}


def _neighbor_set(result):
    return {
        (n["type"], n["id"], n["kind"], n["direction"]) for n in result["neighbors"]
    }


class AssetRelationsGraphTest(unittest.TestCase):
    def test_no_apps_graph_still_nonempty_with_correct_kinds(self):
        result = _graph(_base_db())
        node_ids = {n["id"] for n in result["nodes"]}
        self.assertEqual(node_ids, {
            "certificate:cert-1",
            "domain:1", "domain:2",
            "host:h1", "host:h2",
            "credential:cred-acme", "credential:cred-dns", "credential:cred-ssh",
        })
        self.assertEqual(result["total_nodes"], 8)
        # h3 无凭据引用、无部署、无绑定，不作为任何边的端点，故不出现在图中
        edges = _edge_set(result)
        self.assertEqual(edges, {
            ("certificate:cert-1", "domain:1", "issued_for"),
            ("certificate:cert-1", "domain:2", "issued_for"),
            ("certificate:cert-1", "credential:cred-acme", "acme_account"),
            ("domain:1", "credential:cred-dns", "dns_account"),
            ("domain:2", "credential:cred-dns", "dns_account"),
            ("host:h1", "credential:cred-ssh", "ssh_credential"),
            ("host:h2", "credential:cred-ssh", "ssh_credential"),
            ("certificate:cert-1", "host:h1", "deployed_to"),
            ("certificate:cert-1", "host:h2", "deployed_to"),
        })
        # 同一凭据被多方引用只出现一次
        self.assertEqual(
            sum(1 for n in result["nodes"] if n["id"] == "credential:cred-dns"), 1
        )

    def test_wildcard_domain_matching(self):
        db = _base_db(
            DomainRecord=[
                _domain(1, "example.com", "example.com"),
                _domain(2, "api.example.com", "example.com"),
                _domain(3, "other.com", "other.com"),
                _domain(4, "deep.api.example.com", "example.com"),
            ],
            CertificateRecord=[_cert("cert-1", "*.example.com")],
            CertificateDeployment=[],
        )
        edges = _edge_set(_graph(db))
        issued = {(s, t) for s, t, k in edges if k == "issued_for"}
        self.assertEqual(issued, {
            ("certificate:cert-1", "domain:1"),
            ("certificate:cert-1", "domain:2"),
            ("certificate:cert-1", "domain:4"),
        })

    def test_non_wildcard_does_not_match_subdomain(self):
        db = _base_db(
            DomainRecord=[_domain(1, "api.example.com", "example.com")],
            CertificateRecord=[_cert("cert-1", "example.com")],
            CertificateDeployment=[],
        )
        edges = _edge_set(_graph(db))
        self.assertFalse(any(k == "issued_for" for _, _, k in edges))

    def test_missing_credential_draws_no_edge_or_node(self):
        db = _base_db(
            CertificateRecord=[_cert("cert-1", "example.com", "", "cred-gone")],
            DomainRecord=[_domain(1, "example.com", "example.com", "cred-gone")],
            Asset=[_host("h1", "cred-gone")],
            Credential=[],
            CertificateDeployment=[],
        )
        result = _graph(db)
        self.assertFalse(any("credential:" in e["target"] for e in result["edges"]))
        self.assertFalse(any(n["type"] == "credential" for n in result["nodes"]))

    def test_deployed_to_dedup_and_skip_rows_without_asset_id(self):
        db = _base_db(
            CertificateDeployment=[
                _deploy("cert-1", results=[
                    {"asset_id": "h1", "success": True, "message": "ok"},
                    {"target": "h1", "success": True, "message": "无 asset_id 跳过"},
                ]),
                _deploy("cert-1", results=[
                    {"asset_id": "h1", "success": True, "message": "重复部署"},
                ]),
                _deploy("cert-1", target_type="cloud", results=[
                    {"asset_id": "h2", "success": True, "message": "非 host 行跳过"},
                ]),
            ],
        )
        edges = _edge_set(_graph(db))
        deployed = {(s, t) for s, t, k in edges if k == "deployed_to"}
        self.assertEqual(deployed, {("certificate:cert-1", "host:h1")})

    def test_deployed_to_skips_failed_deployments(self):
        db = _base_db(
            CertificateDeployment=[
                _deploy("cert-1", results=[
                    {"asset_id": "h1", "success": False, "message": "部署失败"},
                    {"asset_id": "h2", "success": True, "message": "ok"},
                ]),
            ],
        )
        edges = _edge_set(_graph(db))
        deployed = {(s, t) for s, t, k in edges if k == "deployed_to"}
        self.assertEqual(deployed, {("certificate:cert-1", "host:h2")})

    def test_binds_and_asset_edges_coexist(self):
        app = Application(
            app_id="app-1", name="订单服务", owner="张三",
            environment="prod", category="业务应用", status="active", tags=[],
        )
        binding = ApplicationResource(
            app_id="app-1", resource_type="host", resource_id="h1", resource_name="h1",
        )
        result = _graph(_base_db(
            Application=[app], ApplicationResource=[binding],
        ))
        edges = _edge_set(result)
        self.assertIn(("app-1", "host:h1", "binds"), edges)
        self.assertIn(("certificate:cert-1", "host:h1", "deployed_to"), edges)
        self.assertIn("app-1", {n["id"] for n in result["nodes"]})

    def test_app_filter_does_not_affect_asset_edges(self):
        app = Application(
            app_id="app-1", name="订单服务", owner="张三",
            environment="test", category="业务应用", status="active", tags=[],
        )
        binding = ApplicationResource(
            app_id="app-1", resource_type="host", resource_id="h1", resource_name="h1",
        )
        db = _base_db(Application=[app], ApplicationResource=[binding])
        filtered = _graph(db, environment="prod")
        node_ids = {n["id"] for n in filtered["nodes"]}
        self.assertNotIn("app-1", node_ids)
        self.assertFalse(any(e["kind"] == "binds" for e in filtered["edges"]))
        self.assertIn(
            ("certificate:cert-1", "domain:1", "issued_for"),
            _edge_set(filtered),
        )
        self.assertIn(
            ("certificate:cert-1", "host:h1", "deployed_to"),
            _edge_set(filtered),
        )
        unfiltered = _graph(db, environment="test")
        self.assertIn(("app-1", "host:h1", "binds"), _edge_set(unfiltered))

    def test_app_credential_edge_only_for_credential_ref_configs(self):
        app = Application(
            app_id="app-1", name="订单服务", owner="张三",
            environment="prod", category="业务应用", status="active", tags=[],
        )
        configs = [
            ApplicationConfig(
                app_id="app-1", config_id="cfg-1", key="db_pwd",
                value_type="credential_ref", credential_ref="cred-app",
            ),
            ApplicationConfig(
                app_id="app-1", config_id="cfg-2", key="plain_key",
                value_type="plain", credential_ref="cred-app",
            ),
            ApplicationConfig(
                app_id="app-1", config_id="cfg-3", key="gone",
                value_type="credential_ref", credential_ref="cred-gone",
            ),
        ]
        db = _base_db(
            Application=[app],
            ApplicationConfig=configs,
            Credential=[_cred("cred-app")],
        )
        edges = _edge_set(_graph(db))
        app_creds = {(s, t) for s, t, k in edges if k == "app_credential"}
        self.assertEqual(app_creds, {("app-1", "credential:cred-app")})


class ImpactNeighborsTest(unittest.TestCase):
    def test_impact_certificate_neighbors(self):
        result = _impact(_base_db(), "certificate", "cert-1")
        self.assertEqual(result["node"]["id"], "cert-1")
        self.assertEqual(_neighbor_set(result), {
            ("domain", "1", "issued_for", "out"),
            ("domain", "2", "issued_for", "out"),
            ("credential", "cred-acme", "acme_account", "out"),
            ("host", "h1", "deployed_to", "out"),
            ("host", "h2", "deployed_to", "out"),
        })
        # 原有字段语义不变
        self.assertEqual(result["apps"], [])
        self.assertEqual(result["impact_summary"]["app_count"], 0)

    def test_impact_credential_neighbors(self):
        app = Application(
            app_id="app-1", name="订单服务", owner="张三",
            environment="prod", category="业务应用", status="active", tags=[],
        )
        cfg = ApplicationConfig(
            app_id="app-1", config_id="cfg-1", key="k",
            value_type="credential_ref", credential_ref="cred-ssh",
        )
        result = _impact(
            _base_db(Application=[app], ApplicationConfig=[cfg]),
            "credential", "cred-ssh",
        )
        self.assertEqual(_neighbor_set(result), {
            ("host", "h1", "ssh_credential", "in"),
            ("host", "h2", "ssh_credential", "in"),
            ("application", "app-1", "app_credential", "in"),
        })
        # 凭据节点本身的 neighbors 也覆盖证书/域名引用
        result = _impact(_base_db(), "credential", "cred-acme")
        self.assertIn(
            ("certificate", "cert-1", "acme_account", "in"), _neighbor_set(result)
        )
        result = _impact(_base_db(), "credential", "cred-dns")
        self.assertIn(
            ("domain", "1", "dns_account", "in"), _neighbor_set(result)
        )
        self.assertIn(
            ("domain", "2", "dns_account", "in"), _neighbor_set(result)
        )

    def test_impact_domain_neighbors(self):
        result = _impact(_base_db(), "domain", "1")
        self.assertEqual(_neighbor_set(result), {
            ("certificate", "cert-1", "issued_for", "in"),
            ("credential", "cred-dns", "dns_account", "out"),
        })

    def test_impact_host_neighbors(self):
        result = _impact(_base_db(), "host", "h1")
        self.assertEqual(_neighbor_set(result), {
            ("certificate", "cert-1", "deployed_to", "in"),
            ("credential", "cred-ssh", "ssh_credential", "out"),
        })
        self.assertEqual(result["owners"], [])


if __name__ == "__main__":
    unittest.main()
