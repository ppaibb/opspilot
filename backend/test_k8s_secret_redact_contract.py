"""K8s Secret 内容边界契约测试。

阶段1安全边界：
1. detail / yaml / edit 三个读取口都不得回传 Secret 的 data/stringData；
2. apply 类端点：提交的 Secret 不带 data/stringData 时，必须先从集群读现有
   对象并保留其原密文，防止 server-side apply "空白覆盖"抹掉线上密钥；
3. 读取现有对象失败（非 404）时宁可拒绝提交，也不能静默清空。

全部通过 mock dynamic client 完成，不真连集群（参考现有 clusters 测试模式）。
"""
import unittest
import unittest.mock
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from app.api import clusters

EXISTING_DATA = {"password": "old-secret-value"}


class _FakeObj:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class _FakeResource:
    """dynamic client Resource 桩：记录 get / server_side_apply 调用。"""

    def __init__(self, existing=None, get_error=None):
        self.existing = existing
        self.get_error = get_error
        self.get_calls = []
        self.applied = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        if self.existing is None:
            raise ApiException(status=404, reason="Not Found")
        return _FakeObj(self.existing)

    def server_side_apply(self, **kwargs):
        self.applied.append(kwargs)
        return _FakeObj({"kind": "Secret"})


def _fake_dyn(resource):
    return SimpleNamespace(resources=SimpleNamespace(get=lambda **kwargs: resource))


def _secret_doc(with_data: bool) -> dict:
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db-auth", "namespace": "prod"},
        "type": "Opaque",
    }
    if with_data:
        doc["data"] = {"password": "new-secret-value"}
    return doc


def _secret_raw() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db-auth", "namespace": "prod", "managedFields": []},
        "type": "Opaque",
        "data": dict(EXISTING_DATA),
        "stringData": {"token": "plain-secret"},
    }


def _patch_cluster_io(raw_obj):
    """同时 mock api_client / dynamic_client，让读取口函数可直调。"""
    resource = _FakeResource(existing=raw_obj)
    dyn = _fake_dyn(resource)
    patcher_client = unittest.mock.patch.object(clusters, "_get_api_client", lambda cid: SimpleNamespace())
    patcher_dyn = unittest.mock.patch.object(clusters, "_get_dynamic_client", lambda cid: dyn)
    return resource, patcher_client, patcher_dyn


class SecretReadRedactContract(unittest.TestCase):
    """读取口：detail / yaml / edit 均不得出现 data/stringData。"""

    def test_detail_redacts_secret(self):
        resource, p1, p2 = _patch_cluster_io(_secret_raw())
        with p1, p2:
            result = clusters.get_resource_detail("cls-1", "prod", "Secret", "db-auth")
        self.assertNotIn("data", result)
        self.assertNotIn("stringData", result)
        self.assertTrue(result.get("secret_redacted"))
        self.assertNotIn("old-secret-value", str(result))

    def test_detail_keeps_configmap_data(self):
        raw = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "app-config"},
            "data": {"LOG_LEVEL": "info"},
        }
        resource, p1, p2 = _patch_cluster_io(raw)
        with p1, p2:
            result = clusters.get_resource_detail("cls-1", "prod", "ConfigMap", "app-config")
        self.assertEqual(result.get("data"), {"LOG_LEVEL": "info"})
        self.assertNotIn("secret_redacted", result)

    def test_yaml_redacts_secret_and_adds_comment(self):
        resource, p1, p2 = _patch_cluster_io(_secret_raw())
        with p1, p2:
            result = clusters.get_resource_yaml("cls-1", "prod", "Secret", "db-auth")
        content = result["yaml_content"]
        self.assertNotIn("old-secret-value", content)
        self.assertNotIn("plain-secret", content)
        self.assertNotIn("stringData", content)
        self.assertNotIn("\ndata:", content)  # data 键整体不出现
        self.assertIn("脱敏", content)  # 注释提示，而非字段（避免回放 Apply 校验失败）

    def test_edit_form_secret_has_no_data(self):
        resource, p1, p2 = _patch_cluster_io(_secret_raw())
        with p1, p2:
            payload = clusters.get_resource_edit_form("cls-1", "prod", "Secret", "db-auth")
        self.assertIsNone(payload["data"])
        self.assertFalse(payload["fields"]["data"])
        self.assertTrue(payload.get("secret_redacted"))
        self.assertNotIn("old-secret-value", str(payload))


