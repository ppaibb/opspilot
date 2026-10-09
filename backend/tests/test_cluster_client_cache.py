import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.api import clusters
from app.api.clusters import KubeScanRequest


class ClusterClientCacheTest(unittest.TestCase):
    def tearDown(self):
        clusters._clear_cluster_client_cache()
        clusters._clear_k8s_read_cache()

    def test_scan_uses_temporary_kubeconfig_without_cluster_id(self):
        """扫描新集群时还没有 cluster_id，必须直接使用临时 kubeconfig。"""
        fake_node = SimpleNamespace(
            status=SimpleNamespace(
                capacity={"cpu": "2", "memory": "1048576Ki", "pods": "110"},
            ),
        )
        fake_v1 = Mock()
        fake_v1.list_node.return_value = SimpleNamespace(items=[fake_node])
        fake_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[object(), object()])
        fake_version = Mock()
        fake_version.get_code.return_value = SimpleNamespace(git_version="v1.29.0")

        with patch.object(clusters.config, "new_client_from_config", return_value=object()) as new_client, \
             patch.object(clusters.client, "CoreV1Api", return_value=fake_v1), \
             patch.object(clusters.client, "VersionApi", return_value=fake_version):
            result = clusters.scan_kubernetes_cluster(KubeScanRequest(kubeconfig_yaml="apiVersion: v1\nkind: Config\n"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_count"], 1)
        self.assertEqual(new_client.call_count, 1)

    def test_api_client_cache_rebuilds_when_kubeconfig_changes(self):
        """kubeconfig 文件变化后，缓存必须失效，避免继续使用旧连接。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            kubeconfig = Path(tmp_dir) / "c-test.yaml"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")

            first_client = object()
            second_client = object()
            with patch.object(clusters, "KUBECONFIG_DIR", tmp_dir), \
                 patch.object(clusters.config, "new_client_from_config", side_effect=[first_client, second_client]):
                self.assertIs(clusters._get_api_client("c-test"), first_client)
                clusters._client_cache["c-test"]["mtime"] = -1
                self.assertIs(clusters._get_api_client("c-test"), second_client)

    def test_clear_client_cache_removes_api_and_dynamic_clients(self):
        clusters._client_cache["c-test"] = {"client": object(), "mtime": 1, "expires_at": 999999}
        clusters._dynamic_client_cache["c-test"] = {"client": object(), "mtime": 1, "expires_at": 999999}

        clusters._clear_cluster_client_cache("c-test")

        self.assertNotIn("c-test", clusters._client_cache)
        self.assertNotIn("c-test", clusters._dynamic_client_cache)

    def test_read_cache_reuses_value_until_ttl_expires(self):
        """只读资源短时间重复查询时应复用缓存结果，减少 API Server 压力。"""
        calls = []

        def loader():
            calls.append(object())
            return [{"name": "default"}]

        first = clusters._get_or_set_k8s_read_cache(
            "c-test",
            "namespaces",
            ("_all",),
            loader,
            ttl_seconds=10,
        )
        second = clusters._get_or_set_k8s_read_cache(
            "c-test",
            "namespaces",
            ("_all",),
            loader,
            ttl_seconds=10,
        )

        self.assertEqual(first, [{"name": "default"}])
        self.assertEqual(second, [{"name": "default"}])
        self.assertEqual(len(calls), 1)

    def test_read_cache_marks_paged_envelope_cache_hits(self):
        """分页 envelope 缓存命中时应标记 cached=true，便于观测。"""
        calls = []

        def loader():
            calls.append(object())
            return {"items": [{"name": "pod-a"}], "continue_token": "", "limit": 100, "cached": False}

        first = clusters._get_or_set_k8s_read_cache("c-test", "pods", ("_all", 100), loader, ttl_seconds=10)
        second = clusters._get_or_set_k8s_read_cache("c-test", "pods", ("_all", 100), loader, ttl_seconds=10)

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)

    def test_read_cache_invalidates_when_kubeconfig_changes(self):
        """kubeconfig 更新后只读缓存也必须失效，避免旧集群数据串用。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            kubeconfig = Path(tmp_dir) / "c-test.yaml"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            calls = []

            def loader():
                calls.append(object())
                return [{"count": len(calls)}]

            with patch.object(clusters, "KUBECONFIG_DIR", tmp_dir):
                first = clusters._get_or_set_k8s_read_cache("c-test", "pods", ("_all",), loader, ttl_seconds=10)
                clusters._k8s_read_cache[("c-test", "pods", ("_all",))]["mtime"] = -1
                second = clusters._get_or_set_k8s_read_cache("c-test", "pods", ("_all",), loader, ttl_seconds=10)

            self.assertEqual(first, [{"count": 1}])
            self.assertEqual(second, [{"count": 2}])
            self.assertEqual(len(calls), 2)

    def test_read_cache_singleflight_collapses_concurrent_misses(self):
        """同一缓存键并发 miss 时，只允许一个请求真正访问 K8s。"""
        calls = []

        def loader():
            calls.append(object())
            time.sleep(0.05)
            return [{"name": "pod-a"}]

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    clusters._get_or_set_k8s_read_cache(
                        "c-test",
                        "pods",
                        ("_all", 100, "", "", ""),
                        loader,
                        ttl_seconds=10,
                    )
                )
            )
            for _ in range(5)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [[{"name": "pod-a"}]] * 5)

    def test_selector_validation_rejects_control_characters(self):
        """selector 参数会进入 K8s API，必须拒绝控制字符和超长文本。"""
        with self.assertRaises(HTTPException):
            clusters._validate_k8s_selector("app=demo\nmetadata.name=bad", "label_selector")

    def test_paged_pods_passes_limit_continue_and_selectors(self):
        """分页模式应把 limit/continue/selector 下推给 Kubernetes API。"""
        fake_pod = SimpleNamespace(
            metadata=SimpleNamespace(
                name="pod-a",
                namespace="default",
                creation_timestamp="2026-05-08T00:00:00Z",
                labels={"app": "demo"},
                owner_references=[],
            ),
            status=SimpleNamespace(phase="Running", reason="", container_statuses=[]),
            spec=SimpleNamespace(node_name="node-a", containers=[SimpleNamespace(image="nginx")]),
        )
        fake_v1 = Mock()
        fake_v1.list_namespaced_pod.return_value = SimpleNamespace(
            items=[fake_pod],
            metadata=SimpleNamespace(_continue="next-page"),
        )

        with patch.object(clusters, "_get_api_client", return_value=object()), \
             patch.object(clusters.client, "CoreV1Api", return_value=fake_v1):
            result = clusters.list_pods(
                "c-test",
                namespace="default",
                limit=50,
                continue_token="token-a",
                label_selector="app=demo",
                field_selector="status.phase=Running",
                paginated=True,
            )

        fake_v1.list_namespaced_pod.assert_called_once_with(
            namespace="default",
            limit=50,
            _continue="token-a",
            label_selector="app=demo",
            field_selector="status.phase=Running",
            _request_timeout=6,
        )
        self.assertEqual(result["continue_token"], "next-page")
        self.assertEqual(result["limit"], 50)
        self.assertEqual(result["items"][0]["name"], "pod-a")

    def test_paged_dynamic_resources_passes_limit_continue_and_selectors(self):
        """非 typed 覆盖的通用资源分页模式应下推分页参数到 DynamicClient。"""
        fake_resource = Mock()
        fake_resource.namespaced = True
        fake_resource.get.return_value = SimpleNamespace(
            to_dict=lambda: {
                "items": [
                    {
                        "metadata": {
                            "name": "ing-a",
                            "namespace": "default",
                            "creationTimestamp": "2026-05-08T00:00:00Z",
                        },
                        "spec": {"rules": [{"host": "demo.example.com"}]},
                        "status": {},
                    }
                ],
                "metadata": {"continue": "next-resource"},
            }
        )
        fake_dyn = Mock()
        fake_dyn.resources.get.return_value = fake_resource

        with patch.object(clusters, "_get_api_client", return_value=object()), \
             patch.object(clusters, "_get_dynamic_client", return_value=fake_dyn):
            result = clusters.list_cluster_resources(
                "c-test",
                kind="Ingress",
                namespace="default",
                limit=25,
                continue_token="token-r",
                label_selector="app=demo",
                field_selector="metadata.name=ing-a",
                paginated=True,
            )

        fake_resource.get.assert_called_once_with(
            namespace="default",
            limit=25,
            _continue="token-r",
            label_selector="app=demo",
            field_selector="metadata.name=ing-a",
        )
        self.assertEqual(result["continue_token"], "next-resource")
        self.assertEqual(result["items"][0]["name"], "ing-a")

    def test_service_resources_use_typed_client_fast_path(self):
        """Service 这类高频资源应走 typed client，避免 DynamicClient discovery 开销。"""
        fake_service = SimpleNamespace(
            to_dict=lambda: {
                "metadata": {
                    "name": "svc-a",
                    "namespace": "default",
                    "creationTimestamp": "2026-05-08T00:00:00Z",
                },
                "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.1"},
                "status": {},
            }
        )
        fake_v1 = Mock()
        fake_v1.list_namespaced_service.return_value = SimpleNamespace(
            items=[fake_service],
            metadata=SimpleNamespace(_continue=""),
        )

        with patch.object(clusters, "_get_api_client", return_value=object()), \
             patch.object(clusters.client, "CoreV1Api", return_value=fake_v1), \
             patch.object(clusters, "_get_dynamic_client") as dynamic_client:
            result = clusters.list_cluster_resources(
                "c-test",
                kind="Service",
                namespace="default",
                limit=None,
                continue_token="",
                label_selector="",
                field_selector="",
                paginated=False,
            )

        dynamic_client.assert_not_called()
        fake_v1.list_namespaced_service.assert_called_once_with(namespace="default", _request_timeout=6)
        self.assertEqual(result[0]["kind"], "Service")
        self.assertEqual(result[0]["detail"], "ClusterIP/10.0.0.1 | Ports: -")

    def test_k8s_api_exception_is_classified_for_permissions_and_rate_limit(self):
        """K8s 认证、授权和限流错误应返回可诊断的 HTTP 状态与信息。"""
        forbidden = clusters.ApiException(status=403, reason="Forbidden")
        rate_limited = clusters.ApiException(status=429, reason="Too Many Requests")

        forbidden_http = clusters._raise_k8s_http_exception(forbidden, "查询资源")
        rate_limited_http = clusters._raise_k8s_http_exception(rate_limited, "查询资源")

        self.assertEqual(forbidden_http.status_code, 403)
        self.assertIn("权限不足", forbidden_http.detail)
        self.assertEqual(rate_limited_http.status_code, 429)
        self.assertIn("限流", rate_limited_http.detail)

    def test_k8s_ws_session_is_one_time_and_bound_to_target(self):
        """K8s Pod logs/exec WebSocket 会话必须一次性消费，并绑定目标资源。"""
        session_id, token, _ = clusters._create_k8s_ws_session(
            cluster_id="c-test",
            namespace="default",
            pod_name="pod-a",
            action="logs",
            created_by="alice",
            container="app",
            tail=100,
        )

        ok, message, context = clusters._consume_k8s_ws_session(
            session_id,
            token,
            cluster_id="c-test",
            namespace="default",
            pod_name="pod-a",
            action="logs",
        )
        self.assertTrue(ok, message)
        self.assertEqual(context.container, "app")

        ok, message, context = clusters._consume_k8s_ws_session(
            session_id,
            token,
            cluster_id="c-test",
            namespace="default",
            pod_name="pod-a",
            action="logs",
        )
        self.assertFalse(ok)
        self.assertIsNone(context)

        other_id, other_token, _ = clusters._create_k8s_ws_session(
            cluster_id="c-test",
            namespace="default",
            pod_name="pod-a",
            action="exec",
            created_by="alice",
        )
        ok, message, context = clusters._consume_k8s_ws_session(
            other_id,
            other_token,
            cluster_id="c-test",
            namespace="default",
            pod_name="pod-a",
            action="logs",
        )
        self.assertFalse(ok)
        self.assertIsNone(context)

    def test_workloads_are_loaded_concurrently(self):
        """三类 workload 查询应并发执行，避免串行叠加 K8s API 延迟。"""
        def workload(name):
            return SimpleNamespace(
                metadata=SimpleNamespace(namespace="default", name=name, creation_timestamp="2026-05-08T00:00:00Z"),
                spec=SimpleNamespace(
                    replicas=1,
                    template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image="nginx")])),
                ),
                status=SimpleNamespace(
                    ready_replicas=1,
                    desired_number_scheduled=1,
                    number_ready=1,
                    available_replicas=1,
                    updated_replicas=1,
                ),
            )

        fake_apps_v1 = Mock()
        # Job / CronJob 走 BatchV1Api：这里同样返回空列表，避免真实客户端被构造出来
        # （kubernetes 客户端的 generated 方法会访问 api_client，测试传的是占位对象）。
        fake_batch_v1 = Mock()
        fake_batch_v1.list_job_for_all_namespaces.return_value = SimpleNamespace(items=[])
        fake_batch_v1.list_cron_job_for_all_namespaces.return_value = SimpleNamespace(items=[])
        fake_batch_v1.list_namespaced_job.return_value = SimpleNamespace(items=[])
        fake_batch_v1.list_namespaced_cron_job.return_value = SimpleNamespace(items=[])

        def slow_response(item):
            time.sleep(0.12)
            return SimpleNamespace(items=[item])

        fake_apps_v1.list_deployment_for_all_namespaces.side_effect = lambda _request_timeout: slow_response(workload("dep"))
        fake_apps_v1.list_stateful_set_for_all_namespaces.side_effect = lambda _request_timeout: slow_response(workload("sts"))
        fake_apps_v1.list_daemon_set_for_all_namespaces.side_effect = lambda _request_timeout: slow_response(workload("ds"))
        # ReplicaSet 不在本用例关注范围内，返回空列表即可。
        fake_apps_v1.list_replica_set_for_all_namespaces.return_value = SimpleNamespace(items=[])

        with tempfile.TemporaryDirectory() as tmp_dir:
            kubeconfig = Path(tmp_dir) / "c-test.yaml"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            with patch.object(clusters, "KUBECONFIG_DIR", tmp_dir), \
                 patch.object(clusters, "_get_api_client", return_value=object()), \
                 patch.object(clusters.client, "AppsV1Api", return_value=fake_apps_v1), \
                 patch.object(clusters.client, "BatchV1Api", return_value=fake_batch_v1):
                start = time.perf_counter()
                result = clusters.get_cluster_workloads("c-test", namespace="_all")
                elapsed = time.perf_counter() - start

        self.assertCountEqual([row["kind"] for row in result], ["DaemonSet", "Deployment", "StatefulSet"])
        self.assertLess(elapsed, 0.25)

    def test_pod_diagnostics_aggregates_status_events_logs_and_summary(self):
        """诊断接口必须一次性返回容器状态、近期事件、日志尾部、探针与 summary。"""
        last_terminated = SimpleNamespace(
            exit_code=137, reason="OOMKilled", signal=9,
            started_at=None, finished_at=None, message="killed",
        )
        cs_main = SimpleNamespace(
            name="main", ready=False, restart_count=5, image="nginx:1.25",
            image_id="docker://sha256:abc",
            state=SimpleNamespace(running=None, waiting=SimpleNamespace(reason="CrashLoopBackOff", message="back-off"), terminated=None),
            last_state=SimpleNamespace(terminated=last_terminated),
        )
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="pod-a", namespace="default"),
            spec=SimpleNamespace(
                node_name="node-1",
                containers=[SimpleNamespace(
                    name="main",
                    liveness_probe=SimpleNamespace(
                        http_get=SimpleNamespace(scheme="HTTP", host="", path="/health", port=8080),
                        tcp_socket=None, _exec=None, grpc=None,
                        initial_delay_seconds=5, period_seconds=10, timeout_seconds=1, failure_threshold=3,
                    ),
                    readiness_probe=None, startup_probe=None,
                )],
            ),
            status=SimpleNamespace(
                phase="Running", pod_ip="10.0.0.5",
                container_statuses=[cs_main], init_container_statuses=None,
            ),
        )
        fake_event = SimpleNamespace(
            type="Warning", reason="BackOff",
            message="Back-off restarting failed container",
            count=8,
            last_timestamp=None, event_time=None,
            metadata=SimpleNamespace(creation_timestamp=None),
            source=SimpleNamespace(component="kubelet"),
        )
        fake_v1 = Mock()
        fake_v1.read_namespaced_pod.return_value = pod
        fake_v1.list_namespaced_event.return_value = SimpleNamespace(items=[fake_event])
        fake_v1.read_namespaced_pod_log.return_value = "line1\nline2\nERROR oom\n"

        with tempfile.TemporaryDirectory() as tmp_dir:
            kubeconfig = Path(tmp_dir) / "c-test.yaml"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            with patch.object(clusters, "KUBECONFIG_DIR", tmp_dir), \
                 patch.object(clusters, "_get_api_client", return_value=object()), \
                 patch.object(clusters.client, "CoreV1Api", return_value=fake_v1):
                result = clusters.get_pod_diagnostics(
                    cluster_id="c-test", namespace="default", name="pod-a", include_logs=True,
                )

        self.assertEqual(result["phase"], "Running")
        self.assertEqual(len(result["container_statuses"]), 1)
        cs = result["container_statuses"][0]
        self.assertEqual(cs["state"], "Waiting")
        self.assertEqual(cs["state_reason"], "CrashLoopBackOff")
        self.assertEqual(cs["last_terminated"]["exit_code"], 137)
        self.assertEqual(cs["last_terminated"]["reason"], "OOMKilled")
        self.assertEqual(len(result["recent_events"]), 1)
        self.assertEqual(result["recent_events"][0]["reason"], "BackOff")
        self.assertGreater(len(result["recent_logs"]), 0)
        self.assertIn("line1", result["recent_logs"][0]["lines"])
        self.assertEqual(result["probes"][0]["type"], "liveness")
        # OOMKilled 优先级高于 CrashLoopBackOff，summary 应给出 OOM 引导
        self.assertEqual(result["summary"]["level"], "critical")
        self.assertTrue(any("OOM" in h or "内存" in h for h in result["summary"]["hints"]))

    def test_pod_diagnostics_degrades_when_logs_fail(self):
        """日志拉取异常时必须降级为 error 字段，而不是抛 500。"""
        cs = SimpleNamespace(
            name="main", ready=True, restart_count=0, image="nginx:1.25", image_id="",
            state=SimpleNamespace(running=SimpleNamespace(), waiting=None, terminated=None),
            last_state=None,
        )
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="pod-a", namespace="default"),
            spec=SimpleNamespace(node_name="node-1", containers=[
                SimpleNamespace(name="main", liveness_probe=None, readiness_probe=None, startup_probe=None),
            ]),
            status=SimpleNamespace(
                phase="Running", pod_ip="10.0.0.5",
                container_statuses=[cs], init_container_statuses=None,
            ),
        )
        fake_v1 = Mock()
        fake_v1.read_namespaced_pod.return_value = pod
        fake_v1.list_namespaced_event.return_value = SimpleNamespace(items=[])
        fake_v1.read_namespaced_pod_log.side_effect = clusters.ApiException(status=400, reason="container not started")

        with tempfile.TemporaryDirectory() as tmp_dir:
            kubeconfig = Path(tmp_dir) / "c-test.yaml"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            with patch.object(clusters, "KUBECONFIG_DIR", tmp_dir), \
                 patch.object(clusters, "_get_api_client", return_value=object()), \
                 patch.object(clusters.client, "CoreV1Api", return_value=fake_v1):
                result = clusters.get_pod_diagnostics(
                    cluster_id="c-test", namespace="default", name="pod-a", include_logs=True,
                )

        self.assertEqual(result["phase"], "Running")
        self.assertEqual(len(result["recent_logs"]), 1)
        self.assertEqual(result["recent_logs"][0]["lines"], [])
        self.assertIn("400", result["recent_logs"][0]["error"])
        # 容器 Running 且 ready，summary 应判定 ok
        self.assertEqual(result["summary"]["level"], "ok")


if __name__ == "__main__":
    unittest.main()
