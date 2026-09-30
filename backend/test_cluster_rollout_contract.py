"""工作负载 rollout 状态 / 镜像更新契约测试。

不直连 K8s，只验证纯函数：
- `_rollout_status_summary` 会正确计算 Deployment / DaemonSet 的滚动状态；
- `_validate_image_ref` 拒绝非法镜像串。
"""
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.clusters import (
    _rollout_status_summary,
    _validate_image_ref,
)


def _deployment(
    *,
    generation: int = 2,
    observed_generation: int = 2,
    desired: int = 3,
    ready: int = 3,
    updated: int = 3,
    available: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name="api", namespace="default", generation=generation),
        spec=SimpleNamespace(replicas=desired),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            ready_replicas=ready,
            updated_replicas=updated,
            available_replicas=available,
            conditions=[],
        ),
    )


def _daemonset(
    *,
    generation: int = 1,
    observed_generation: int = 1,
    desired: int = 5,
    ready: int = 5,
    updated: int = 5,
    available: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name="fluentd", namespace="kube-system", generation=generation),
        spec=SimpleNamespace(replicas=None),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            desired_number_scheduled=desired,
            number_ready=ready,
            updated_number_scheduled=updated,
            number_available=available,
            conditions=[],
        ),
    )


class RolloutStatusTest(unittest.TestCase):
    def test_deployment_completed_status(self):
        summary = _rollout_status_summary("deployment", _deployment())
        self.assertEqual(summary["rollout_status"], "completed")
        self.assertEqual(summary["desired_replicas"], 3)
        self.assertEqual(summary["ready_replicas"], 3)

    def test_deployment_progressing_when_generation_not_observed(self):
        summary = _rollout_status_summary(
            "deployment",
            _deployment(generation=3, observed_generation=2, ready=1, available=1, updated=1),
        )
        self.assertEqual(summary["rollout_status"], "progressing")

    def test_deployment_progressing_when_ready_lt_desired(self):
        summary = _rollout_status_summary(
            "deployment",
            _deployment(ready=1, available=1, updated=1),
        )
        self.assertEqual(summary["rollout_status"], "progressing")

    def test_deployment_stopped_when_desired_zero(self):
        summary = _rollout_status_summary(
            "deployment",
            _deployment(desired=0, ready=0, updated=0, available=0),
        )
        self.assertEqual(summary["rollout_status"], "stopped")

    def test_daemonset_completed_status(self):
        summary = _rollout_status_summary("daemonset", _daemonset())
        self.assertEqual(summary["rollout_status"], "completed")
        self.assertEqual(summary["ready_replicas"], 5)

    def test_daemonset_progressing_when_updated_lt_desired(self):
        summary = _rollout_status_summary(
            "daemonset",
            _daemonset(updated=3, ready=3),
        )
        self.assertEqual(summary["rollout_status"], "progressing")


class ImageValidationTest(unittest.TestCase):
    def test_accepts_valid_image(self):
        self.assertEqual(_validate_image_ref("registry/mall-api:v1.2.3"), "registry/mall-api:v1.2.3")

    def test_rejects_empty(self):
        with self.assertRaises(HTTPException):
            _validate_image_ref("")

    def test_rejects_newline(self):
        with self.assertRaises(HTTPException):
            _validate_image_ref("registry/mall-api:v1\nmalicious")

    def test_rejects_space(self):
        with self.assertRaises(HTTPException):
            _validate_image_ref("mall-api:v1 extra")

    def test_rejects_too_long(self):
        with self.assertRaises(HTTPException):
            _validate_image_ref("x" * 600)


if __name__ == "__main__":
    unittest.main()
