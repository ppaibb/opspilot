"""风险等级重评（2026-11-10）契约测试。

重评规则：high 只留效果不可逆的操作（硬删除、吊销及同类不可逆破坏）；
读、下载、导出、连接、执行、部署、重启、关机等可恢复/可重做的操作一律
medium 或 low；同类操作拉齐。

本文件钉死重评后的整张表，并验证降级项的关键安全属性没有跟着丢：
1. 整表 46 条逐条断言新等级（get_risk_level）——任务所写“44 条”是
   笔误，任务贴出的全表逐条数出 46 条，改动前后字典实测均为 46 条；
2. 原 high 的 SSH 连接与证书下载现在是 medium，但仍需一次性票据——
   无票据调守卫应被拒、有票据放行（require_risk_ack 本身不动）；
3. 留 high 的不可逆操作（吊销、硬删除、DB/变更执行）仍是 high；
4. 降级后的 GET 下载类路由在审计规则表里仍解析为 medium（非 low），
   即审计中间件继续记录它们，不会因降级悄悄掉出审计。
"""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.rbac import get_risk_level
from app.db.database import Base
from app.main import resolve_request_risk_level
from app.models.risk_ticket import RiskAckTicket
from app.models.system_config import SystemConfig  # noqa: F401 与其他套件一致的建表集合
from app.models.user import User

