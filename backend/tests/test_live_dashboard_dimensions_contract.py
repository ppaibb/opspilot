"""实时看板六项监控维度契约测试（只增不减 / PromQL 降级）。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api.monitoring.constants import (
    DASHBOARD_RANGE_QUERIES,
    FS_SIZE_QUERY,
    FS_USED_PCT_TOP_QUERY,
    LOAD1_AVG_QUERY,
)
from app.api.monitoring.routes import HOST_METRIC_QUERIES
from app.api.monitoring.services.dashboard import (
    _fetch_summary_load1,
    _fetch_top_filesystems,
    _parse_instant_rows,
)


class LiveDashboardDimensionsContractTest(unittest.TestCase):
    """钉死六项补维度依赖的查询模板与解析契约。"""

    def test_dashboard_range_queries_include_load_and_disk_io(self) -> None:
        """全局 summary/时序要能取到 load1 与磁盘读写吞吐。"""
        for key in ("load1", "diskRead", "diskWrite", "cpu", "memory", "networkIn", "networkOut"):
            self.assertIn(key, DASHBOARD_RANGE_QUERIES)

        self.assertIn("node_load1", DASHBOARD_RANGE_QUERIES["load1"])
        self.assertIn("node_disk_read_bytes_total", DASHBOARD_RANGE_QUERIES["diskRead"])
        self.assertIn("node_disk_written_bytes_total", DASHBOARD_RANGE_QUERIES["diskWrite"])
        self.assertIn("rate(", DASHBOARD_RANGE_QUERIES["diskRead"])

    def test_host_metric_queries_include_load_and_disk_io(self) -> None:
        """对比模式 host-metrics 要补 load / disk 读写序列。"""
        from app.api.monitoring.services.dashboard import prom_instance_regex

        for key in ("load1", "diskRead", "diskWrite", "cpu", "memory", "networkIn", "networkOut"):
            self.assertIn(key, HOST_METRIC_QUERIES)

        host_re = prom_instance_regex("10.0.0.1")
        formatted = {
            key: tpl.format(host=host_re)
            for key, tpl in HOST_METRIC_QUERIES.items()
        }
        # instance 匹配必须锁到本机（可带 :port），但不能用前缀 .* 误命中 10.0.0.10
        self.assertIn(f'node_load1{{instance=~"{host_re}"}}', formatted["load1"])
        self.assertIn("(:.*)?", formatted["load1"])
        self.assertNotIn("10.0.0.1.*", formatted["load1"])
        self.assertIn("node_disk_read_bytes_total", formatted["diskRead"])
        self.assertIn("node_disk_written_bytes_total", formatted["diskWrite"])
        # 设备过滤应保留，避免 loop/ram 噪声
        self.assertIn('device!~"loop.*|ram.*"', formatted["diskRead"])

    def test_filesystem_top_queries(self) -> None:
        """文件系统 Top5 查询基于 node_filesystem_* 并过滤临时文件系统。"""
        self.assertIn("topk(5", FS_USED_PCT_TOP_QUERY)
        self.assertIn("node_filesystem_size_bytes", FS_USED_PCT_TOP_QUERY)
        self.assertIn("node_filesystem_avail_bytes", FS_USED_PCT_TOP_QUERY)
        self.assertIn("node_filesystem_size_bytes", FS_SIZE_QUERY)
        self.assertEqual(LOAD1_AVG_QUERY, "avg(node_load1)")

    def test_parse_instant_rows_keeps_labels(self) -> None:
        """instant 查询解析要保留 mountpoint 等标签，供 Top5 表拼装。"""
        payload = {
            "data": {
                "result": [
                    {
                        "metric": {
                            "instance": "10.0.0.1:9100",
                            "mountpoint": "/",
                            "device": "/dev/sda1",
                            "fstype": "ext4",
                        },
                        "value": [1700000000, "85.5"],
                    }
                ]
            }
        }
        rows = _parse_instant_rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric"]["mountpoint"], "/")
        self.assertEqual(rows[0]["value"], 85.5)

    def test_parse_instant_rows_tolerates_bad_payload(self) -> None:
        """非法 payload 返回空列表，不抛异常。"""
        self.assertEqual(_parse_instant_rows({}), [])
        self.assertEqual(_parse_instant_rows({"data": {"result": "oops"}}), [])

    def test_fetch_summary_load1_degrades_on_error(self) -> None:
        """Load1 查询失败返回 None，不 500。"""
        with patch(
            "app.api.monitoring.services.dashboard._vm_get",
            side_effect=RuntimeError("vm down"),
        ):
            self.assertIsNone(_fetch_summary_load1({"vm_url": "http://vm"}))

    def test_fetch_summary_load1_reads_value(self) -> None:
        with patch(
            "app.api.monitoring.services.dashboard._vm_get",
            return_value={
                "data": {"result": [{"metric": {}, "value": [1, "1.25"]}]}
            },
        ):
            self.assertEqual(_fetch_summary_load1({"vm_url": "http://vm"}), 1.25)

    def test_fetch_top_filesystems_returns_rows_and_joins_size(self) -> None:
        used_payload = {
            "data": {
                "result": [
                    {
                        "metric": {
                            "instance": "10.0.0.1:9100",
                            "mountpoint": "/data",
                            "device": "/dev/sdb1",
                            "fstype": "xfs",
                        },
                        "value": [1, "91.2"],
                    },
                    {
                        "metric": {
                            "instance": "10.0.0.2:9100",
                            "mountpoint": "/",
                            "device": "/dev/sda1",
                            "fstype": "ext4",
                        },
                        "value": [1, "40.0"],
                    },
                ]
            }
        }
        size_payload = {
            "data": {
                "result": [
                    {
                        "metric": {
                            "instance": "10.0.0.1:9100",
                            "mountpoint": "/data",
                            "device": "/dev/sdb1",
                        },
                        "value": [1, "1000"],
                    }
                ]
            }
        }

        def fake_vm_get(_cfg, path, params):
            if params.get("query") == FS_USED_PCT_TOP_QUERY:
                return used_payload
            return size_payload

        with patch(
            "app.api.monitoring.services.dashboard._vm_get",
            side_effect=fake_vm_get,
        ):
            rows = _fetch_top_filesystems({"vm_url": "http://vm"})

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["mountpoint"], "/data")
        self.assertEqual(rows[0]["used_pct"], 91.2)
        self.assertEqual(rows[0]["size_bytes"], 1000)
        self.assertEqual(rows[0]["used_bytes"], 912)
        # size 查询失败/缺失时容量字段为 None，仍然返回占用率
        self.assertIsNone(rows[1]["size_bytes"])

    def test_fetch_top_filesystems_degrades_on_error(self) -> None:
        with patch(
            "app.api.monitoring.services.dashboard._vm_get",
            side_effect=RuntimeError("vm down"),
        ):
            self.assertEqual(_fetch_top_filesystems({"vm_url": "http://vm"}), [])

    def test_recent_alert_shape_has_name_severity_instance_time(self) -> None:
        """最近告警卡片字段：名称/级别/实例/时间。"""
        with patch(
            "app.api.monitoring.services.dashboard.SessionLocal"
        ) as session_local:
            session_local.return_value = None
            # _fetch_alerts 在 db=None 时会自建 session；这里直接测字段拼装逻辑
            from app.api.monitoring.services.dashboard import _fetch_alerts

            class _Alert:
                fingerprint = "fp1"
                name = "OpsPilotNodeCpuHigh"
                instance = "10.0.0.1:9100"
                description = "cpu high"
                active_at = "2026-01-01 00:00:00"
                severity = "warning"
                state = "firing"
                labels = "{}"

            class _Query:
                def order_by(self, *_a, **_k):
                    return self

                def all(self):
                    return [_Alert()]

            class _Db:
                def query(self, *_a, **_k):
                    return _Query()

                def rollback(self):
                    return None

                def close(self):
                    return None

            session_local.return_value = _Db()
            alerts = _fetch_alerts({"vm_url": "http://vm"})
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["name"], "OpsPilotNodeCpuHigh")
            self.assertEqual(alerts[0]["instance"], "10.0.0.1:9100")
            self.assertEqual(alerts[0]["severity"], "warning")
            self.assertEqual(alerts[0]["time"], "2026-01-01 00:00:00")
            # 旧字段仍在（只增不减）
            self.assertEqual(alerts[0]["rule"], "OpsPilotNodeCpuHigh")


if __name__ == "__main__":
    unittest.main()
