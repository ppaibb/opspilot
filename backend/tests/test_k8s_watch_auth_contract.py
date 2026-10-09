"""K8s Watch SSE 鉴权契约测试。

阶段1安全边界：
1. watch session 签发入口必须登录（未登录 401）且要求集群模块权限（403）；
2. session token 与签发用户/kinds/namespace 绑定，消费端复核订阅范围；
3. token 一次性（重放 401）、90 秒 TTL；
4. 正常签发 → 消费 → 订阅链路可用（mock InformerManager，不真连集群）。

不使用 TestClient：直接调用路由函数与依赖闭包（参考 test_finance_api_e2e.py）。
"""
import asyncio
import os
import time
import unittest
import unittest.mock
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException

from app.api import k8s_watch
from app.core.security import get_current_user, require_permissions

CLUSTER = "cls-1"
ADMIN = SimpleNamespace(id=1, username="admin", role="管理员", permission_overrides="")
READONLY = SimpleNamespace(id=2, username="viewer", role="只读", permission_overrides="")


def create_session(cluster_id: str = CLUSTER, kinds: str = "", namespace: str = "", user=ADMIN):
    return k8s_watch.create_watch_session(
        cluster_id, kinds=kinds, namespace=namespace, current_user=user
    )


class WatchSessionIssueContract(unittest.TestCase):
    """签发入口的登录与权限边界。"""

    def test_unauthenticated_request_is_401(self):
        """未登录（无法解析 Bearer Token）必须在 get_current_user 处 401。"""
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token="not-a-jwt", db=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_issue_route_requires_cluster_permission_guard(self):
        """签发路由必须挂 require_permissions 的权限守卫（非仅登录）。"""
        from app.main import app

        guard_names = set()
        for route in app.routes:
            if getattr(route, "path", "") != "/api/clusters/{cluster_id}/watch/session":
                continue
            for dependency in getattr(route.dependant, "dependencies", []):
                guard_names.add(getattr(dependency.call, "__name__", ""))
        self.assertIn("_permission_guard", guard_names)

    def test_user_without_cluster_module_permission_is_403(self):
        guard = require_permissions("module.assets.clusters")
        with self.assertRaises(HTTPException) as ctx:
            guard(current_user=READONLY)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_session_records_user_kinds_namespace(self):
        result = create_session(kinds="Pod,Deployment", namespace="prod")
        session = k8s_watch._watch_sessions[result["token"]]
        self.assertEqual(session["user_id"], ADMIN.id)
        self.assertEqual(session["kinds"], ["Pod", "Deployment"])
        self.assertEqual(session["namespace"], "prod")

    def test_empty_kinds_and_namespace_mean_unlimited(self):
        result = create_session()
        session = k8s_watch._watch_sessions[result["token"]]
        self.assertEqual(session["kinds"], [])
        self.assertEqual(session["namespace"], "")


