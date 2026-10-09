"""SQL 工单原子状态转换 + 自助查询 readonly 接线契约测试。

验证阶段2并发安全：
1. _claim_ticket_for_execution 原子认领：approved → executing 只有一个并发方能成功；
2. 非 approved / 不存在的工单认领一律 422，不泄露状态；
3. 定时执行器 _run_due_tickets 与接口走同一个认领入口，重复扫描不会重复执行；
4. 自助查询入口必须传 readonly=True（纵深防御接线检查）。

不使用 TestClient：直接调用路由函数与内部 helper（与 test_risk_ticket_contract.py 同风格）。
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.passwords import hash_password
from app.db.database import Base
from app.models.database import DbInstance, DbQueryGrant, DbTicket
from app.models.user import User

from app.api import database as db_api

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(
    bind=engine,
    tables=[
        User.__table__,
        DbInstance.__table__,
        DbTicket.__table__,
        DbQueryGrant.__table__,  # _run_due_tickets 顺带回收到期查询授权
    ],
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def make_ticket(**overrides) -> DbTicket:
    defaults = dict(
        instance_id=1,
        title="删除脏数据",
        sql_text="DELETE FROM orders WHERE id = 1",
        ticket_type="change",
        status="approved",
        submitter="小张",
        created_at="2026-01-01 00:00:00",
    )
    defaults.update(overrides)
    return DbTicket(**defaults)


class ClaimTicketAtomicContract(unittest.TestCase):
    """原子认领：并发只有一个成功，状态机正确。"""

    def setUp(self):
        self.db = TestingSession()
        for model in (DbTicket, DbInstance, User):
            self.db.query(model).delete()
        self.db.commit()
        self.instance = DbInstance(
            id=1, name="订单库", db_type="mysql", host="127.0.0.1", port=3306,
            username="root", password="", database="orders",
        )
        self.db.add(self.instance)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_approved_ticket_claim_flips_to_executing(self):
        ticket = make_ticket()
        self.db.add(ticket)
        self.db.commit()
        claimed = db_api._claim_ticket_for_execution(self.db, ticket.id)
        self.assertEqual(claimed.status, "executing")
        self.db.expire_all()
        self.assertEqual(
            self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one().status,
            "executing",
        )

    def test_two_concurrent_claims_only_one_wins(self):
        """两个 session 并发认领同一张 approved 工单，只有一个成功。"""
        ticket = make_ticket()
        self.db.add(ticket)
        self.db.commit()
        ticket_id = ticket.id

        db_first = TestingSession()
        db_second = TestingSession()
        try:
            winner = db_api._claim_ticket_for_execution(db_first, ticket_id)
            self.assertEqual(winner.status, "executing")
            with self.assertRaises(HTTPException) as ctx:
                db_api._claim_ticket_for_execution(db_second, ticket_id)
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("工单不存在或已被执行", ctx.exception.detail)
        finally:
            db_first.close()
            db_second.close()

    def test_claim_missing_ticket_is_422(self):
        with self.assertRaises(HTTPException) as ctx:
            db_api._claim_ticket_for_execution(self.db, 99999)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_claim_non_approved_ticket_is_422(self):
        for status in ("pending", "executing", "success", "failed", "rejected"):
            with self.subTest(status=status):
                ticket = make_ticket(status=status)
                self.db.add(ticket)
                self.db.commit()
                with self.assertRaises(HTTPException) as ctx:
                    db_api._claim_ticket_for_execution(self.db, ticket.id)
                self.assertEqual(ctx.exception.status_code, 422)
                self.db.expire_all()
                # 状态未被认领动作篡改。
                self.assertEqual(
                    self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one().status,
                    status,
                )
                self.db.query(DbTicket).filter(DbTicket.id == ticket.id).delete()
                self.db.commit()


class SchedulerClaimContract(unittest.TestCase):
    """定时执行器走同一认领入口：到期的 approved 单执行一次，重复扫描不重复执行。"""

    def setUp(self):
        self.db = TestingSession()
        for model in (DbTicket, DbInstance, User):
            self.db.query(model).delete()
        self.db.commit()
        self.db.add(DbInstance(
            id=1, name="订单库", db_type="mysql", host="127.0.0.1", port=3306,
            username="root", password="", database="orders",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _patch_exec_side(self):
        """把真实 DB 执行换成本地假实现：快照为空、执行成功。"""
        return (
            patch.object(db_api, "snapshot_dangerous_statements", lambda *a, **k: {"items": []}),
            patch.object(
                db_api,
                "execute_statements",
                lambda *a, **k: {"ok": True, "results": [], "total_duration_ms": 1},
            ),
            patch("app.db.database.SessionLocal", TestingSession),
        )

    def test_due_ticket_executes_once_and_records_scheduler(self):
        due = make_ticket(execute_after="2000-01-01 00:00:00")
        future = make_ticket(execute_after="2999-01-01 00:00:00", sql_text="DELETE FROM x WHERE 0=1")
        self.db.add_all([due, future])
        self.db.commit()

        p1, p2, p3 = self._patch_exec_side()
        with p1, p2, p3:
            db_api._run_due_tickets()
            self.db.expire_all()
            row_due = self.db.query(DbTicket).filter(DbTicket.id == due.id).one()
            self.assertEqual(row_due.status, "success")
            self.assertEqual(row_due.executor, "scheduler")

            # 再扫一轮：已执行的工单不再是 approved，不会被认领二次执行。
            executed_calls = []

            def _counting_execute(*args, **kwargs):
                executed_calls.append(args)
                return {"ok": True, "results": [], "total_duration_ms": 1}

            with patch.object(db_api, "snapshot_dangerous_statements", lambda *a, **k: {"items": []}), \
                 patch.object(db_api, "execute_statements", _counting_execute), \
                 patch("app.db.database.SessionLocal", TestingSession):
                db_api._run_due_tickets()
            self.assertEqual(executed_calls, [])

        row_future = self.db.query(DbTicket).filter(DbTicket.id == future.id).one()
        self.assertEqual(row_future.status, "approved")  # 未到期不动

    def test_due_ticket_claimed_by_api_is_skipped_by_scheduler(self):
        """定时扫描期间工单被人工认领（executing），调度器跳过不重复执行。"""
        due = make_ticket(execute_after="2000-01-01 00:00:00")
        self.db.add(due)
        self.db.commit()
        ticket_id = due.id

        # 人工先认领走（executing）。
        other = TestingSession()
        db_api._claim_ticket_for_execution(other, ticket_id)
        other.close()

        p1, p2, p3 = self._patch_exec_side()
        with p1, p2, p3:
            db_api._run_due_tickets()  # 不应抛异常，也不应改写执行结果
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket_id).one()
        self.assertEqual(row.status, "executing")
        self.assertEqual(row.executor or "", "")


class ReadonlyWiringContract(unittest.TestCase):
    """自助查询必须以 readonly=True 执行（纵深防御接线）。"""

    def test_run_query_passes_readonly_flag(self):
        import inspect

        source = inspect.getsource(db_api.run_query)
        self.assertIn("readonly=True", source)

    def test_execute_statements_supports_readonly_kwarg(self):
        """db_ops.execute_statements 保留 readonly 形参（上游契约）。"""
        import inspect

        from app.services.db_ops import execute_statements

        params = inspect.signature(execute_statements).parameters
        self.assertIn("readonly", params)
        self.assertIs(params["readonly"].default, False)


if __name__ == "__main__":
    unittest.main()
