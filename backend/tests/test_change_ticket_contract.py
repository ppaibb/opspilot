"""通用变更工单契约：创建 → 多级审批 → 执行/取消。"""

from __future__ import annotations

import unittest
from datetime import datetime

from app.api import changes
from app.models.change_ticket import ChangeTicket
from app.models.user import User


class FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def count(self):
        return len(self._rows)

    def order_by(self, *args, **kwargs):
        return self

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class FakeDB:
    def __init__(self, mapping):
        self._mapping = mapping
        self.committed = 0

    def query(self, *entities):
        name = getattr(entities[0], "__name__", str(entities[0]))
        return FakeQuery(self._mapping.get(name, []))

    def add(self, obj):
        self._mapping.setdefault(type(obj).__name__, []).append(obj)

    def delete(self, obj):
        for bucket in self._mapping.values():
            if obj in bucket:
                bucket.remove(obj)
                return

    def commit(self):
        self.committed += 1

    def refresh(self, obj):
        return obj


def _user(role="admin", name="张三", username="zhangsan"):
    return User(id=1, username=username, role=role, name=name)


class ChangeTicketFlowTest(unittest.TestCase):
    def test_create_without_approval_chain_is_approved(self):
        db = FakeDB({"ChangeTicket": [], "User": []})
        payload = changes.ChangeTicketCreate(
            title="删除下线主机",
            change_type="host.delete",
            resource_type="host",
            resource_id="i-1",
            resource_name="web-1",
            impact="该机无业务流量",
            rollback_plan="从备份恢复",
            approval_steps=[],
        )
        result = changes.create_ticket(payload, db=db, current_user=_user())
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["ticket_no"].startswith("CHG-"))
        self.assertEqual(result["change_type_label"], "删除主机")

    def test_create_with_chain_stays_pending(self):
        db = FakeDB({"ChangeTicket": [], "User": []})
        payload = changes.ChangeTicketCreate(
            title="证书删除",
            change_type="certificate.delete",
            resource_type="certificate",
            resource_id="cert-1",
            approval_steps=["组长", "经理"],
        )
        result = changes.create_ticket(payload, db=db, current_user=_user())
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["approval_steps"], ["组长", "经理"])
        self.assertEqual(result["current_step"], 1)

    def test_two_step_approve_promotes_then_approves(self):
        row = ChangeTicket(
            id=1,
            ticket_no="CHG-20260923-0001",
            title="删机",
            change_type="host.delete",
            resource_id="i-1",
            status="pending",
            approval_steps=["组长", "经理"],
            current_step=1,
            approval_log=[],
            created_at=datetime.utcnow(),
        )
        db = FakeDB({"ChangeTicket": [row], "User": [], "Asset": []})

        r1 = changes.approve_ticket(1, changes.ReviewBody(note="ok"), db=db, current_user=_user(role="admin", name="组长"))
        self.assertEqual(r1["status"], "pending")
        self.assertEqual(r1["current_step"], 2)

        r2 = changes.approve_ticket(1, changes.ReviewBody(note="go"), db=db, current_user=_user(role="admin", name="经理"))
        self.assertEqual(r2["status"], "approved")
        self.assertEqual(r2["reviewer"], "经理")

    def test_reject_closes_ticket(self):
        row = ChangeTicket(
            id=2,
            ticket_no="CHG-20260923-0002",
            title="删机",
            change_type="host.delete",
            status="pending",
            approval_steps=["组长"],
            current_step=1,
            approval_log=[],
            created_at=datetime.utcnow(),
        )
        db = FakeDB({"ChangeTicket": [row], "User": []})
        r = changes.reject_ticket(2, changes.ReviewBody(note="风险高"), db=db, current_user=_user())
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["approval_log"][-1]["decision"], "reject")

    def test_execute_host_delete_runs_executor(self):
        row = ChangeTicket(
            id=3,
            ticket_no="CHG-20260923-0003",
            title="删机",
            change_type="host.delete",
            resource_id="i-9",
            status="approved",
            approval_steps=[],
            current_step=1,
            approval_log=[],
            created_at=datetime.utcnow(),
        )

        class AssetRow:
            instance_id = "i-9"
            name = "web-9"

        db = FakeDB({"ChangeTicket": [row], "User": [], "Asset": [AssetRow()]})
        r = changes.execute_ticket(
            3,
            changes.ExecuteBody(),
            db=db,
            current_user=_user(),
            _risk=None,
        )
        self.assertEqual(r["status"], "success")
        self.assertTrue(r["exec_result"]["ok"])
        self.assertEqual(len(db._mapping["Asset"]), 0)

    def test_execute_custom_without_manual_flag_fails(self):
        row = ChangeTicket(
            id=4,
            ticket_no="CHG-20260923-0004",
            title="自定义",
            change_type="custom",
            status="approved",
            approval_steps=[],
            current_step=1,
            approval_log=[],
            created_at=datetime.utcnow(),
        )
        db = FakeDB({"ChangeTicket": [row], "User": []})
        r = changes.execute_ticket(4, changes.ExecuteBody(), db=db, current_user=_user(), _risk=None)
        self.assertEqual(r["status"], "failed")
        self.assertIn("人工执行", r["exec_result"]["message"])


if __name__ == "__main__":
    unittest.main()
