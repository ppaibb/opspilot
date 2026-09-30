"""CMDB 关联图谱 + SSH 指纹列表 + 账单 CSV 导出契约。"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from app.api import cmdb
from app.models.application import Application, ApplicationResource
from app.models.asset import Asset
from app.models.asset_host_key import AssetHostKey


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

    def order_by(self, *args, **kwargs):
        return self

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def delete(self, *args, **kwargs):
        n = len(self._rows)
        self._rows.clear()
        return n


class FakeDB:
    def __init__(self, mapping):
        self._mapping = mapping

    def query(self, *entities):
        key = entities[0]
        name = getattr(key, "__name__", str(key))
        rows = self._mapping.get(name, [])
        return FakeQuery(rows)


class CmdbGraphTest(unittest.TestCase):
    def _db(self):
        app = Application(
            app_id="app-1",
            name="订单服务",
            owner="张三",
            environment="prod",
            category="业务应用",
            status="active",
            tags=["核心"],
        )
        asset = Asset(instance_id="i-1", name="web-1", ip="10.0.0.1", type="healthy", group_id=None)
        binding = ApplicationResource(
            app_id="app-1",
            resource_type="host",
            resource_id="i-1",
            resource_name="web-1",
        )
        return FakeDB({
            "Application": [app],
            "Asset": [asset],
            "VirtualMachine": [],
            "DomainRecord": [],
            "CertificateRecord": [],
            "ApplicationResource": [binding],
        })

    def test_graph_contains_app_and_host_with_edge(self):
        result = cmdb.get_cmdb_graph(
            environment="",
            owner="",
            db=self._db(),
            _=None,
            __=None,
        )
        node_ids = {n["id"] for n in result["nodes"]}
        self.assertIn("app-1", node_ids)
        self.assertIn("host:i-1", node_ids)
        self.assertEqual(result["total_edges"], 1)
        edge = result["edges"][0]
        self.assertEqual(edge["source"], "app-1")
        self.assertEqual(edge["target"], "host:i-1")

    def test_graph_filters_by_environment(self):
        from unittest.mock import MagicMock, patch

        mock_db = MagicMock()
        q = MagicMock()
        mock_db.query.return_value = q
        q.filter.return_value = q
        q.all.return_value = []
        result = cmdb.get_cmdb_graph(
            environment="test",
            owner="",
            db=mock_db,
            _=None,
            __=None,
        )
        self.assertTrue(q.filter.called)
        self.assertEqual(result["total_nodes"], 0)
        self.assertEqual(result["total_edges"], 0)

    def test_impact_by_host_returns_app_and_owner(self):
        result = cmdb.get_cmdb_impact(
            type="host",
            id="i-1",
            db=self._db(),
            _=None,
            __=None,
        )
        self.assertEqual(result["node"]["id"], "i-1")
        self.assertEqual(result["owners"], ["张三"])
        self.assertEqual(result["impact_summary"]["app_count"], 1)

    def test_impact_by_application_lists_resources(self):
        result = cmdb.get_cmdb_impact(
            type="application",
            id="app-1",
            db=self._db(),
            _=None,
            __=None,
        )
        self.assertEqual(len(result["resources"]), 1)
        self.assertEqual(result["resources"][0]["id"], "i-1")


class HostKeyContractTest(unittest.TestCase):
    def test_asset_host_key_fields(self):
        row = AssetHostKey(
            host="10.0.0.1",
            port=22,
            key_type="ssh-ed25519",
            fingerprint="SHA256:abc",
            first_seen_at="2026-09-23 10:00:00",
            last_seen_at="2026-09-23 11:00:00",
        )
        self.assertEqual(row.host, "10.0.0.1")
        self.assertEqual(row.fingerprint, "SHA256:abc")
        self.assertTrue(hasattr(row, "first_seen_at"))


class BillCsvExportContractTest(unittest.TestCase):
    def test_export_route_registered(self):
        from app.api.finance import router

        paths = [getattr(r, "path", "") for r in router.routes]
        self.assertIn("/bills/export", paths)


if __name__ == "__main__":
    unittest.main()
