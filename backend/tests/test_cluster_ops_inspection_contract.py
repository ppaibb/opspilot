"""集群巡检修复契约测试（2026-10-08 集群管理专项巡检）。

钉死四类修复，防回归：
1. 核心组件健康：外置占位行不带真实 Pod 名（前端据此渲染「—」）；
   componentstatuses 命中占位行时覆盖而非追加，避免「etcd 外置 + etcd-0 正常」两行矛盾；
2. 集群子路径写操作（cordon/drain/apply/pod 删除等）在审计风险规则表里的等级；
3. 集群审计接口返回：naive UTC 转本地时间、action 只留方法标签、resource 去前缀；
4. 风险短键等级：cordon/drain/set-image 等注册为高危。
"""
import datetime
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api import clusters as clusters_module
from app.api.clusters import cluster_mgmt
from app.main import resolve_request_risk_level
from app.timefmt import to_local_str


def _fake_pod(name: str, phase: str = "Running", ready: bool = True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            phase=phase,
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
        ),
    )


def _fake_component_status(name: str, healthy: bool = True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        conditions=[SimpleNamespace(status="True" if healthy else "False")],
    )


class _FakeCoreV1:
    """按用例装配返回值的 CoreV1Api 替身。"""

    def __init__(self, pods=None, component_statuses=None):
        self._pods = pods or []
        self._cs = component_statuses or []

    def list_namespaced_pod(self, namespace=None, _request_timeout=None):
        return SimpleNamespace(items=self._pods)

    def list_component_status(self, _request_timeout=None):
        return SimpleNamespace(items=self._cs)


class CoreComponentsClassificationTest(unittest.TestCase):
    """_classify_core_components / componentstatuses 覆盖逻辑。"""

    def test_placeholder_rows_carry_no_pod_name(self):
        """外置/未发现占位行的 name 必须等于 component（前端据此不渲染 Pod 名）。"""
        components = cluster_mgmt._classify_core_components([
            _fake_pod("coredns-c6c64fd-bxv5v"),
        ])
        by_component = {c["component"]: c for c in components}
        self.assertEqual(by_component["apiserver"]["name"], "apiserver")
        self.assertEqual(by_component["apiserver"]["status"], "external")
        self.assertEqual(by_component["coredns"]["status"], "healthy")

    def test_component_status_overrides_external_placeholder(self):
        """componentstatuses 报告的 etcd-0 必须覆盖「etcd 外置」占位行，而不是并存两行。"""
        fake_v1 = _FakeCoreV1(
            pods=[_fake_pod("coredns-c6c64fd-bxv5v")],
            component_statuses=[_fake_component_status("etcd-0", healthy=True)],
        )
        with patch.object(cluster_mgmt, "_shared") as fake_shared:
            fake_shared._get_api_client.return_value = object()
            fake_shared._get_or_set_k8s_read_cache = (
                lambda cid, op, params, loader, **kwargs: loader()
            )
            with patch.object(cluster_mgmt, "client") as fake_client:
                fake_client.CoreV1Api.return_value = fake_v1
                components = cluster_mgmt.get_cluster_components_health("cid-x")

        etcd_rows = [c for c in components if c["component"] == "etcd"]
        self.assertEqual(len(etcd_rows), 1)
        self.assertEqual(etcd_rows[0]["name"], "etcd-0")
        self.assertEqual(etcd_rows[0]["status"], "healthy")
        # 未被 componentstatuses 覆盖的仍是外置占位，但 Pod 列不得显示成 Pod 名。
        apiserver = next(c for c in components if c["component"] == "apiserver")
        self.assertEqual(apiserver["name"], "apiserver")

    def test_match_component_target_maps_instances(self):
        match = cluster_mgmt._match_component_target
        self.assertEqual(match("etcd-0"), "etcd")
        self.assertEqual(match("kube-apiserver-k8s-01"), "apiserver")
        self.assertEqual(match("coredns-autoscaler-abc"), "coredns")
        self.assertIsNone(match("canal-6s2l7"))


