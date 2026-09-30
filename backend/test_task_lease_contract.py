"""数据库租约（task_leases）契约测试。

覆盖阶段3「后台任务可靠性」的最小可行目标：多 worker / 多进程部署时，
同一后台任务（定时任务、账单同步、应用发布）靠数据库租约互斥，不重复执行。
"""
from __future__ import annotations

import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import scheduler_registry as registry
from app.db.database import Base
from app.models.application import ApplicationRelease
from app.models.finance import BillSyncRun
from app.models.task_lease import TaskLease
from app.models.user import User
from app.services.application_time import format_app_datetime, now_dt, parse_app_datetime
from app.services.leases import acquire_lease, lease_owner, release_lease, renew_lease


def _make_session():
    """创建内存 SQLite 会话并按当前 Base.metadata 建表（与既有契约测试一致）。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class LeaseOwnerContractTest(unittest.TestCase):
    """lease_owner 必须是可区分实例的标识。"""

    def test_owner_format_and_uniqueness(self):
        owner = lease_owner()
        self.assertRegex(owner, r"^.+:\d+:[0-9a-f]{8}$")
        self.assertNotEqual(owner, lease_owner())


class LeaseServiceContractTest(unittest.TestCase):
    """acquire / renew / release 的核心语义。"""

    def setUp(self):
        self.db = _make_session()

    def tearDown(self):
        self.db.close()

    def _lease_row(self, lease_key: str) -> TaskLease | None:
        return self.db.query(TaskLease).filter(TaskLease.lease_key == lease_key).first()

    def test_acquire_free_key_writes_lease_row(self):
        self.assertTrue(acquire_lease(self.db, "scheduler:bill_sync", "host-a:1:aaaaaaaa", 600))
        row = self._lease_row("scheduler:bill_sync")
        self.assertIsNotNone(row)
        self.assertEqual("host-a:1:aaaaaaaa", row.owner)
        for field in ("acquired_at", "heartbeat_at", "expires_at"):
            self.assertRegex(getattr(row, field) or "", TIME_PATTERN)

    def test_duplicate_acquire_fails_while_held(self):
        self.assertTrue(acquire_lease(self.db, "scheduler:bill_sync", "host-a:1:aaaaaaaa", 600))
        # 其他实例抢未过期租约失败
        self.assertFalse(acquire_lease(self.db, "scheduler:bill_sync", "host-b:2:bbbbbbbb", 600))
        # 同一 owner 重复获取同样失败（未过期即被占用，与 owner 无关）
        self.assertFalse(acquire_lease(self.db, "scheduler:bill_sync", "host-a:1:aaaaaaaa", 600))
        self.assertEqual("host-a:1:aaaaaaaa", self._lease_row("scheduler:bill_sync").owner)

    def test_acquire_steals_only_expired_lease(self):
        self.assertTrue(acquire_lease(self.db, "scheduler:bill_sync", "host-a:1:aaaaaaaa", 600))
        self.assertFalse(acquire_lease(self.db, "scheduler:bill_sync", "host-b:2:bbbbbbbb", 600))
        # 模拟租约过期（实例崩溃后 TTL 到期）
        row = self._lease_row("scheduler:bill_sync")
        row.expires_at = "2000-01-01 00:00:00"
        self.db.commit()
        self.assertTrue(acquire_lease(self.db, "scheduler:bill_sync", "host-b:2:bbbbbbbb", 600))
        row = self._lease_row("scheduler:bill_sync")
        self.assertEqual("host-b:2:bbbbbbbb", row.owner)
        self.assertGreater(parse_app_datetime(row.expires_at), now_dt())

    def test_release_deletes_row_and_allows_reacquire(self):
        self.assertTrue(acquire_lease(self.db, "bill-sync:3", "host-a:1:aaaaaaaa", 600))
        release_lease(self.db, "bill-sync:3", "host-a:1:aaaaaaaa")
        self.assertIsNone(self._lease_row("bill-sync:3"))
        self.assertTrue(acquire_lease(self.db, "bill-sync:3", "host-b:2:bbbbbbbb", 600))

    def test_release_is_owner_scoped_and_idempotent(self):
        self.assertTrue(acquire_lease(self.db, "bill-sync:3", "host-a:1:aaaaaaaa", 600))
        # 他人释放是空操作，不能误删
        release_lease(self.db, "bill-sync:3", "host-b:2:bbbbbbbb")
        self.assertIsNotNone(self._lease_row("bill-sync:3"))
        # 持有者释放后再次释放同样幂等
        release_lease(self.db, "bill-sync:3", "host-a:1:aaaaaaaa")
        release_lease(self.db, "bill-sync:3", "host-a:1:aaaaaaaa")
        self.assertIsNone(self._lease_row("bill-sync:3"))

    def test_renew_only_by_owner_extends_expiry(self):
        self.assertTrue(acquire_lease(self.db, "release:REL-1", "host-a:1:aaaaaaaa", 100))
        before = parse_app_datetime(self._lease_row("release:REL-1").expires_at)
        # 他人续约失败，到期时间不变
        self.assertFalse(renew_lease(self.db, "release:REL-1", "host-b:2:bbbbbbbb", 9999))
        self.assertEqual(before, parse_app_datetime(self._lease_row("release:REL-1").expires_at))
        # 持有者续约成功且有效期被延长
        self.assertTrue(renew_lease(self.db, "release:REL-1", "host-a:1:aaaaaaaa", 9999))
        self.assertGreater(parse_app_datetime(self._lease_row("release:REL-1").expires_at), before)


class TwoInstanceMutualExclusionTest(unittest.TestCase):
    """两个"实例"（不同 owner）串行模拟互斥。"""

    def setUp(self):
        self.db = _make_session()

    def tearDown(self):
        self.db.close()

    def test_second_instance_denied_until_first_releases(self):
        owner_a, owner_b = "host-a:100:aaaaaaaa", "host-b:200:bbbbbbbb"
        executed: list[str] = []

        # 实例 A 抢到租约并进入执行
        self.assertTrue(acquire_lease(self.db, "scheduler:alert_engine", owner_a, 120))
        # 执行窗口内实例 B 反复尝试均被拒绝，只能跳过本轮
        self.assertFalse(acquire_lease(self.db, "scheduler:alert_engine", owner_b, 120))
        self.assertFalse(acquire_lease(self.db, "scheduler:alert_engine", owner_b, 120))
        executed.append("A")
        # A 执行完释放，B 才能接管执行
        release_lease(self.db, "scheduler:alert_engine", owner_a)
        self.assertTrue(acquire_lease(self.db, "scheduler:alert_engine", owner_b, 120))
        executed.append("B")
        release_lease(self.db, "scheduler:alert_engine", owner_b)

        self.assertEqual(["A", "B"], executed)
        self.assertIsNone(self.db.query(TaskLease).filter(TaskLease.lease_key == "scheduler:alert_engine").first())

    def test_crashed_instance_lease_is_stolen_after_expiry(self):
        owner_a, owner_b = "host-a:100:aaaaaaaa", "host-b:200:bbbbbbbb"
        self.assertTrue(acquire_lease(self.db, "bill-sync:5", owner_a, 1800))
        # A 崩溃未释放；TTL 未到前 B 不能执行
        self.assertFalse(acquire_lease(self.db, "bill-sync:5", owner_b, 1800))
        # TTL 到期后 B 自动接管，任务不会永久停摆
        row = self.db.query(TaskLease).filter(TaskLease.lease_key == "bill-sync:5").first()
        row.expires_at = format_app_datetime(now_dt() - timedelta(seconds=1))
        self.db.commit()
        self.assertTrue(acquire_lease(self.db, "bill-sync:5", owner_b, 1800))
        self.assertEqual(owner_b, row.owner)


class SchedulerLeaseContractTest(unittest.TestCase):
    """调度器每轮执行前后的租约行为。"""

    def setUp(self):
        self.db = _make_session()

    def tearDown(self):
        self.db.close()

    def test_cycle_lease_gates_second_manager_and_releases_after_run(self):
        manager_a = registry.SchedulerManager()
        manager_b = registry.SchedulerManager()
        with patch("app.db.database.SessionLocal", return_value=self.db):
            self.assertTrue(manager_a._acquire_cycle_lease("alert_engine", 30))
            row = self.db.query(TaskLease).filter(TaskLease.lease_key == "scheduler:alert_engine").first()
            self.assertIsNotNone(row)
            self.assertEqual("scheduler:alert_engine", row.lease_key)
            # 另一"进程"（不同 owner）被挡住
            self.assertFalse(manager_b._acquire_cycle_lease("alert_engine", 30))
            # A 执行完释放后，B 可以接管
            manager_a._release_cycle_lease("alert_engine")
            self.assertTrue(manager_b._acquire_cycle_lease("alert_engine", 30))
            manager_b._release_cycle_lease("alert_engine")
        self.assertIsNone(self.db.query(TaskLease).first())

    def test_ttl_floor_uses_max_of_120_and_two_intervals(self):
        manager = registry.SchedulerManager()
        with patch("app.db.database.SessionLocal", return_value=self.db):
            self.assertTrue(manager._acquire_cycle_lease("alert_engine", 30))
            row = self.db.query(TaskLease).first()
            # TTL = max(120, 2*30) = 120 秒
            self.assertGreaterEqual((parse_app_datetime(row.expires_at) - now_dt()).total_seconds(), 119)
            manager._release_cycle_lease("alert_engine")

    def test_lease_infra_failure_fails_open(self):
        manager = registry.SchedulerManager()
        with patch("app.db.database.SessionLocal", side_effect=RuntimeError("db down")):
            # 租约设施异常时放行本进程执行，避免任务整体停摆
            self.assertTrue(manager._acquire_cycle_lease("bill_sync", 21600))


class ReleaseJobLeaseContractTest(unittest.TestCase):
    """发布后台任务的租约互斥。"""

    def setUp(self):
        self.db = _make_session()
        self.db.add(User(username="tester", name="Tester", hashed_password="x"))
        self.db.add(
            ApplicationRelease(
                release_id="REL-LEASE",
                app_id="APP-1",
                version="1.0.0",
                env="prod",
                release_type="kubernetes",
                status="running",
                summary="发布任务已提交",
                started_at=now_dt(),
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_skips_execution_and_keeps_record_when_lease_held(self):
        held_by_other = lease_owner()
        self.assertTrue(acquire_lease(self.db, "release:REL-LEASE", held_by_other, 3600))
        with patch("app.api.application_routes.releases.SessionLocal", return_value=self.db):
            with patch("app.api.application_routes.releases._execute_release_job_with_session") as executor:
                from app.api.application_routes.releases import _run_release_job

                _run_release_job("REL-LEASE", {}, "tester", "127.0.0.1")
        executor.assert_not_called()
        row = self.db.query(ApplicationRelease).filter(ApplicationRelease.release_id == "REL-LEASE").first()
        self.assertEqual("running", row.status)
        self.assertEqual("发布任务已提交", row.summary)
        # 原持有者的租约不受影响
        lease = self.db.query(TaskLease).filter(TaskLease.lease_key == "release:REL-LEASE").first()
        self.assertEqual(held_by_other, lease.owner)

    def test_executes_and_releases_lease_when_acquired(self):
        with patch("app.api.application_routes.releases.SessionLocal", return_value=self.db):
            with patch("app.api.application_routes.releases._execute_release_job_with_session") as executor:
                from app.api.application_routes.releases import _run_release_job

                _run_release_job("REL-LEASE", {"version": "1.0.0"}, "tester", "127.0.0.1")
        executor.assert_called_once()
        # 成功路径 finally 释放租约，失败后可立即重试
        self.assertIsNone(self.db.query(TaskLease).filter(TaskLease.lease_key == "release:REL-LEASE").first())


class BillSyncLeaseContractTest(unittest.TestCase):
    """账单同步的跨进程租约互斥：拿不到租约不落同步记录。"""

    def setUp(self):
        self.db = _make_session()

    def tearDown(self):
        self.db.close()

    def test_sync_skipped_without_sync_run_when_lease_held(self):
        self.assertTrue(acquire_lease(self.db, "bill-sync:1", lease_owner(), 1800))
        from app.services import bill_sync

        with patch("app.services.bill_sync.SessionLocal", return_value=self.db):
            result = bill_sync.sync_account_cycle(1, "2026-09", trigger="scheduled")
        self.assertIsNone(result)
        # 未产生重复的同步记录（第二实例静默退出，不写 running 脏数据）
        self.assertEqual(0, self.db.query(BillSyncRun).count())


if __name__ == "__main__":
    unittest.main()