class WatchSessionConsumeContract(unittest.TestCase):
    """消费端：一次性、TTL、范围复核。"""

    def setUp(self):
        k8s_watch._watch_sessions.clear()

    def tearDown(self):
        k8s_watch._watch_sessions.clear()

    def test_valid_session_consumes_ok(self):
        token = create_session(kinds="Pod", namespace="prod")["token"]
        self.assertEqual(k8s_watch._consume_watch_session(token, CLUSTER, "Pod", "prod"), "ok")

    def test_replayed_token_is_missing(self):
        token = create_session()["token"]
        self.assertEqual(k8s_watch._consume_watch_session(token, CLUSTER, "Pod", ""), "ok")
        # 第二次使用同一个 token：已被原子弹出。
        self.assertEqual(k8s_watch._consume_watch_session(token, CLUSTER, "Pod", ""), "missing")

    def test_unknown_token_is_missing(self):
        self.assertEqual(k8s_watch._consume_watch_session("nope", CLUSTER, "Pod", ""), "missing")

    def test_expired_session_is_expired(self):
        token = create_session()["token"]
        k8s_watch._watch_sessions[token]["created_at"] = time.time() - (k8s_watch._WATCH_SESSION_TTL + 1)
        self.assertEqual(k8s_watch._consume_watch_session(token, CLUSTER, "Pod", ""), "expired")

    def test_cluster_mismatch_is_rejected(self):
        token = create_session()["token"]
        self.assertEqual(
            k8s_watch._consume_watch_session(token, "cls-other", "Pod", ""), "cluster_mismatch"
        )

    def test_kinds_beyond_issued_scope_is_rejected(self):
        token = create_session(kinds="Pod")["token"]
        self.assertEqual(
            k8s_watch._consume_watch_session(token, CLUSTER, "Pod,Deployment", ""),
            "kinds_out_of_scope",
        )

    def test_issued_empty_kinds_allow_any(self):
        token = create_session(kinds="")["token"]
        self.assertEqual(
            k8s_watch._consume_watch_session(token, CLUSTER, "Pod,Deployment,Service", ""), "ok"
        )

    def test_namespace_mismatch_is_rejected(self):
        token = create_session(namespace="prod")["token"]
        self.assertEqual(
            k8s_watch._consume_watch_session(token, CLUSTER, "Pod", "staging"),
            "namespace_mismatch",
        )

    def test_issued_empty_namespace_allows_any(self):
        token = create_session(namespace="")["token"]
        self.assertEqual(k8s_watch._consume_watch_session(token, CLUSTER, "Pod", "whatever"), "ok")


class WatchEndpointContract(unittest.TestCase):
    """GET /watch 正常链路：签发 → 消费 → 订阅（mock InformerManager）。"""

    def setUp(self):
        k8s_watch._watch_sessions.clear()

    def tearDown(self):
        k8s_watch._watch_sessions.clear()

    def _fake_manager(self):
        class _FakeManager:
            def __init__(self):
                self.subscribe_calls = []
                self.unsubscribed = []

            async def subscribe(self, cluster_id, kinds, namespace):
                self.subscribe_calls.append((cluster_id, set(kinds), namespace))
                return "sub-1", asyncio.Queue(), {"items": []}

            async def unsubscribe(self, cluster_id, subscriber_id):
                self.unsubscribed.append((cluster_id, subscriber_id))

        return _FakeManager()

    def test_watch_stream_issued_and_consumed(self):
        fake = self._fake_manager()
        token = create_session(kinds="Pod,Deployment", namespace="prod")["token"]
        with unittest.mock.patch.object(
            k8s_watch, "InformerManager", SimpleNamespace(get_instance=lambda: fake)
        ):
            response = asyncio.run(
                k8s_watch.watch_resources(CLUSTER, token=token, kinds="Deployment", namespace="prod")
            )
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(fake.subscribe_calls, [(CLUSTER, {"Deployment"}, "prod")])

    def test_watch_with_bad_token_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(k8s_watch.watch_resources(CLUSTER, token="bad", kinds="Pod", namespace=""))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_watch_beyond_scope_is_403(self):
        token = create_session(kinds="Pod")["token"]
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                k8s_watch.watch_resources(CLUSTER, token=token, kinds="Pod,Deployment", namespace="")
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_watch_get_route_has_no_http_bearer_dependency(self):
        """SSE 用 EventSource，不能带 header：GET 路由不得挂 HTTP Bearer 依赖。"""
        from app.main import app

        for route in app.routes:
            if getattr(route, "path", "") != "/api/clusters/{cluster_id}/watch":
                continue
            dependency_names = {
                getattr(dependency.call, "__name__", "")
                for dependency in getattr(route.dependant, "dependencies", [])
            }
            self.assertNotIn("get_current_user", dependency_names)
            self.assertNotIn("_permission_guard", dependency_names)
            return
        self.fail("watch route not found")


if __name__ == "__main__":
    unittest.main()
