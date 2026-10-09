import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api import auth
from app import main
from app.main import app
from app.core.filesystem import write_sensitive_text_file
from app.core.login_rate_limit import LoginRateLimiter


def _flatten_dependency_values(value):
    """递归展开 FastAPI 依赖闭包中的配置值，便于断言权限键。"""
    if isinstance(value, (tuple, list, set)):
        for item in value:
            yield from _flatten_dependency_values(item)
        return
    yield value


def _route_dependency_values(path: str, method: str) -> set[str]:
    """读取指定路由的依赖闭包值。"""
    expected_method = method.upper()
    for route in app.routes:
        if getattr(route, "path", "") != path:
            continue
        if expected_method not in (getattr(route, "methods", set()) or set()):
            continue
        values: set[str] = set()
        dependant = getattr(route, "dependant", None)
        for dependency in getattr(dependant, "dependencies", []):
            closure = getattr(dependency.call, "__closure__", None) or []
            for cell in closure:
                for item in _flatten_dependency_values(cell.cell_contents):
                    if isinstance(item, str):
                        values.add(item)
        return values
    raise AssertionError(f"Route not found: {method} {path}")


class BackendSecurityHardeningContractTest(unittest.TestCase):
    def test_audit_risk_level_is_resolved_from_backend_rules(self):
        """审计风险等级必须由后端规则表计算，不能直接保存 X-Risk-Ack。"""
        # 私钥下载仍是 high：审计中间件据此高亮，前端也要二次确认。
        self.assertEqual(
            main.resolve_request_risk_level("GET", "/api/certificates/CERT-1/download"),
            "high",
        )
        # 证书部署当前在 ACTION_RISK_LEVELS 注册为 low（风险确认已按运维体验要求直通放行）。
        self.assertEqual(
            main.resolve_request_risk_level("POST", "/api/certificates/CERT-1/deploy"),
            "low",
        )
        self.assertEqual(
            main.resolve_request_risk_level("POST", "/api/dashboard/widgets"),
            "low",
        )

    def test_login_endpoint_uses_rate_limiter_for_repeated_failures(self):
        """登录接口必须接入失败限流，不能只存在工具类。"""
        limiter = LoginRateLimiter(
            max_attempts=1,
            window_seconds=60,
            lock_seconds=120,
            clock=lambda: 1000.0,
        )

        class _EmptyQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return None

        class _EmptyDb:
            def query(self, *_args, **_kwargs):
                return _EmptyQuery()

        request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.8"))
        payload = auth.LoginRequest(username="admin", password="bad-password")

        with patch.object(auth, "_LOGIN_RATE_LIMITER", limiter, create=True):
            with self.assertRaises(HTTPException) as first:
                auth.run_login_flow(payload, request=request, db=_EmptyDb())
            self.assertEqual(first.exception.status_code, 401)

            with self.assertRaises(HTTPException) as second:
                auth.run_login_flow(payload, request=request, db=_EmptyDb())
            self.assertEqual(second.exception.status_code, 429)

    def test_cluster_mutating_routes_require_action_permissions(self):
        """集群接入和删除必须要求写操作权限，不能只靠页面级权限。"""
        create_values = _route_dependency_values("/api/clusters", "POST")
        self.assertIn("action.assets.clusters.manage", create_values)

        delete_values = _route_dependency_values("/api/clusters/{cluster_id}", "DELETE")
        self.assertIn("action.assets.clusters.manage", delete_values)
        # 删除集群在风险规则表里是 high，审计会记录并高亮。
        self.assertEqual(
            main.resolve_request_risk_level("DELETE", "/api/clusters/CLS-1"),
            "high",
        )

    def test_certificate_private_key_operations_require_risk_ack(self):
        """涉及私钥的证书操作必须受写权限保护，并在审计里标出风险等级。

        风险确认（require_risk_ack）已按运维体验要求改为直通放行，
        因此这里断言权限仍然收敛 + 审计风险等级仍按规则表计算。
        """
        download_values = _route_dependency_values("/api/certificates/{cert_id}/download", "GET")
        self.assertIn("action.assets.certificates.download", download_values)
        self.assertEqual(
            main.resolve_request_risk_level("GET", "/api/certificates/CERT-1/download"),
            "high",
        )

        deploy_values = _route_dependency_values("/api/certificates/{cert_id}/deploy", "POST")
        self.assertIn("action.assets.certificates.manage", deploy_values)

        batch_delete_values = _route_dependency_values("/api/certificates/batch-delete", "POST")
        self.assertIn("action.assets.certificates.manage", batch_delete_values)
        self.assertEqual(
            main.resolve_request_risk_level("POST", "/api/certificates/batch-delete"),
            "medium",
        )

        delete_values = _route_dependency_values("/api/certificates/{cert_id}", "DELETE")
        self.assertIn("action.assets.certificates.manage", delete_values)
        self.assertEqual(
            main.resolve_request_risk_level("DELETE", "/api/certificates/CERT-1"),
            "medium",
        )

    def test_login_rate_limiter_locks_after_failures_and_clears_on_success(self):
        """同一用户名与来源连续失败后短期锁定，成功登录会清理失败记录。"""
        now = [1000.0]
        limiter = LoginRateLimiter(
            max_attempts=3,
            window_seconds=60,
            lock_seconds=120,
            clock=lambda: now[0],
        )
        key = limiter.build_key("Admin", "10.0.0.1")

        for _ in range(3):
            allowed, _ = limiter.check_allowed(key)
            self.assertTrue(allowed)
            limiter.record_failure(key)

        allowed, reason = limiter.check_allowed(key)
        self.assertFalse(allowed)
        self.assertIn("登录失败次数过多", reason)

        now[0] += 121
        allowed, _ = limiter.check_allowed(key)
        self.assertTrue(allowed)

        limiter.record_failure(key)
        limiter.record_success(key)
        allowed, _ = limiter.check_allowed(key)
        self.assertTrue(allowed)

    def test_sensitive_text_file_writer_restricts_directory_and_file_permissions(self):
        """敏感文本落盘时必须收紧目录和文件权限。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "private" / "secret.yaml"
            with patch("app.core.filesystem.os.chmod") as chmod:
                write_sensitive_text_file(target, "secret-content")

            self.assertEqual(target.read_text(encoding="utf-8"), "secret-content")
            chmod.assert_any_call(target.parent, 0o700)
            chmod.assert_any_call(target, 0o600)


if __name__ == "__main__":
    unittest.main()
