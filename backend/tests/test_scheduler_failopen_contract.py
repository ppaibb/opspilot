"""scheduler 租约 fail-open 升级契约测试。

修复点：租约设施自身异常时 _acquire_cycle_lease 放行本进程执行（fail-open，
防止租约故障放大成任务停摆），此前只打 warning。多 worker 部署下持续
fail-open 意味着同一任务会被多个进程重复执行，必须足够显眼——连续
fail-open 达到阈值（FAIL_OPEN_ERROR_THRESHOLD=3）升级为 error 日志；
租约恢复正常（获取成功）计数清零。
"""
from __future__ import annotations

import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.core.scheduler_registry import (
    FAIL_OPEN_ERROR_THRESHOLD,
    SchedulerManager,
)


class FailOpenEscalationContract(unittest.TestCase):
    """连续 fail-open 计数：第 3 次起 error，成功后清零。"""

    def _manager(self) -> SchedulerManager:
        manager = SchedulerManager()
        manager._lease_owner = "owner-test"  # noqa: SLF001 单测注入，跳过懒加载
        return manager

    def _fail_open(self, manager: SchedulerManager, key: str = "alert_engine") -> bool:
        with patch("app.db.database.SessionLocal", side_effect=RuntimeError("db down")):
            return manager._acquire_cycle_lease(key, 30)

    def _lease_ok(self, manager: SchedulerManager, key: str = "alert_engine") -> bool:
        with patch(
            "app.db.database.SessionLocal",
            return_value=SimpleNamespace(close=lambda: None),
        ):
            with patch("app.services.leases.acquire_lease", return_value=True):
                return manager._acquire_cycle_lease(key, 30)

    def test_threshold_constant_is_three(self):
        self.assertEqual(FAIL_OPEN_ERROR_THRESHOLD, 3)

    def test_first_two_fail_opens_are_warning_third_is_error(self):
        manager = self._manager()
        with self.assertLogs("app.core.scheduler_registry", level="WARNING") as logs:
            self.assertTrue(self._fail_open(manager))
            self.assertTrue(self._fail_open(manager))
            errors = [r for r in logs.records if r.levelno == logging.ERROR]
            self.assertEqual(errors, [], "前两次只应 warning")
            self.assertTrue(self._fail_open(manager))
        errors = [r for r in logs.records if r.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1, "连续第 3 次必须升级为 error")
        self.assertIn("连续第 3 次", errors[0].getMessage())

    def test_success_resets_counter(self):
        """清零：2 次失败 → 成功 → 再 2 次失败仍是 warning（不再连续）。"""
        manager = self._manager()
        with self.assertLogs("app.core.scheduler_registry", level="WARNING"):
            self._fail_open(manager)
            self._fail_open(manager)
        self.assertTrue(self._lease_ok(manager))
        with self.assertLogs("app.core.scheduler_registry", level="WARNING") as logs:
            self._fail_open(manager)
            self._fail_open(manager)
        self.assertEqual(
            [r for r in logs.records if r.levelno == logging.ERROR], [],
            "成功清零后重新计数，不应立即 error",
        )

    def test_counters_are_per_task(self):
        """不同任务的 fail-open 计数互相独立。"""
        manager = self._manager()
        with self.assertLogs("app.core.scheduler_registry", level="WARNING"):
            self._fail_open(manager, "alert_engine")
            self._fail_open(manager, "alert_engine")
        self.assertEqual(manager._fail_open_counts.get("alert_engine"), 2)
        self.assertIsNone(manager._fail_open_counts.get("bill_sync"))

    def test_recovery_after_success_logs_info_once(self):
        """从 fail-open 恢复到成功时打一条 info（运维可感知恢复时点）。"""
        manager = self._manager()
        with self.assertLogs("app.core.scheduler_registry", level="WARNING"):
            self._fail_open(manager)
        with self.assertLogs("app.core.scheduler_registry", level="INFO") as logs:
            self.assertTrue(self._lease_ok(manager))
        self.assertTrue(any("恢复正常" in r.getMessage() for r in logs.records))
        self.assertEqual(manager._fail_open_counts, {})


if __name__ == "__main__":
    unittest.main()