class ClusterAuditShapeTest(unittest.TestCase):
    """集群审计接口的展示层适配（时间本地化 / 动作对象拆分 / 筛选分页信封）。"""

    def _run(self, logs, **kwargs):
        class _Query:
            def filter(self, *_a, **_k):
                return self

            def order_by(self, *_a, **_k):
                return self

            def offset(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            def count(self):
                return len(logs)

            def all(self):
                return logs

        result = clusters_module.get_cluster_audit_logs(
            "cid-1", db=SimpleNamespace(query=lambda *_a, **_k: _Query()), **kwargs,
        )
        return result

    def test_audit_row_is_localized_and_split(self):
        raw_time = datetime.datetime(2026, 10, 8, 8, 28, 3)
        logs = [
            SimpleNamespace(
                id=20389,
                time=raw_time,
                user="系统管理员",
                action="创建 /api/clusters/cid-1/nodes/k8s-01/cordon",
                resource="/api/clusters/cid-1/nodes/k8s-01/cordon",
                ip="10.0.0.2",
                result="success",
                risk_level="high",
                risk_reason="集群运维动作",
            )
        ]
        result = self._run(logs)
        # 信封：items + total（前端服务端分页要 total）
        self.assertEqual(result["total"], 1)
        row = result["items"][0]
        # naive UTC → 本地时间串
        self.assertEqual(row["time"], to_local_str(raw_time))
        # action 只剩方法标签，resource 去前缀，path 保留完整路径
        self.assertEqual(row["action"], "创建")
        self.assertEqual(row["resource"], "/nodes/k8s-01/cordon")
        self.assertEqual(row["path"], "/api/clusters/cid-1/nodes/k8s-01/cordon")
        # 二轮补齐：与全局审计接口对齐的两个字段
        self.assertEqual(row["ip"], "10.0.0.2")
        self.assertEqual(row["risk_reason"], "集群运维动作")

    def test_non_middleware_resource_is_kept_as_is(self):
        """服务侧写的审计（resource 不是请求路径）原样保留，不猜结构。"""
        logs = [
            SimpleNamespace(
                id=1, time=None, user="admin", action="审批通过",
                resource="工单 #12", ip="", result="success", risk_level="medium",
                risk_reason="",
            )
        ]
        row = self._run(logs)["items"][0]
        self.assertEqual(row["action"], "审批通过")
        self.assertEqual(row["resource"], "工单 #12")
        self.assertEqual(row["time"], "")

    def test_filters_and_pagination_params_are_accepted(self):
        """筛选/分页参数全部可传，且时间参数非法时静默忽略而不是把范围卡死。"""
        result = self._run(
            [],
            user="系统管理员", action="创建", result="failed",
            risk_level="high", q="k8s-01", time_from="2026-10-08 00:00:00",
            time_to="not-a-date", limit=100, offset=20,
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)


class ClusterAuditRiskRouteRulesTest(unittest.TestCase):
    """集群子路径写操作在审计风险规则表里的等级。"""

    def test_cluster_subpaths_resolve_registered_levels(self):
        cases = [
            ("POST", "/api/clusters/c1/nodes/n1/cordon", "high"),
            ("POST", "/api/clusters/c1/nodes/n1/uncordon", "medium"),
            ("POST", "/api/clusters/c1/nodes/n1/drain", "high"),
            ("PATCH", "/api/clusters/c1/nodes/n1/taints", "medium"),
            ("PATCH", "/api/clusters/c1/nodes/n1/labels", "medium"),
            ("PATCH", "/api/clusters/c1/namespaces/ns1/labels", "medium"),
            ("POST", "/api/clusters/c1/apply", "medium"),
            ("PATCH", "/api/clusters/c1/workloads/Deployment/ns/name/scale", "medium"),
            ("PATCH", "/api/clusters/c1/workloads/Deployment/ns/name/set-image", "high"),
            ("PATCH", "/api/clusters/c1/workloads/Deployment/ns/name/restart", "medium"),
            ("POST", "/api/clusters/c1/workloads/Deployment/ns/name/rollout-undo", "high"),
            ("DELETE", "/api/clusters/c1/pods/ns/name", "high"),
            ("DELETE", "/api/clusters/c1/namespaces/kube-system", "high"),
            ("POST", "/api/clusters/c1/namespaces", "medium"),
            ("PATCH", "/api/clusters/c1/pvc/ns/p1/resize", "medium"),
            # 二轮补规则：这三条此前不在表里，后端已挂 require_risk_ack 却记 low
            ("PUT", "/api/clusters/c1", "medium"),
            ("PATCH", "/api/clusters/c1/resources/ns/ConfigMap/cm1/edit", "medium"),
            ("DELETE", "/api/clusters/c1/resources/ns/ConfigMap/cm1", "high"),
            ("POST", "/api/clusters/c1/pods/ns/p1/exec/session", "medium"),
            # logs/session 只是读日志，低敏，不应被 exec 规则误命中
            ("POST", "/api/clusters/c1/pods/ns/p1/logs/session", "low"),
            # watch/refresh/scan 是读或快照操作，保持 low
            ("POST", "/api/clusters/c1/refresh", "low"),
            ("GET", "/api/clusters/c1/pods", "low"),
            ("DELETE", "/api/clusters/c1", "high"),
        ]
        for method, path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(resolve_request_risk_level(method, path), expected)


if __name__ == "__main__":
    unittest.main()
