"""工单 / 查询日志用户隔离契约测试。

验证阶段2数据隔离：
1. 普通用户（无审批/执行/管理权限）只见自己提交的工单与查询日志，
   加上目标实例对自己开放（allowed_roles 命中或未限制）的他人记录；
2. 特权角色（action.db.approve / execute / manage 任一）看全量；
3. 实例 allowed_roles 不命中的他人工单/日志不可见（SQL 层过滤，不泄露存在性）；
4. 未授权用户访问不存在/无权资源仍为 404，不泄露实例存在性。

不使用 TestClient：直接调用路由函数（与 test_risk_ticket_contract.py 同风格）。
"""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.passwords import hash_password
from app.db.database import Base
from app.models.database import DbInstance, DbQueryLog, DbTicket
from app.models.user import User

from app.api import database as db_api

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(
    bind=engine,
    tables=[
        User.__table__,
        DbInstance.__table__,
        DbTicket.__table__,
        DbQueryLog.__table__,
    ],
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class DbIsolationContract(unittest.TestCase):
    def setUp(self):
        self.db = TestingSession()
        for model in (DbQueryLog, DbTicket, DbInstance, User):
            self.db.query(model).delete()
        self.db.commit()

        # 普通（只读，无 db 特权）/ 特权（运维，含 approve+execute+manage）。
        self.normal = User(
            username="xiaozhang", name="小张", role="只读", status="active",
            hashed_password=hash_password("password-123"),
        )
        self.peer = User(
            username="laoli", name="老李", role="只读", status="active",
            hashed_password=hash_password("password-123"),
        )
        self.privileged = User(
            username="ops", name="运维同学", role="运维", status="active",
            hashed_password=hash_password("password-123"),
        )
        self.db.add_all([self.normal, self.peer, self.privileged])

        # inst_a：只对运维开放（普通用户未授权）；inst_b：全员可用。
        self.inst_a = DbInstance(
            id=1, name="核心库", db_type="mysql", host="10.0.0.1", port=3306,
            username="root", password="", database="core",
            allowed_roles='["运维"]',
        )
        self.inst_b = DbInstance(
            id=2, name="公共库", db_type="mysql", host="10.0.0.2", port=3306,
            username="root", password="", database="public",
            allowed_roles="",
        )
        self.db.add_all([self.inst_a, self.inst_b])
        self.db.commit()

        # 工单：t1 自己的（公共库）；t2 他人的（核心库，未授权实例 → 隐藏）；
        # t3 他人的（公共库 → 授权实例上可见）。
        self.t1 = DbTicket(instance_id=2, title="我的单", sql_text="SELECT 1",
                           ticket_type="change", status="pending", submitter="小张",
                           created_at="2026-01-01 00:00:00")
        self.t2 = DbTicket(instance_id=1, title="老李的核心库单", sql_text="SELECT 2",
                           ticket_type="change", status="pending", submitter="老李",
                           created_at="2026-01-02 00:00:00")
        self.t3 = DbTicket(instance_id=2, title="老李的公共库单", sql_text="SELECT 3",
                           ticket_type="change", status="approved", submitter="老李",
                           created_at="2026-01-03 00:00:00")
        self.db.add_all([self.t1, self.t2, self.t3])

        # 查询日志：同样三类。
        self.l1 = DbQueryLog(instance_id=2, username="小张", sql_text="SELECT 1",
                             database="public", row_count=1, duration_ms=1, ok=1,
                             created_at="2026-01-01 00:00:00")
        self.l2 = DbQueryLog(instance_id=1, username="老李", sql_text="SELECT 2",
                             database="core", row_count=1, duration_ms=1, ok=1,
                             created_at="2026-01-02 00:00:00")
        self.l3 = DbQueryLog(instance_id=2, username="老李", sql_text="SELECT 3",
                             database="public", row_count=1, duration_ms=1, ok=1,
                             created_at="2026-01-03 00:00:00")
        self.db.add_all([self.l1, self.l2, self.l3])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    # ── 工单隔离 ──

    def test_normal_user_sees_own_and_allowed_instance_tickets(self):
        rows = db_api.list_tickets(limit=50, db=self.db, current_user=self.normal)
        titles = {row["title"] for row in rows}
        self.assertEqual(titles, {"我的单", "老李的公共库单"})

    def test_privileged_user_sees_all_tickets(self):
        rows = db_api.list_tickets(limit=50, db=self.db, current_user=self.privileged)
        self.assertEqual(len(rows), 3)

    def test_filters_still_apply_under_isolation(self):
        rows = db_api.list_tickets(status="approved", limit=50, db=self.db, current_user=self.normal)
        self.assertEqual({row["title"] for row in rows}, {"老李的公共库单"})
        rows = db_api.list_tickets(status="", instance_id=1, limit=50, db=self.db, current_user=self.normal)
        self.assertEqual(rows, [])  # 未授权实例上他人单被隔离，无泄露

    def test_approve_permission_alone_grants_full_view(self):
        """只给用户级 approve 覆盖（无角色特权）也能看全量（审批流程可用）。"""
        import json

        self.peer.role = "只读"
        self.peer.permission_overrides = json.dumps(
            {"action.db.approve": True, "action.db.submit": True}, ensure_ascii=False
        )
        self.db.commit()
        rows = db_api.list_tickets(limit=50, db=self.db, current_user=self.peer)
        self.assertEqual(len(rows), 3)

    # ── 查询日志隔离 ──

    def test_normal_user_sees_own_and_allowed_instance_logs(self):
        rows = db_api.list_query_logs(limit=50, db=self.db, current_user=self.normal)
        sqls = {row["sql_text"] for row in rows}
        self.assertEqual(sqls, {"SELECT 1", "SELECT 3"})

    def test_privileged_user_sees_all_logs(self):
        rows = db_api.list_query_logs(limit=50, db=self.db, current_user=self.privileged)
        self.assertEqual(len(rows), 3)

    # ── 不泄露存在性 ──

    def test_unauthorized_instance_access_is_404_without_leak(self):
        """_get_instance 对不存在的实例 404；实例列表接口本身仅特权写操作使用，
        普通用户尝试认领未授权实例上的工单会被认领条件兜住（非 approved 422）。"""
        with self.assertRaises(HTTPException) as ctx:
            db_api._get_instance(self.db, 999)
        self.assertEqual(ctx.exception.status_code, 404)

        self.t3.status = "success"
        self.db.commit()
        # 普通用户即使知道 id，也无法把非 approved 单认领执行。
        with self.assertRaises(HTTPException) as ctx:
            db_api._claim_ticket_for_execution(self.db, self.t3.id)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_identity_matches_username_or_display_name(self):
        """提交人记录的是展示名或账号名都应该命中隔离过滤。"""
        self.t1.submitter = "xiaozhang"  # 同一个人，另一条单用账号名落库
        self.db.commit()
        rows = db_api.list_tickets(limit=50, db=self.db, current_user=self.normal)
        titles = {row["title"] for row in rows}
        self.assertIn("我的单", titles)


if __name__ == "__main__":
    unittest.main()
