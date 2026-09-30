"""账单同步租约被占时的可见反馈契约测试。

修复点：sync_account_cycle 拿不到跨进程租约（其他实例正在同步同一账号）
时此前静默 return None——手动同步走后台线程，调用方拿不到返回值，
页面上就是"点了同步毫无反应"。现在写一条 status='skipped' 的 BillSyncRun
占位记录，让触发方至少能看到"被跳过"。
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.finance import BillSyncRun, CloudAccount
from app.services import bill_sync


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        bind=engine,
        tables=[CloudAccount.__table__, BillSyncRun.__table__],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class BillSyncSkippedRunContract(unittest.TestCase):
    """租约被占 → 写 skipped 记录；其他静默路径保持不变。"""

    def setUp(self):
        self.TestingSession = _make_db()
        self.db = self.TestingSession()
        self.account = CloudAccount(
            id=7, name="主账号", provider="aliyun", credential_ref="cred-1",
            status="active", enabled=1,
        )
        self.db.add(self.account)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _run_sync_blocked_by_lease(self):
        with patch.object(bill_sync, "SessionLocal", self.TestingSession), \
             patch.object(bill_sync, "acquire_lease", return_value=False):
            result = bill_sync.sync_account_cycle(7, "2026-09", trigger="manual")
        return result

    def test_lease_miss_writes_skipped_record(self):
        """拿不到租约：返回 None，但留下 status=skipped 的同步记录。"""
        result = self._run_sync_blocked_by_lease()
        self.assertIsNone(result)
        rows = self.db.query(BillSyncRun).filter(BillSyncRun.account_id == 7).all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "skipped")
        self.assertEqual(row.billing_cycle, "2026-09")
        self.assertEqual(row.trigger, "manual")
        self.assertIn("另一同步", row.error_message)
        self.assertTrue(row.started_at)
        self.assertTrue(row.finished_at)

    def test_skipped_record_does_not_touch_account(self):
        """占位记录不改变账号状态（同步并未真的失败）。"""
        self._run_sync_blocked_by_lease()
        self.db.expire_all()
        account = self.db.query(CloudAccount).filter(CloudAccount.id == 7).one()
        self.assertEqual(account.status, "active")
        self.assertEqual(account.last_sync_at or "", "")

    def test_missing_account_writes_no_skipped_record(self):
        """账号不存在：不写占位记录（避免脏数据）。"""
        with patch.object(bill_sync, "SessionLocal", self.TestingSession), \
             patch.object(bill_sync, "acquire_lease", return_value=False):
            result = bill_sync.sync_account_cycle(999, "2026-09", trigger="manual")
        self.assertIsNone(result)
        self.assertEqual(self.db.query(BillSyncRun).count(), 0)

    def test_inprocess_lock_branch_stays_silent(self):
        """同进程锁被占（原有行为）：静默 None，不写 skipped。"""
        lock = bill_sync._account_lock(7)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with patch.object(bill_sync, "SessionLocal", self.TestingSession):
                result = bill_sync.sync_account_cycle(7, "2026-09", trigger="manual")
            self.assertIsNone(result)
            self.assertEqual(self.db.query(BillSyncRun).count(), 0)
        finally:
            # sync_account_cycle 的 finally 会尝试释放这把锁（含未持有的场景），
            # 这里兜底忽略二次释放。
            try:
                lock.release()
            except RuntimeError:
                pass

    def test_successful_lease_path_unaffected(self):
        """拿到租约的正常路径不受影响（mock 掉凭证加载直接失败，重点是有 running 记录）。"""
        with patch.object(bill_sync, "SessionLocal", self.TestingSession), \
             patch.object(bill_sync, "acquire_lease", return_value=True), \
             patch.object(bill_sync, "lease_owner", return_value="owner-test"), \
             patch.object(bill_sync, "release_lease", lambda *a, **k: None), \
             patch.object(
                 bill_sync, "load_account_keys",
                 lambda db, account: ("", "", "未绑定 AK/SK 凭证"),
             ):
            result = bill_sync.sync_account_cycle(7, "2026-09", trigger="manual")
        self.assertIsInstance(result, BillSyncRun)
        # 返回的 run 绑定在 sync 内部会话上（已关闭），状态以落库记录为准。
        statuses = [r.status for r in self.db.query(BillSyncRun).all()]
        self.assertEqual(statuses, ["failed"])  # 凭证缺失失败，但走的是真实执行分支


if __name__ == "__main__":
    unittest.main()
