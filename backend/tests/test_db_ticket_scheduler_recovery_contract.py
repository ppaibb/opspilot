"""SQL 工单定时执行与中断恢复契约测试。

修复回归钉死（复审确认的三个 bug）：
1. P0 定时工单错库：_run_due_tickets 必须用"工单目标库"（ticket.database
   优先，空才落实例默认库）——此前写死 instance.database，定时单会绕过
   审批人选择的目标库直接在默认库上执行；
2. P1 执行结果通知丢人：提交人按 username OR name 双匹配（与审批通知同款），
   工单里存展示名时此前查不到人、结果通知整个丢掉；
3. P1 卡死恢复：executing 且心跳超时（或心跳为空的历史行）的工单在启动 /
   每轮被收敛为 failed 并注明"结果未知，请人工核对目标库"。

测试套路：mock 收窄到真正的 DB 连接层（db_ops._build_connection），
execute_statements 真实跑起来，让 conn_args（尤其目标库名）可断言——
把 execute_statements 整个 mock 掉是此前的盲区，错库 bug 因此漏网。
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401  灌满 Base.metadata
from app.models.database import DbInstance, DbTicket
from app.models.notification import UserNotification
from app.models.user import User
from app.services.application_time import format_app_datetime, now_dt

import app.api.database as db_api


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ── 假连接层：execute_statements 真实逻辑在假连接上跑 ─────────────────────────

class _FakeCursor:
    description = None
    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, *args, **kwargs):
        return None

    def fetchmany(self, size):
        return []


class _FakeConnection:
    def __init__(self, info: dict):
        self.info = info

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _make_instance(db, *, database="orders") -> DbInstance:
    instance = DbInstance(
        name="订单库", db_type="mysql", host="10.0.0.5", port=3306,
        database=database, username="deployer", password="",
        status="ok", created_at="2026-09-21 10:00:00",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _make_due_ticket(db, instance_id: int, *, database: str | None = None) -> DbTicket:
    ticket = DbTicket(
        instance_id=instance_id,
        title="清理脏数据",
        sql_text="DELETE FROM orders WHERE id = 1",
        ticket_type="change",
        status="approved",
        submitter="小张",
        database=database if database is not None else "",
        execute_after="2000-01-01 00:00:00",
        created_at="2026-09-21 10:00:00",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


# ── 1. P0 定时工单目标库 ──────────────────────────────────────────────────────

class SchedulerTargetDatabaseContract(unittest.TestCase):
    """定时执行必须连工单指定的目标库，而不是实例默认库。"""

    def setUp(self):
        self.db = _make_db()
        self.instance = _make_instance(self.db, database="orders")

    def tearDown(self):
        self.db.close()

    def _run_and_collect_connections(self):
        connections: list[dict] = []

        def fake_build_connection(db_type, host, port, username, password, database):
            connections.append({
                "db_type": db_type, "host": host, "port": port,
                "username": username, "password": password, "database": database,
            })
            return _FakeConnection(connections[-1])

        with patch("app.services.db_ops._build_connection", side_effect=fake_build_connection), \
             patch("app.db.database.SessionLocal", sessionmaker(bind=self.db.get_bind())):
            db_api._run_due_tickets()
        return connections

    def test_due_ticket_executes_against_ticket_database(self):
        """工单指定库 ticket_db：所有连接（备份快照 + 执行）都连 ticket_db。"""
        ticket = _make_due_ticket(self.db, self.instance.id, database="ticket_db")
        connections = self._run_and_collect_connections()
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one()
        self.assertEqual(row.status, "success")
        self.assertEqual(row.executor, "scheduler")
        # 快照与执行至少各一次连接，库名必须全部是工单目标库。
        self.assertTrue(connections, "执行路径应产生真实连接调用")
        for conn in connections:
            self.assertEqual(
                conn["database"], "ticket_db",
                f"定时工单连错了库: {conn['database']}（应为工单目标库 ticket_db）",
            )

    def test_blank_ticket_database_falls_back_to_instance_database(self):
        """工单未指定库（空串）：回落实例默认库，与 execute_ticket 行为一致。"""
        ticket = _make_due_ticket(self.db, self.instance.id, database="")
        connections = self._run_and_collect_connections()
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one()
        self.assertEqual(row.status, "success")
        for conn in connections:
            self.assertEqual(conn["database"], "orders")


# ── 2. P1 执行结果通知双匹配 ──────────────────────────────────────────────────

class NotifyResultDoubleMatchContract(unittest.TestCase):
    """_notify_ticket_result：username OR name 双匹配，展示名提交人不再丢通知。"""

    def setUp(self):
        self.db = _make_db()

    def tearDown(self):
        self.db.close()

    def _make_submitter(self, *, username: str, name: str) -> User:
        user = User(
            username=username, name=name, email=f"{username}@example.com",
            hashed_password="fake", role="开发", status="active",
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def _make_ticket(self, submitter: str) -> DbTicket:
        ticket = DbTicket(
            instance_id=1, title="删除脏数据", sql_text="DELETE FROM t WHERE id=1",
            ticket_type="change", status="failed", submitter=submitter,
            executed_at=format_app_datetime(now_dt()),
        )
        self.db.add(ticket)
        self.db.commit()
        self.db.refresh(ticket)
        return ticket

    def _notices(self, user_id: int) -> list[UserNotification]:
        self.db.expire_all()
        return (
            self.db.query(UserNotification)
            .filter(UserNotification.user_id == user_id)
            .all()
        )

    def test_notice_reaches_submitter_by_display_name(self):
        """工单 submitter 存展示名（≠username）：结果通知必须送达。"""
        user = self._make_submitter(username="zhang01", name="小张")
        ticket = self._make_ticket("小张")
        db_api._notify_ticket_result(self.db, ticket)
        rows = [n for n in self._notices(user.id) if "执行" in n.title]
        self.assertEqual(len(rows), 1)
        self.assertIn("失败", rows[0].title)

    def test_notice_reaches_submitter_by_username(self):
        """工单 submitter 存账号名：原路径保持可用。"""
        user = self._make_submitter(username="zhang01", name="小张")
        ticket = self._make_ticket("zhang01")
        db_api._notify_ticket_result(self.db, ticket)
        rows = [n for n in self._notices(user.id) if "执行" in n.title]
        self.assertEqual(len(rows), 1)

    def test_unknown_submitter_produces_no_notice(self):
        """提交人既不是 username 也不是 name：静默跳过，不误发。"""
        user = self._make_submitter(username="zhang01", name="小张")
        ticket = self._make_ticket("陌生提交人")
        db_api._notify_ticket_result(self.db, ticket)
        self.assertEqual(self._notices(user.id), [])


# ── 3. P1 卡死在 executing 的工单恢复 ────────────────────────────────────────

class StaleExecutingRecoveryContract(unittest.TestCase):
    """recover_stale_executing_tickets：心跳超时/为空 → failed + 结果未知。"""

    def setUp(self):
        self.db = _make_db()
        self.instance = _make_instance(self.db)

    def tearDown(self):
        self.db.close()

    def _make_executing(self, *, heartbeat: str) -> DbTicket:
        ticket = DbTicket(
            instance_id=self.instance.id, title="更新配置", sql_text="UPDATE t SET a=1 WHERE id=1",
            ticket_type="change", status="executing", submitter="小张",
            heartbeat_at=heartbeat,
        )
        self.db.add(ticket)
        self.db.commit()
        self.db.refresh(ticket)
        return ticket

    def test_empty_heartbeat_executing_is_recovered(self):
        """心跳为空（心跳机制上线前认领的历史行）→ 恢复为 failed。"""
        ticket = self._make_executing(heartbeat="")
        recovered = db_api.recover_stale_executing_tickets(self.db)
        self.assertEqual(recovered, 1)
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one()
        self.assertEqual(row.status, "failed")
        self.assertIn("执行器中断", str(row.exec_result[0]["error"]))
        self.assertIn("结果未知", str(row.exec_result[0]["error"]))
        self.assertTrue(row.executed_at)

    def test_fresh_heartbeat_executing_is_untouched(self):
        """心跳新鲜（正在正常执行）：不误伤。"""
        ticket = self._make_executing(heartbeat=format_app_datetime(now_dt()))
        recovered = db_api.recover_stale_executing_tickets(self.db)
        self.assertEqual(recovered, 0)
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one()
        self.assertEqual(row.status, "executing")

    def test_old_heartbeat_executing_is_recovered(self):
        """心跳超过 2 小时未刷新（进程已死）：收敛为 failed。"""
        stale_time = format_app_datetime(now_dt() - timedelta(hours=3))
        ticket = self._make_executing(heartbeat=stale_time)
        recovered = db_api.recover_stale_executing_tickets(self.db)
        self.assertEqual(recovered, 1)
        self.db.expire_all()
        row = self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one()
        self.assertEqual(row.status, "failed")

    def test_recovery_is_idempotent(self):
        """重复调用不重复改写：第二轮 count=0，状态保持 failed。"""
        ticket = self._make_executing(heartbeat="")
        self.assertEqual(db_api.recover_stale_executing_tickets(self.db), 1)
        first_result = str(
            self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one().exec_result
        )
        self.assertEqual(db_api.recover_stale_executing_tickets(self.db), 0)
        self.assertEqual(
            str(self.db.query(DbTicket).filter(DbTicket.id == ticket.id).one().exec_result),
            first_result,
        )

    def test_run_due_tickets_recovers_stale_before_scanning(self):
        """定时 runner 每轮开头先恢复卡死单：同一轮里恢复 + 执行互不干扰。"""
        stale = self._make_executing(heartbeat="")
        due = _make_due_ticket(self.db, self.instance.id, database="")

        connections: list[dict] = []

        def fake_build_connection(db_type, host, port, username, password, database):
            connections.append({"database": database})
            return _FakeConnection(connections[-1])

        with patch("app.services.db_ops._build_connection", side_effect=fake_build_connection), \
             patch("app.db.database.SessionLocal", sessionmaker(bind=self.db.get_bind())):
            db_api._run_due_tickets()

        self.db.expire_all()
        self.assertEqual(
            self.db.query(DbTicket).filter(DbTicket.id == stale.id).one().status,
            "failed",
        )
        self.assertEqual(
            self.db.query(DbTicket).filter(DbTicket.id == due.id).one().status,
            "success",
        )


if __name__ == "__main__":
    unittest.main()
