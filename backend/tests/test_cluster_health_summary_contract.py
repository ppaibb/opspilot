"""集群健康总览接口最基本契约测试。

只验证：
- 未纳管集群（kubeconfig 不存在）会 404；
- 健康总览响应包含固定的字段集合（通过降级分支）。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

from app.api import clusters as clusters_module


class HealthSummaryContractTest(unittest.TestCase):
    def test_unknown_cluster_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            clusters_module.get_cluster_health_summary("does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_api_unreachable_branch_returns_graceful_shape(self):
        """kubeconfig 存在但 API 不通时，响应也应当是完整结构。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cluster_id = "cid-probe"
            kc_path = os.path.join(tmp_dir, f"{cluster_id}.yaml")
            with open(kc_path, "w", encoding="utf-8") as fp:
                fp.write("apiVersion: v1\nkind: Config\n")

            # Patch 全局目录和 _get_api_client，避免真的去找 API。
            with patch.object(clusters_module, "KUBECONFIG_DIR", tmp_dir):
                def _fake_api_client(*_args, **_kwargs):
                    raise ApiException(status=503, reason="ServiceUnavailable")

                with patch.object(clusters_module, "_get_api_client", _fake_api_client):
                    # 绕过只读缓存，直接调用 loader 逻辑。
                    with patch.object(
                        clusters_module,
                        "_get_or_set_k8s_read_cache",
                        lambda cluster_id, operation, params, loader, **kwargs: loader(),
                    ):
                        result = clusters_module.get_cluster_health_summary(cluster_id)

        self.assertFalse(result["api_reachable"])
        self.assertIn("node_stats", result)
        self.assertIn("workload_stats", result)
        self.assertIn("pod_stats", result)
        self.assertIn("recent_warning_events", result)
        self.assertIn("top_issues", result)
        self.assertIn("api_error", result)
        self.assertIn("503", result["api_error"])


if __name__ == "__main__":
    unittest.main()
