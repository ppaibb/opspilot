import unittest
from types import SimpleNamespace

from app.api.clusters import (
    _diagnose_node,
    _diagnose_pod,
    _diagnose_workload,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


class ClusterDiagnosisTest(unittest.TestCase):
    def test_diagnose_pod_image_pull_backoff(self):
        pod = _ns(
            metadata=_ns(deletion_timestamp=None),
            status=_ns(
                phase="Pending",
                reason="",
                container_statuses=[
                    _ns(
                        ready=False,
                        restart_count=0,
                        state=_ns(
                            waiting=_ns(
                                reason="ImagePullBackOff",
                                message="pull image failed",
                            ),
                            terminated=None,
                        ),
                    )
                ],
            ),
        )

        diagnosis = _diagnose_pod(pod)

        self.assertEqual(diagnosis["level"], "warning")
        self.assertEqual(diagnosis["reason"], "镜像拉取失败")
        self.assertIn("镜像地址", diagnosis["suggestion"])

    def test_diagnose_workload_when_replicas_not_ready(self):
        workload = _ns(
            spec=_ns(replicas=3),
            status=_ns(ready_replicas=1, available_replicas=1, updated_replicas=2),
        )

        diagnosis = _diagnose_workload("Deployment", workload, ready_replicas=1, desired_replicas=3)

        self.assertEqual(diagnosis["level"], "warning")
        self.assertEqual(diagnosis["reason"], "副本未就绪")
        self.assertIn("查看关联 Pod", diagnosis["suggestion"])

    def test_diagnose_node_not_ready_and_pressure(self):
        node = _ns(
            spec=_ns(unschedulable=False),
            status=_ns(
                conditions=[
                    _ns(type="Ready", status="False", reason="KubeletNotReady", message="kubelet stopped"),
                    _ns(type="DiskPressure", status="True", reason="DiskFull", message="disk pressure"),
                ],
            ),
        )

        diagnosis = _diagnose_node(node)

        self.assertEqual(diagnosis["level"], "critical")
        self.assertEqual(diagnosis["reason"], "节点不可用")
        self.assertIn("磁盘压力", diagnosis["detail"])


if __name__ == "__main__":
    unittest.main()
