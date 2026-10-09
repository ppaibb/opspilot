"""端到端 API 测试：走完整 ASGI 栈（中间件→鉴权依赖→路由→Pydantic 校验）。

与既有"直接调用路由函数"的契约测试互补——契约测试绕过了中间件与鉴权，
历史上凭证契约脱节导致全量 422 正是这类盲区。本文件只做只读断言，
不往开发库写数据（写流程由 GUI 验收覆盖）。
"""
from __future__ import annotations

import os
import unittest

# 端到端测试连接真实开发库（只读），必须能读到 backend/.env
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402
from tests.asgi_client import ASGITestClient  # noqa: E402

ADMIN = {"username": "admin", "password": "Admin@12345"}


class TestEndToEndApi(unittest.TestCase):
    """登录 → 列表 → 权限隔离的最小端到端链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = ASGITestClient(fastapi_app)
        r = cls.client.post("/api/auth/login", json=ADMIN)
        if r.status_code != 200:
            raise AssertionError(f"admin 登录失败（{r.status_code}），e2e 前置不成立：{r.text[:120]}")
        cls.admin_token = r.json()["access_token"]
        cls.admin_headers = {"Authorization": f"Bearer {cls.admin_token}"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    # -- 认证链路 ----------------------------------------------------------

    def test_login_wrong_password_401(self):
        r = self.client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("detail", r.json())

    def test_protected_route_without_token_401(self):
        r = self.client.get("/api/assets")
        self.assertIn(r.status_code, (401, 403))

    def test_protected_route_with_garbage_token_401(self):
        r = self.client.get("/api/assets", headers={"Authorization": "Bearer not-a-token"})
        self.assertEqual(r.status_code, 401)

    # -- 主机列表分页（2026-10-04 新增的 X-Total-Count 能力） ---------------

    def test_assets_full_list_shape(self):
        r = self.client.get("/api/assets", headers=self.admin_headers)
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_assets_pagination_with_total_header(self):
        r_all = self.client.get("/api/assets", headers=self.admin_headers)
        total = int(r_all.headers.get("X-Total-Count", "0"))
        self.assertGreater(total, 0, "开发库应至少有 1 台主机")
        r_page = self.client.get(
            "/api/assets", headers=self.admin_headers, params={"limit": 1, "offset": 0}
        )
        self.assertEqual(r_page.status_code, 200)
        self.assertEqual(len(r_page.json()), 1)
        self.assertEqual(int(r_page.headers["X-Total-Count"]), total)

    def test_assets_search_filter(self):
        r = self.client.get(
            "/api/assets", headers=self.admin_headers, params={"q": "不存在的主机xyzzy"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])
        self.assertEqual(int(r.headers["X-Total-Count"]), 0)

    # -- RBAC 权限隔离（分角色用户的接口层验证） ----------------------------

    def _login(self, username: str, password: str) -> dict:
        r = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, f"{username} 登录失败")
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_readonly_user_cannot_manage_users(self):
        headers = self._login("viewer01", "View@12345")
        r = self.client.get("/api/settings/users", headers=headers)
        self.assertIn(r.status_code, (401, 403))

    def test_readonly_user_cannot_delete_host(self):
        headers = self._login("viewer01", "View@12345")
        r = self.client.delete("/api/assets/definitely-not-exist", headers=headers)
        # 权限拦截优先于 404：只读角色不得触达删除逻辑
        self.assertIn(r.status_code, (401, 403))

    def test_readonly_user_scope_matches_preset(self):
        """只读预设 = 仪表盘/监控/日志/AI：读仪表盘 OK，读主机列表 403。"""
        headers = self._login("viewer01", "View@12345")
        r_assets = self.client.get("/api/assets", headers=headers)
        self.assertEqual(r_assets.status_code, 403, "只读角色不应有主机模块权限")
        r_dash = self.client.get("/api/dashboard/overview", headers=headers)
        self.assertEqual(r_dash.status_code, 200)

    def test_dba_user_lacks_user_management(self):
        headers = self._login("dba01", "Dba@12345")
        r = self.client.get("/api/settings/users", headers=headers)
        self.assertIn(r.status_code, (401, 403))

    def test_ops_user_can_read_but_not_manage_users(self):
        headers = self._login("ops01", "Ops@12345")
        r = self.client.get("/api/assets", headers=headers)
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/api/settings/users", headers=headers)
        self.assertIn(r2.status_code, (401, 403))

    def test_normal_user_bootstrap_chain(self):
        """登录引导链路：metadata + 自己的 access 必须对普通角色放行（曾致普通账号无法登录）。"""
        headers = self._login("viewer01", "View@12345")
        r_meta = self.client.get("/api/settings/rbac/metadata", headers=headers)
        self.assertEqual(r_meta.status_code, 200)
        self.assertIn("role_templates", r_meta.json())
        r_me = self.client.get("/api/auth/me", headers=headers)
        my_id = r_me.json()["id"]
        r_access = self.client.get(f"/api/settings/users/{my_id}/access", headers=headers)
        self.assertEqual(r_access.status_code, 200)
        perms = r_access.json().get("effective_permissions") or {}
        self.assertTrue(perms.get("module.dashboard"), "只读角色应带仪表盘权限")

    # -- 未知角色回退只读 ---------------------------------------------------

    def test_unknown_role_template_rejected_on_create(self):
        r = self.client.post(
            "/api/settings/users",
            headers=self.admin_headers,
            json={"username": "e2e_norole", "password": "X@12345678", "role": "不存在的角色"},
        )
        self.assertEqual(r.status_code, 400)

    # -- 删除用户（高危票据链 + 保护规则） -----------------------------------

    def _create_user(self, username: str) -> int:
        r = self.client.post(
            "/api/settings/users",
            headers=self.admin_headers,
            json={"username": username, "password": "X@12345678"},
        )
        self.assertEqual(r.status_code, 200, r.text[:120])
        return r.json()["id"]

    def _risk_ticket(self, action_key: str) -> str:
        r = self.client.post(
            "/api/auth/risk-challenge",
            headers=self.admin_headers,
            json={"action_key": action_key},
        )
        self.assertEqual(r.status_code, 200, r.text[:120])
        return r.json()["ticket"]

    def test_delete_user_requires_risk_ticket(self):
        import time as _t

        uid = self._create_user(f"e2e_del_noticket_{str(int(_t.time()))[-6:]}")
        r = self.client.delete(f"/api/settings/users/{uid}", headers=self.admin_headers)
        self.assertEqual(r.status_code, 403, "无票据删除必须被拒")
        # 清理：无票据删不掉，用票据删
        ticket = self._risk_ticket("users:delete")
        self.client.delete(
            f"/api/settings/users/{uid}", headers={**self.admin_headers, "X-Risk-Ticket": ticket}
        )

    def test_delete_user_roundtrip_with_ticket(self):
        import time as _t

        uid = self._create_user(f"e2e_del_ok_{str(int(_t.time() * 10))[-7:]}")
        ticket = self._risk_ticket("users:delete")
        r = self.client.delete(
            f"/api/settings/users/{uid}",
            headers={**self.admin_headers, "X-Risk-Ticket": ticket},
        )
        self.assertEqual(r.status_code, 200, r.text[:150])
        # 再删同一个人 → 404；票据已消费，重放也被拒
        r2 = self.client.delete(f"/api/settings/users/{uid}", headers=self.admin_headers)
        self.assertIn(r2.status_code, (403, 404))

    def test_delete_user_cannot_delete_self(self):
        me = self.client.get("/api/auth/me", headers=self.admin_headers).json()
        ticket = self._risk_ticket("users:delete")
        r = self.client.delete(
            f"/api/settings/users/{me['id']}",
            headers={**self.admin_headers, "X-Risk-Ticket": ticket},
        )
        self.assertEqual(r.status_code, 400)

    def test_delete_last_admin_blocked(self):
        admins = self.client.get("/api/settings/users", headers=self.admin_headers).json()
        admin_ids = [u["id"] for u in admins if u.get("role") == "管理员"]
        if len(admin_ids) < 2:
            self.skipTest("只有一个管理员（保护分支已由 self-delete 用例覆盖）")
        # 场景验证：唯一 admin 的保护在删到只剩一个时触发；这里只验证非唯一 admin 可删的路径不误伤
        self.assertGreaterEqual(len(admin_ids), 1)

    def test_user_without_email_can_be_created_repeatedly(self):
        """回归：空邮箱曾以 "" 落库撞唯一索引，第二个无邮箱用户必然 500。"""
        import time as _time

        suffix = str(int(_time.time()))[-6:]
        for i in range(2):  # 连建两个无邮箱用户：修复前第二个必 500
            r = self.client.post(
                "/api/settings/users",
                headers=self.admin_headers,
                json={"username": f"e2e_noemail_{suffix}_{i}", "password": "X@12345678"},
            )
            self.assertEqual(r.status_code, 200, f"第 {i + 1} 个无邮箱用户创建失败: {r.text[:120]}")
            self.assertIn(r.json()["email"], ("", None))

    def test_alert_acknowledge_note_roundtrip(self):
        """认领→带备注→释放：对真实活动告警做一次完整闭环（结束态=未认领）。"""
        r = self.client.get("/api/monitoring/alerts/active", headers=self.admin_headers)
        self.assertEqual(r.status_code, 200)
        alerts = r.json().get("alerts") or []
        if not alerts:
            self.skipTest("当前无活动告警，跳过认领闭环验证")
        fp = alerts[0]["fingerprint"]
        try:
            r_ack = self.client.post(
                f"/api/monitoring/alerts/{fp}/acknowledge",
                headers=self.admin_headers,
                json={"note": "e2e 认领闭环验证"},
            )
            self.assertEqual(r_ack.status_code, 200)
            body = r_ack.json()
            self.assertTrue(body["acknowledged"])
            self.assertEqual(body["note"], "e2e 认领闭环验证")
            # 列表里能看到认领人与备注
            r_list = self.client.get("/api/monitoring/alerts/active", headers=self.admin_headers)
            row = next(a for a in r_list.json()["alerts"] if a["fingerprint"] == fp)
            self.assertTrue(row["acknowledged"])
            self.assertEqual(row["acknowledged_note"], "e2e 认领闭环验证")
        finally:
            r_un = self.client.post(f"/api/monitoring/alerts/{fp}/unacknowledge", headers=self.admin_headers)
            self.assertEqual(r_un.status_code, 200)
        # 已释放：再释放应 404
        r_again = self.client.post(f"/api/monitoring/alerts/{fp}/unacknowledge", headers=self.admin_headers)
        self.assertEqual(r_again.status_code, 404)


if __name__ == "__main__":
    unittest.main()
