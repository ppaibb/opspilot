"""logs/fields 契约测试：VLogs field_names/field_values 上游参数。

背景（235 部署演练发现）：VLogs v1.50 的 /select/logsql/field_names 强制要求
`query` 参数，缺失直接 400，后端把它翻译成 502——日志页「标签」下拉整体不可用。
这里钉死枚举字段时必须携带 query（match-all），防止回归。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api import grafana_sso  # noqa: F401  # 确保路由模块已加载
from app.api.monitoring import routes as monitoring_routes
from app.core.security import get_current_user
from app.db.database import get_db
from app.main import app as fastapi_app
from app.models.user import User
from tests.asgi_client import ASGITestClient


class _FakeUpstream:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class LogsFieldsContractTest(unittest.TestCase):
    """POST /api/monitoring/logs/fields 的上游参数契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = ASGITestClient(fastapi_app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        user = User(
            id=1,
            username="admin",
            name="管理员",
            email="admin@opspilot.local",
            role="管理员",
            status="active",
        )
        fastapi_app.dependency_overrides[get_db] = lambda: None
        fastapi_app.dependency_overrides[get_current_user] = lambda: user
        self._cfg = patch.object(
            monitoring_routes,
            "read_monitoring_config",
            return_value={
                "vlogs_url": "http://192.168.3.235:9428",
                "verify_tls": True,
                "timeout_seconds": 15,
            },
        )
        self._cfg.start()

    def tearDown(self) -> None:
        self._cfg.stop()
        fastapi_app.dependency_overrides.pop(get_db, None)
        fastapi_app.dependency_overrides.pop(get_current_user, None)

    def test_field_names_carries_query_param(self) -> None:
        """枚举字段名时必须带 query（VLogs v1.50 缺 query 直接 400）。"""
        captured: dict = {}

        def _fake_get(url, params=None, **kwargs):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _FakeUpstream({"values": ["level", "job"]})

        with patch("app.api.monitoring.routes.requests.get", side_effect=_fake_get):
            r = self.client.post("/api/monitoring/logs/fields", json={})

        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(captured["url"].endswith("/field_names"), captured["url"])
        self.assertEqual(captured["params"].get("query"), "*")
        self.assertIn("start", captured["params"])
        self.assertIn("end", captured["params"])

    def test_field_values_carries_field_and_query(self) -> None:
        """枚举字段取值时带 field + query + limit。"""
        captured: dict = {}

        def _fake_get(url, params=None, **kwargs):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _FakeUpstream({"values": [{"value": "container"}, {"value": "opspilot-node"}]})

        with patch("app.api.monitoring.routes.requests.get", side_effect=_fake_get):
            r = self.client.post(
                "/api/monitoring/logs/fields", json={"field": "job", "query": "*"}
            )

        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(captured["url"].endswith("/field_values"), captured["url"])
        self.assertEqual(captured["params"].get("field"), "job")
        self.assertEqual(captured["params"].get("query"), "*")
        self.assertEqual(captured["params"].get("limit"), 200)

    def test_fields_unconfigured_returns_empty(self) -> None:
        """未配置 vlogs_url 时不打上游，返回 configured=False。"""
        self._cfg.stop()
        with patch.object(
            monitoring_routes, "read_monitoring_config", return_value={"vlogs_url": ""}
        ):
            r = self.client.post("/api/monitoring/logs/fields", json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["configured"])


if __name__ == "__main__":
    unittest.main()
