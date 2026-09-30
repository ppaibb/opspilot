"""数据库管理 Yearning 对齐（分权 / 多级审批流 / 查询授权）契约测试。

覆盖四条主线：
1. RBAC 拆分：action.db.submit 从 execute 分出，存量角色自动补齐（legacy 兼容）；
2. 自审批拦截：提交人不能审自己的单（manage 豁免），提交人不收待审批通知；
3. 多级审批流：配置链 → 错误角色 403 → 逐级推进 → 终审落地；
4. 查询授权：query 工单免 SQL 提交 → 终审自动生成时限授权 → 无授权查询 403
   → 管理员豁免 → 手动授权 / 吊销 / 到期翻转。

测试套路与仓库一致：in-memory SQLite 直接建表（路由函数内部会 commit，
不能靠 rollback 收尾），路由函数直接调用绕过 Depends。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401  灌满 Base.metadata
from app.models.database import DbInstance, DbQueryGrant, DbTicket
from app.models.user import User
from app.core.rbac import (
    PERMISSION_DEFAULTS,
    resolve_effective_permissions_from_values,
)
from app.core.security import require_permissions
import app.api.database as db_api


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_user(db, *, username="dev1", name="开发者一号", role="开发", overrides=""):
    user = User(
        username=username, name=name, email=f"{username}@example.com",
        hashed_password="fake", role=role,
        permission_overrides=overrides or "",
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_instance(db, **kwargs):
    instance = DbInstance(
        name=kwargs.get("name", "订单库"), db_type="mysql", host="10.0.0.5", port=3306,
        database=kwargs.get("database", "orders"), username="deployer", password="",
        allowed_roles="", status="ok", created_at="2026-09-21 10:00:00",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


# ── 1. RBAC 拆分与 legacy 兼容 ────────────────────────────────────────────────

class RbacSplitContract(unittest.TestCase):
    def test_submit_key_registered(self):
        """action.db.submit 已注册进权限目录，execute 描述改为纯执行。"""
        self.assertIn("action.db.submit", PERMISSION_DEFAULTS)
        self.assertIn("action.db.execute", PERMISSION_DEFAULTS)
        self.assertIn("action.db.approve", PERMISSION_DEFAULTS)

    def test_legacy_execute_implies_submit(self):
        """存量角色只有 execute：自动补 submit，升级后不会凭空失去提交能力。"""
        perms = resolve_effective_permissions_from_values("运维", "")
        self.assertTrue(perms.get("action.db.execute"))
        self.assertTrue(perms.get("action.db.submit"))

    def test_no_execute_no_submit(self):
        """没有 execute 的角色（如开发模板）不应自动获得 submit。"""
        perms = resolve_effective_permissions_from_values("开发", "")
        self.assertFalse(perms.get("action.db.execute"))
        self.assertFalse(perms.get("action.db.submit"))
        self.assertFalse(perms.get("action.db.approve"))

    def test_permission_guard_rejects_without_submit(self):
        """无 submit 权限的用户过不了 require_permissions 守卫。"""
        guard = require_permissions("action.db.submit")
        user = SimpleNamespace(role="开发", permission_overrides="", status="active")
        with self.assertRaises(HTTPException) as ctx:
            guard(user)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_permission_guard_allows_user_override(self):
        """用户级覆盖可以单独授予 submit（不用改角色模板）。"""
        guard = require_permissions("action.db.submit")
        import json as _json
        overrides = _json.dumps({"action.db.submit": True})
        user = SimpleNamespace(role="只读", permission_overrides=overrides, status="active")
        self.assertIs(guard(user), user)


# ── 2. 自审批拦截 ─────────────────────────────────────────────────────────────

class SelfReviewGuardTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self.instance = _make_instance(self.db)
        # 提交人：运维角色带 submit（用覆盖模拟，避开模板差异）
        import json as _json
        self.submitter = _make_user(
            self.db, username="op1", name="运维一号", role="运维",
            overrides=_json.dumps({"action.db.submit": True}),
        )

    def tearDown(self):
        self.db.close()

    def _submit(self) -> dict:
        """提交一张变更工单（路由函数返回序列化 dict）。"""
        return db_api.create_ticket(
            {"instance_id": self.instance.id, "sql_text": "UPDATE t SET a=1 WHERE id=1", "ticket_type": "change"},
            db=self.db, current_user=self.submitter,
        )

    def test_submitter_cannot_review_own_ticket(self):
        """提交人本人（含同账号）审批自己的单 → 403，即使他有审批权。"""
        ticket = self._submit()
        # 用提交人自己的账号直接审（他有 approve 覆盖也不行）。
        self.submitter.permission_overrides = '{"action.db.submit": true, "action.db.approve": true}'
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.submitter)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_same_name_user_blocked(self):
        """同 name 不同账号（重名场景）也按提交人名拦截。"""
        ticket = self._submit()
        twin = _make_user(
            self.db, username="twin", name="运维一号", role="运维",
            overrides='{"action.db.approve": true}',
        )
        with self.assertRaises(HTTPException) as ctx:
            db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=twin)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_submitter_blocked_even_with_approve(self):
        """提交人自己有审批权时同样被拦（旧版允许自审批，现收紧）。"""
        import json as _json
        ticket = self._submit()
        self.submitter.permission_overrides = _json.dumps({"action.db.submit": True, "action.db.approve": True})
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.submitter)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_manage_exemption_can_review_own_ticket(self):
        """豁免只认"管理员"角色本身：运维模板带 manage 也不能借此自审。"""
        ticket = self._submit()
        admin = _make_user(self.db, username="op1b", name="运维一号", role="管理员")
        result = db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=admin)
        self.assertEqual(result["status"], "approved")

    def test_ops_with_manage_override_cannot_self_review(self):
        """运维有 action.db.manage 覆盖也不能自审（豁免不看 manage 权限）。"""
        ticket = self._submit()
        self.submitter.permission_overrides = '{"action.db.submit": true, "action.db.approve": true, "action.db.manage": true}'
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.submitter)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_submitter_not_notified_for_pending_review(self):
        """待审批通知不发给提交人本人（旧版会发，自审批禁止后通知也应排除）。"""
        from app.models.notification import UserNotification
        # 提交人自己也有审批权：旧版他会收到待审批通知，新版必须排除。
        import json as _json
        self.submitter.permission_overrides = _json.dumps({"action.db.submit": True, "action.db.approve": True})
        self.db.commit()
        self._submit()
        rows = self.db.query(UserNotification).filter(UserNotification.user_id == self.submitter.id).all()
        self.assertEqual([r.title for r in rows if "待审批" in r.title], [])


# ── 3. 多级审批流 ─────────────────────────────────────────────────────────────

class MultiStepApprovalTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self.instance = _make_instance(self.db)
        self.submitter = _make_user(self.db, username="dev1", name="开发者一号", role="开发")
        self.op_user = _make_user(
            self.db, username="op1", name="运维一号", role="运维",
            overrides='{"action.db.approve": true}',
        )
        self.op_user2 = _make_user(
            self.db, username="op2", name="运维二号", role="运维",
            overrides='{"action.db.approve": true}',
        )
        self.admin_user = _make_user(self.db, username="admin1", name="管理员一号", role="管理员")

    def tearDown(self):
        self.db.close()

    def _configure_flow(self, steps):
        return db_api.update_approval_flows(
            {"change": {"steps": steps}}, db=self.db, current_user=self.admin_user,
        )

    def _submit(self) -> DbTicket:
        return db_api.create_ticket(
            {"instance_id": self.instance.id, "sql_text": "UPDATE t SET a=1 WHERE id=1", "ticket_type": "change"},
            db=self.db, current_user=self.submitter,
        )

    def test_config_roundtrip(self):
        """审批流配置保存与回读；角色列表来自角色模板。"""
        result = self._configure_flow(["运维", "管理员"])
        self.assertEqual(result["change"]["steps"], ["运维", "管理员"])
        self.assertIn("运维", result["roles"])
        self.assertEqual(result["query"]["steps"], [])

    def test_config_rejects_duplicate_roles(self):
        with self.assertRaises(HTTPException) as ctx:
            self._configure_flow(["运维", "运维"])
        self.assertEqual(ctx.exception.status_code, 422)

    def test_wrong_role_cannot_review_step(self):
        """多级审批：第一级是运维，管理员来审 → 403 指明应由哪个角色审。"""
        self._configure_flow(["运维", "管理员"])
        ticket = self._submit()
        self.assertEqual(ticket["approval_steps"], ["运维", "管理员"])
        with self.assertRaises(HTTPException) as ctx:
            db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.admin_user)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("运维", ctx.exception.detail)

    def test_multi_step_progression(self):
        """两级链：第一级通过仍 pending（step 推进），第二级通过才 approved。"""
        self._configure_flow(["运维", "管理员"])
        ticket = self._submit()
        # 第一级：运维批准 → 仍 pending，进入第 2 级
        result = db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.op_user)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["current_step"], 2)
        self.assertEqual(len(result["approval_log"]), 1)
        # 第二级：管理员批准 → approved
        result = db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.admin_user)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reviewer"], "管理员一号")
        self.assertEqual(len(result["approval_log"]), 2)
        # 执行通道放行（mock 掉真实数据库连接与备份快照）
        with patch.object(db_api, "snapshot_dangerous_statements", return_value={"items": []}), \
             patch.object(db_api, "execute_statements", return_value={"ok": True, "results": []}):
            executed = db_api.execute_ticket(ticket["id"], db=self.db, current_user=self.admin_user)
        self.assertEqual(executed["status"], "success")

    def test_reject_at_first_step(self):
        """第一级驳回即终止，状态机不进入后续级。"""
        self._configure_flow(["运维", "管理员"])
        ticket = self._submit()
        result = db_api.review_ticket(ticket["id"], {"decision": "reject", "note": "SQL 太重"}, db=self.db, current_user=self.op_user)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["review_note"], "SQL 太重")

    def test_second_step_notified_after_first_approval(self):
        """第一级批准后，下一级（管理员角色）收到通知；运维只在提交时收过一次。"""
        from app.models.notification import UserNotification
        self._configure_flow(["运维", "管理员"])
        ticket = self._submit()
        # 提交时：第一级运维收到 1 条待审批通知。
        op_notices = [
            r for r in self.db.query(UserNotification).filter(UserNotification.user_id == self.op_user.id).all()
            if "待审批" in r.title
        ]
        self.assertEqual(len(op_notices), 1)
        db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.op_user)
        # 第一级批准后：管理员（第二级）收到待审批通知；运维不再有新通知。
        admin_notices = [
            r for r in self.db.query(UserNotification).filter(UserNotification.user_id == self.admin_user.id).all()
            if "待审批" in r.title
        ]
        self.assertEqual(len(admin_notices), 1, "第二级审批人应收到通知")
        op_notices_after = [
            r for r in self.db.query(UserNotification).filter(UserNotification.user_id == self.op_user.id).all()
            if "待审批" in r.title
        ]
        self.assertEqual(len(op_notices_after), 1, "已审完第一级的运维不应再收同一单的通知")
        # 提交人本人从未收到待审批通知（自审批已禁止）。
        submitter_notices = [
            r for r in self.db.query(UserNotification).filter(UserNotification.user_id == self.submitter.id).all()
            if "待审批" in r.title
        ]
        self.assertEqual(submitter_notices, [])

    def test_single_mode_without_config(self):
        """未配置审批流 = 单级模式：任意审批人一步到位（历史行为兼容）。"""
        ticket = self._submit()
        self.assertEqual(ticket["approval_steps"], [])
        result = db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.op_user)
        self.assertEqual(result["status"], "approved")


# ── 4. 查询授权 ───────────────────────────────────────────────────────────────

class QueryGrantTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self.instance = _make_instance(self.db)
        self.submitter = _make_user(self.db, username="dev1", name="开发者一号", role="开发")
        self.approver = _make_user(
            self.db, username="op1", name="运维一号", role="运维",
            overrides='{"action.db.approve": true}',
        )
        self.admin_user = _make_user(self.db, username="admin1", name="管理员一号", role="管理员")

    def tearDown(self):
        self.db.close()

    def _submit_query_ticket(self):
        return db_api.create_ticket(
            {
                "instance_id": self.instance.id,
                "ticket_type": "query",
                "database": "orders",
                "grant_tables": ["users", "orders"],
                "grant_hours": 12,
                "grant_reason": "排查线上问题",
            },
            db=self.db, current_user=self.submitter,
        )

    def test_query_ticket_without_sql_passes_audit(self):
        """查询授权申请不需要 SQL：跳过规则引擎（旧版空 SQL 会被 R0 拦）。"""
        ticket = self._submit_query_ticket()
        self.assertEqual(ticket["status"], "pending")
        self.assertEqual(ticket["database"], "orders")
        self.assertEqual(ticket["grant_detail"]["hours"], 12)
        self.assertEqual(ticket["grant_detail"]["tables"], ["users", "orders"])

    def test_query_ticket_requires_database(self):
        with self.assertRaises(HTTPException) as ctx:
            db_api.create_ticket(
                {"instance_id": self.instance.id, "ticket_type": "query"},
                db=self.db, current_user=self.submitter,
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_final_approval_creates_grant_and_ticket_succeeds(self):
        """终审通过 → 授权自动生效（时限正确）、工单直接终态 success。"""
        ticket = self._submit_query_ticket()
        result = db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.approver)
        self.assertEqual(result["status"], "success")
        grants = self.db.query(DbQueryGrant).filter(DbQueryGrant.ticket_id == ticket["id"]).all()
        self.assertEqual(len(grants), 1)
        grant = grants[0]
        self.assertEqual(grant.username, "dev1")
        self.assertEqual(grant.database, "orders")
        self.assertEqual(grant.status, "active")
        self.assertEqual(grant.granted_by, "运维一号")
        # 时限 12 小时：expires_at 与 created_at 差应在 11~13 小时内（同秒截断容忍）。
        import datetime as _dt
        created = _dt.datetime.strptime(grant.created_at, "%Y-%m-%d %H:%M:%S")
        expires = _dt.datetime.strptime(grant.expires_at, "%Y-%m-%d %H:%M:%S")
        delta_hours = (expires - created).total_seconds() / 3600
        self.assertTrue(11 <= delta_hours <= 13, f"授权时长异常: {delta_hours}h")

    def test_query_ticket_cannot_be_executed(self):
        """查询工单不走执行通道（旧版会按变更执行，属缺陷，现已钉死）。"""
        ticket = self._submit_query_ticket()
        db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.approver)
        with self.assertRaises(HTTPException) as ctx:
            db_api.execute_ticket(ticket["id"], db=self.db, current_user=self.admin_user)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_revoke_grant(self):
        """管理员吊销授权 → status=revoked，重复吊销幂等。"""
        ticket = self._submit_query_ticket()
        db_api.review_ticket(ticket["id"], {"decision": "approve"}, db=self.db, current_user=self.approver)
        grant = self.db.query(DbQueryGrant).filter(DbQueryGrant.ticket_id == ticket["id"]).first()
        result = db_api.revoke_query_grant(grant.id, db=self.db, current_user=self.admin_user)
        self.assertEqual(result["status"], "revoked")
        again = db_api.revoke_query_grant(grant.id, db=self.db, current_user=self.admin_user)
        self.assertEqual(again["status"], "revoked")

    def test_manual_grant_requires_manage_and_valid_user(self):
        """手动授权：用户不存在 → 422；正常路径 granted_by 留痕。"""
        with self.assertRaises(HTTPException) as ctx:
            db_api.create_query_grant(
                {"username": "ghost", "instance_id": self.instance.id, "database": "orders", "hours": 2},
                db=self.db, current_user=self.admin_user,
            )
        self.assertEqual(ctx.exception.status_code, 422)
        result = db_api.create_query_grant(
            {"username": "dev1", "instance_id": self.instance.id, "database": "orders", "hours": 2},
            db=self.db, current_user=self.admin_user,
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["granted_by"], "管理员一号")

    def test_run_query_requires_grant_or_manage(self):
        """自助查询授权校验：无授权 403；有授权放行到执行引擎；管理员豁免。"""
        with self.assertRaises(HTTPException) as ctx:
            db_api.run_query(
                {"instance_id": self.instance.id, "sql_text": "SELECT 1"},
                db=self.db, current_user=self.submitter,
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("查询授权", ctx.exception.detail)

        # 授权后放行（mock 掉真实执行与解密依赖）。
        db_api.create_query_grant(
            {"username": "dev1", "instance_id": self.instance.id, "database": "orders", "hours": 2},
            db=self.db, current_user=self.admin_user,
        )
        fake_result = {
            "ok": True, "columns": ["1"], "rows": [[1]], "results": [{"ok": True}],
            "total_duration_ms": 3,
        }
        with patch.object(db_api, "execute_statements", return_value=fake_result) as mock_exec:
            result = db_api.run_query(
                {"instance_id": self.instance.id, "sql_text": "SELECT 1"},
                db=self.db, current_user=self.submitter,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(mock_exec.call_count, 1)

        # 管理员豁免：无授权也能进到执行引擎。
        with patch.object(db_api, "execute_statements", return_value=fake_result):
            result = db_api.run_query(
                {"instance_id": self.instance.id, "sql_text": "SELECT 1"},
                db=self.db, current_user=self.admin_user,
            )
        self.assertTrue(result["ok"])

    def test_run_query_rejected_for_other_database(self):
        """授权按库精确匹配：授权 orders 库不能查别的库。"""
        db_api.create_query_grant(
            {"username": "dev1", "instance_id": self.instance.id, "database": "orders", "hours": 2},
            db=self.db, current_user=self.admin_user,
        )
        with self.assertRaises(HTTPException) as ctx:
            db_api.run_query(
                {"instance_id": self.instance.id, "sql_text": "SELECT 1", "database": "finance"},
                db=self.db, current_user=self.submitter,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_expired_grant_is_rejected(self):
        """过期授权（即使还没被 scheduler 翻转）也不能查。"""
        grant = DbQueryGrant(
            ticket_id=0, instance_id=self.instance.id, username="dev1", display_name="开发者一号",
            database="orders", tables="", reason="", expires_at="2000-01-01 00:00:00",
            granted_by="管理员一号", status="active", created_at="1999-12-31 23:00:00",
        )
        self.db.add(grant)
        self.db.commit()
        self.assertEqual(db_api._grant_status_now(grant), "expired")
        with self.assertRaises(HTTPException) as ctx:
            db_api.run_query(
                {"instance_id": self.instance.id, "sql_text": "SELECT 1"},
                db=self.db, current_user=self.submitter,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_grant_expiry_flip_is_idempotent(self):
        """scheduler 到期翻转：active→expired，且不碰未到期的。"""
        expired = DbQueryGrant(
            ticket_id=0, instance_id=self.instance.id, username="dev1", display_name="d",
            database="orders", expires_at="2000-01-01 00:00:00",
            granted_by="x", status="active", created_at="1999-12-31 23:00:00",
        )
        alive = DbQueryGrant(
            ticket_id=0, instance_id=self.instance.id, username="dev1", display_name="d",
            database="orders", expires_at="2099-01-01 00:00:00",
            granted_by="x", status="active", created_at="2026-09-21 10:00:00",
        )
        self.db.add_all([expired, alive])
        self.db.commit()
        # _run_due_ticks 内部自建 SessionLocal：patch 成指向测试库的工厂，避免碰真实库。
        with patch("app.db.database.SessionLocal", sessionmaker(bind=self.db.get_bind())):
            db_api._run_due_tickets()
        self.db.expire_all()
        self.assertEqual(
            self.db.query(DbQueryGrant).filter(DbQueryGrant.id == expired.id).first().status, "expired",
        )
        self.assertEqual(
            self.db.query(DbQueryGrant).filter(DbQueryGrant.id == alive.id).first().status, "active",
        )


if __name__ == "__main__":
    unittest.main()