from app.api import auth
from app.core import security

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(
    bind=engine,
    tables=[
        SystemConfig.__table__,
        User.__table__,
        RiskAckTicket.__table__,
    ],
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

ADMIN = User(id=1, username="admin", role="管理员", status="active")

# (权限键, 动作, 重评后等级)。逐条依据见 rbac.py 注释与重评报告。
RERATED_TABLE: list[tuple[str, str, str]] = [
    ("action.assets.credentials.export", "export", "medium"),
    ("module.wireguard.manage", "download_config", "medium"),
    ("action.db.execute", "download_rollback", "medium"),
    ("action.assets.hosts.connect", "download_file", "medium"),
    ("action.assets.certificates.manage", "revoke", "high"),
    ("action.assets.certificates.download", "download", "medium"),
    ("action.assets.certificates.manage", "deploy", "low"),
    ("action.assets.certificates.manage", "delete", "high"),
    ("action.assets.hosts.manage", "batch_delete", "high"),
    ("action.assets.hosts.connect", "exec", "medium"),
    ("action.assets.hosts.connect", "exec_session", "medium"),
    ("action.assets.hosts.monitoring", "uninstall_collector", "medium"),
    ("action.assets.vms.power", "poweroff", "medium"),
    ("action.assets.vms.power", "restart", "medium"),
    ("action.assets.vms.manage", "delete", "high"),
    ("action.assets.clusters.manage", "apply", "medium"),
    ("action.assets.clusters.manage", "edit", "medium"),
    ("action.assets.clusters.manage", "restart", "medium"),
    ("action.assets.clusters.manage", "pause", "medium"),
    ("action.assets.clusters.manage", "delete", "high"),
    ("action.assets.clusters.manage", "cordon", "medium"),
    ("action.assets.clusters.manage", "uncordon", "medium"),
    ("action.assets.clusters.manage", "drain", "medium"),
    ("action.assets.clusters.manage", "set-image", "medium"),
    ("action.assets.clusters.manage", "rollout-undo", "medium"),
    ("action.assets.clusters.manage", "suspend_cronjob", "medium"),
    ("action.assets.clusters.manage", "trigger_cronjob", "medium"),
    ("action.assets.clusters.manage", "patch_taints", "medium"),
    ("action.assets.clusters.manage", "resize_pvc", "medium"),
    ("action.assets.clusters.manage", "patch_labels", "medium"),
    ("action.assets.clusters.manage", "scale", "medium"),
    ("action.assets.clusters.manage", "create_namespace", "medium"),
    ("action.assets.clusters.manage", "exec_session", "medium"),
    ("action.assets.domains.manage", "delete", "high"),
    ("action.tools.scripts.execute", "execute", "medium"),
    ("action.change.execute", "execute", "high"),
    ("action.db.execute", "execute", "high"),
    ("action.apps.manage", "batch_delete_components", "high"),
    ("action.apps.control", "control_component", "medium"),
    ("module.dashboard", "clear_notifications", "medium"),
    ("action.settings.users.manage", "disable", "medium"),
    ("action.settings.users.manage", "delete", "high"),
    ("action.settings.system.manage", "delete", "medium"),
    ("action.monitoring.channels.manage", "delete", "medium"),
    ("action.finance.accounts.manage", "delete", "high"),
    ("action.finance.accounts.manage", "create", "medium"),
]


def challenge(body: dict, db, user=ADMIN):
    return asyncio.run(auth.risk_challenge(auth.RiskChallengeRequest(**body), current_user=user, db=db))


class ReratedRiskLevelsContract(unittest.TestCase):
    """整表逐条断言：新旧对照以本表为准。"""

    def test_full_table_matches_rerated_levels(self):
        self.assertEqual(len(RERATED_TABLE), 46)
        for perm, action, expected in RERATED_TABLE:
            with self.subTest(perm=perm, action=action):
                self.assertEqual(get_risk_level(perm, action), expected)

    def test_high_is_only_irreversible_operations(self):
        highs = {(p, a) for p, a, level in RERATED_TABLE if level == "high"}
        self.assertEqual(
            highs,
            {
                ("action.assets.certificates.manage", "revoke"),
                ("action.assets.certificates.manage", "delete"),
                ("action.assets.hosts.manage", "batch_delete"),
                ("action.assets.vms.manage", "delete"),
                ("action.assets.clusters.manage", "delete"),
                ("action.assets.domains.manage", "delete"),
                ("action.change.execute", "execute"),
                ("action.db.execute", "execute"),
                ("action.apps.manage", "batch_delete_components"),
                ("action.settings.users.manage", "delete"),
                ("action.finance.accounts.manage", "delete"),
            },
        )


class DowngradedActionsStillNeedTicketContract(unittest.TestCase):
    """降级只去掉高危确认框，不去掉票据：无票 403、有票放行。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(RiskAckTicket).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _assert_ticket_still_enforced(self, body: dict, perm: str, action: str, expected_level: str):
        result = challenge(body, self.db)
        self.assertEqual(result["risk_level"], expected_level)
        ticket = result["ticket"]
        guard = security.require_risk_ack(perm, action)
        no_ticket = SimpleNamespace(headers={})
        with self.assertRaises(HTTPException) as ctx:
            guard(request=no_ticket, current_user=ADMIN, db=TestingSession())
        self.assertEqual(ctx.exception.status_code, 403)
        with_ticket = SimpleNamespace(headers={"x-risk-ticket": ticket})
        user = guard(request=with_ticket, current_user=ADMIN, db=TestingSession())
        self.assertEqual(user.id, ADMIN.id)

    def test_ssh_exec_session_is_medium_but_still_needs_ticket(self):
        self._assert_ticket_still_enforced(
            {"action_key": "hosts:exec_session"},
            "action.assets.hosts.connect",
            "exec_session",
            "medium",
        )

    def test_ssh_exec_is_medium_but_still_needs_ticket(self):
        self._assert_ticket_still_enforced(
            {"action_key": "hosts:exec"},
            "action.assets.hosts.connect",
            "exec",
            "medium",
        )

    def test_certificate_download_is_medium_but_still_needs_ticket(self):
        self._assert_ticket_still_enforced(
            {"action_key": "certificates:download"},
            "action.assets.certificates.download",
            "download",
            "medium",
        )

    def test_credential_export_is_medium_but_still_needs_ticket(self):
        self._assert_ticket_still_enforced(
            {"action_key": "credentials:export"},
            "action.assets.credentials.export",
            "export",
            "medium",
        )

    def test_revoke_and_hard_deletes_stay_high(self):
        cases = [
            ({"action_key": "certificates:revoke"}, "high"),
            ({"action_key": "clusters:delete"}, "high"),
            ({"permission_key": "action.assets.certificates.manage", "action_id": "delete"}, "high"),
            ({"permission_key": "action.assets.hosts.manage", "action_id": "batch_delete"}, "high"),
            ({"permission_key": "action.assets.vms.manage", "action_id": "delete"}, "high"),
            ({"permission_key": "action.assets.domains.manage", "action_id": "delete"}, "high"),
            ({"permission_key": "action.settings.users.manage", "action_id": "delete"}, "high"),
            ({"permission_key": "action.finance.accounts.manage", "action_id": "delete"}, "high"),
            ({"permission_key": "action.db.execute", "action_id": "execute"}, "high"),
            ({"permission_key": "action.change.execute", "action_id": "execute"}, "high"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(challenge(body, self.db)["risk_level"], expected)


class DowngradedGetRoutesStillAuditedContract(unittest.TestCase):
    """降级后的 GET 下载类路由仍解析为 medium：审计中间件继续记录。

    中间件只在 method 为写操作或 risk_level != low 时审计；这些 GET 路由
    全靠 AUDIT_RISK_ROUTE_RULES 命中，降到 medium 仍 != low，审计不丢；
    若某天被误降到 low，本测试会先红。
    """

    def test_downgraded_get_routes_resolve_medium_not_low(self):
        cases = [
            ("GET", "/api/credentials/CRED-1/export"),
            ("GET", "/api/wireguard/networks/1/server-config"),
            ("GET", "/api/db/tickets/3/rollback"),
            ("GET", "/api/certificates/CERT-1/download"),
            ("GET", "/api/assets/terminal/files/host-1/download"),
        ]
        for method, path in cases:
            with self.subTest(path=path):
                self.assertEqual(resolve_request_risk_level(method, path), "medium")

    def test_downgraded_cluster_ops_resolve_medium_in_audit_rules(self):
        cases = [
            ("POST", "/api/clusters/c1/nodes/n1/cordon"),
            ("POST", "/api/clusters/c1/nodes/n1/drain"),
            ("PATCH", "/api/clusters/c1/workloads/Deployment/ns/name/set-image"),
            ("POST", "/api/clusters/c1/workloads/Deployment/ns/name/rollout-undo"),
        ]
        for method, path in cases:
            with self.subTest(path=path):
                self.assertEqual(resolve_request_risk_level(method, path), "medium")


if __name__ == "__main__":
    unittest.main()
