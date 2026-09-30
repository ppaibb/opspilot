"""节点 drain 预检与驱逐分类契约测试。

不真连 K8s API，只验证 Pod 分类规则：
- DaemonSet 管理的 Pod 默认跳过；
- 静态 Pod（mirror）无法驱逐；
- emptyDir Pod 需要显式允许；
- 裸 Pod 需要 force；
- 已结束 Pod 不重复驱逐。
"""
import unittest
from types import SimpleNamespace

from app.api.clusters import (
    _pod_has_local_emptydir,
    _pod_is_daemonset_managed,
    _pod_is_mirror,
    _pod_is_standalone,
    _pod_is_terminal,
    _summarize_pod_for_drain,
)


def _make_pod(
    *,
    name: str = "p1",
    namespace: str = "default",
    phase: str = "Running",
    owner_kind: str = "ReplicaSet",
    owner_name: str = "api-xxx",
    volumes: list | None = None,
    mirror: bool = False,
) -> SimpleNamespace:
    annotations = {"kubernetes.io/config.mirror": "true"} if mirror else {}
    owner_refs = []
    if owner_kind:
        owner_refs.append(SimpleNamespace(kind=owner_kind, name=owner_name))
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            owner_references=owner_refs,
            annotations=annotations,
        ),
        spec=SimpleNamespace(volumes=volumes or []),
        status=SimpleNamespace(phase=phase),
    )


class NodeDrainClassificationTest(unittest.TestCase):
    def test_daemonset_pod_is_detected(self):
        pod = _make_pod(owner_kind="DaemonSet", owner_name="ds")
        self.assertTrue(_pod_is_daemonset_managed(pod))

    def test_replicaset_pod_is_not_daemonset(self):
        pod = _make_pod(owner_kind="ReplicaSet")
        self.assertFalse(_pod_is_daemonset_managed(pod))

    def test_mirror_pod_is_detected(self):
        pod = _make_pod(mirror=True)
        self.assertTrue(_pod_is_mirror(pod))

    def test_non_mirror_pod(self):
        pod = _make_pod()
        self.assertFalse(_pod_is_mirror(pod))

    def test_empty_dir_is_detected(self):
        emptydir_vol = SimpleNamespace(empty_dir=SimpleNamespace(), host_path=None, config_map=None)
        pod = _make_pod(volumes=[emptydir_vol])
        self.assertTrue(_pod_has_local_emptydir(pod))

    def test_pvc_volume_is_not_local_storage(self):
        pvc_vol = SimpleNamespace(
            empty_dir=None,
            host_path=None,
            config_map=None,
            persistent_volume_claim=SimpleNamespace(claim_name="foo"),
        )
        pod = _make_pod(volumes=[pvc_vol])
        self.assertFalse(_pod_has_local_emptydir(pod))

    def test_standalone_pod_has_no_owner(self):
        pod = _make_pod(owner_kind="")
        self.assertTrue(_pod_is_standalone(pod))

    def test_pod_with_owner_is_not_standalone(self):
        pod = _make_pod(owner_kind="ReplicaSet")
        self.assertFalse(_pod_is_standalone(pod))

    def test_succeeded_pod_is_terminal(self):
        self.assertTrue(_pod_is_terminal(_make_pod(phase="Succeeded")))
        self.assertTrue(_pod_is_terminal(_make_pod(phase="Failed")))
        self.assertFalse(_pod_is_terminal(_make_pod(phase="Running")))

    def test_summary_exposes_controller_and_flags(self):
        emptydir_vol = SimpleNamespace(empty_dir=SimpleNamespace())
        pod = _make_pod(owner_kind="DaemonSet", owner_name="fluentd", volumes=[emptydir_vol])
        summary = _summarize_pod_for_drain(pod)
        self.assertEqual(summary["controller"], "DaemonSet")
        self.assertEqual(summary["owner"], "fluentd")
        self.assertTrue(summary["is_daemonset"])
        self.assertTrue(summary["has_local_storage"])
        self.assertFalse(summary["is_standalone"])


if __name__ == "__main__":
    unittest.main()