_UNSET = object()


class SecretApplyPreserveContract(unittest.TestCase):
    """apply/dry-run：空白 Secret 提交必须保留线上原密文。"""

    def _run_apply(self, doc, existing=_UNSET, dry_run=False):
        if existing is _UNSET:
            existing = {"data": dict(EXISTING_DATA)}
        resource = _FakeResource(existing=existing)
        dyn = _fake_dyn(resource)
        result = clusters._apply_single_doc(dyn, doc, dry_run=dry_run)
        return result, resource

    def test_apply_without_data_preserves_existing(self):
        result, resource = self._run_apply(_secret_doc(with_data=False))
        self.assertEqual(result["status"], "valid")
        self.assertEqual(len(resource.applied), 1)
        body = resource.applied[0]["body"]
        self.assertEqual(body.get("data"), EXISTING_DATA)

    def test_dry_run_also_preserves(self):
        result, resource = self._run_apply(_secret_doc(with_data=False), dry_run=True)
        self.assertEqual(result["status"], "valid")
        body = resource.applied[0]["body"]
        self.assertEqual(body.get("data"), EXISTING_DATA)
        self.assertEqual(resource.applied[0].get("dry_run"), "All")

    def test_apply_with_submitted_data_not_replaced(self):
        doc = _secret_doc(with_data=True)
        result, resource = self._run_apply(doc)
        self.assertEqual(result["status"], "valid")
        body = resource.applied[0]["body"]
        self.assertEqual(body.get("data"), {"password": "new-secret-value"})
        self.assertEqual(resource.get_calls, [])  # 自带内容时不应读集群

    def test_apply_new_secret_404_proceeds_without_data(self):
        result, resource = self._run_apply(_secret_doc(with_data=False), existing=None)
        self.assertEqual(result["status"], "valid")
        body = resource.applied[0]["body"]
        self.assertNotIn("data", body)
        self.assertNotIn("stringData", body)

    def test_apply_read_failure_blocks_instead_of_wiping(self):
        """读取现有 Secret 失败（非 404）必须拒绝提交，不能静默空白覆盖。"""
        boom = ApiException(status=500, reason="Internal Server Error")
        resource = _FakeResource(get_error=boom)
        dyn = _fake_dyn(resource)
        result = clusters._apply_single_doc(dyn, _secret_doc(with_data=False))
        self.assertEqual(result["status"], "error")
        self.assertIn("空白覆盖", result["error"])
        self.assertEqual(resource.applied, [])

    def test_non_secret_apply_does_not_read_cluster(self):
        doc = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": "prod"},
            "spec": {"replicas": 1},
        }
        resource = _FakeResource(existing=_FakeObj({"data": {}}))
        dyn = _fake_dyn(resource)
        result = clusters._apply_single_doc(dyn, doc)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(resource.get_calls, [])


class SecretApplyEndpointContract(unittest.TestCase):
    """端到端：cluster_apply 直接调用，脱敏 YAML 回写不清空线上密文。"""

    def test_cluster_apply_with_redacted_yaml_preserves_data(self):
        redacted_yaml = (
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n"
            "  name: db-auth\n"
            "  namespace: prod\n"
            "type: Opaque\n"
        )
        resource = _FakeResource(existing={"data": dict(EXISTING_DATA)})
        dyn = _fake_dyn(resource)
        with unittest.mock.patch.object(clusters, "_get_api_client", lambda cid: SimpleNamespace()), \
             unittest.mock.patch.object(clusters, "_get_dynamic_client", lambda cid: dyn), \
             unittest.mock.patch.object(clusters, "_clear_k8s_read_cache", lambda cid: None):
            result = clusters.cluster_apply(
                "cls-1", clusters.YamlApplyRequest(yaml_content=redacted_yaml)
            )
        self.assertTrue(result["success"], result)
        body = resource.applied[0]["body"]
        self.assertEqual(body.get("data"), EXISTING_DATA)


if __name__ == "__main__":
    unittest.main()
