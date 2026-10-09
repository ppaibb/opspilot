"""Grafana SSO / 后端反代契约测试。

覆盖四块：
1. 纯函数：角色映射、上游地址规范化、同源 Location 改写、auth.proxy 头构造、
   静态资源路径判定；
2. `/api/monitoring/grafana/sso-link`：只返同源相对地址，未配置 Grafana 时 400；
3. `/grafana/*` 反代路由：票据换身份头并种会话 Cookie、会话 Cookie 续期、
   `token_version` 过期即失效、静态资源匿名放行、无票无 Cookie 401、
   Location 改写、上游连接失败 502；
4. `/api/monitoring/grafana/auth-validate`：边缘 Nginx `auth_request` 兼容入口。

反代用例全部 mock 掉 `requests.request`，不依赖真实 Grafana。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from jose import jwt

from app.api import grafana_sso
from app.api.grafana_sso import (
    GRAFANA_PROXY_PREFIX,
    GRAFANA_SESSION_COOKIE,
    _build_grafana_login,
    _build_proxy_headers,
    _grafana_upstream_base,
    _identity_headers,
    _is_public_grafana_path,
    _map_grafana_role,
    _same_origin_location,
    _to_header_safe_value,
    _user_identity_headers,
)
from app.core.config import settings
from app.core.security import get_current_user
from app.db.database import get_db
from app.main import app as fastapi_app
from app.models.user import User
from tests.asgi_client import ASGITestClient

GRAFANA_CFG = {
    "grafana_url": "http://192.168.3.99:3002",
    "verify_tls": True,
    "timeout_seconds": 15,
}


def _fake_user(
    username: str = "admin",
    role: str = "管理员",
    email: str = "",
    user_id: int | None = 1,
    token_version: int = 0,
    status: str = "active",
) -> User:
    """构造一个未持久化的用户对象，供 get_current_user / 反代查库覆盖用。"""
    return User(
        id=user_id,
        username=username,
        name=f"{username}-display",
        email=email,
        role=role,
        status=status,
        token_version=token_version,
    )


class _FakeQuery:
    """最小可用的 SQLAlchemy Query 替身：按 `Model.<列> == 值` 过滤。"""

    def __init__(self, users: list[User]):
        self._users = list(users)

    def filter(self, criterion):  # noqa: ANN001
        column = getattr(getattr(criterion, "left", None), "name", None)
        expected = getattr(getattr(criterion, "right", None), "value", None)
        if column is None:
            return _FakeQuery(self._users)
        return _FakeQuery(
            [u for u in self._users if str(getattr(u, column, None)) == str(expected)]
        )

    def first(self) -> User | None:
        return self._users[0] if self._users else None


class _FakeDb:
    """只实现反代用到的 `db.query(User)`。"""

    def __init__(self, users: list[User]):
        self._users = users

    def query(self, _model) -> _FakeQuery:
        return _FakeQuery(self._users)


class _FakeUpstreamResponse:
    """最小可用的 requests.Response 替身。"""

    def __init__(self, status_code: int = 200, headers: dict | None = None, body: bytes = b"ok"):
        self.status_code = status_code
        self.headers = dict(headers or {"content-type": "text/plain"})
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        yield self._body

    def close(self) -> None:
        self.closed = True


class GrafanaSsoHelperTest(unittest.TestCase):
    """身份映射、地址规范化与静态资源判定。"""

    def test_role_mapping_admin_variants(self) -> None:
        """管理员的各种写法都映射为 Grafana Admin。"""
        for role in ("管理员", "admin", "Administrator", " 管理员 "):
            self.assertEqual(_map_grafana_role(role), "Admin", role)

    def test_role_mapping_non_admin_is_viewer(self) -> None:
        """非管理员一律 Viewer，空值也不放行成 Admin。"""
        for role in ("只读", "运维", "DBA", "", None, "guest"):
            self.assertEqual(_map_grafana_role(role), "Viewer", str(role))

    def test_upstream_base_appends_sub_path(self) -> None:
        """用户填裸端口或带 /grafana 都要归一成同一上游基地址。"""
        self.assertEqual(_grafana_upstream_base("http://h:3002"), "http://h:3002/grafana")
        self.assertEqual(_grafana_upstream_base("http://h:3002/"), "http://h:3002/grafana")
        self.assertEqual(_grafana_upstream_base("http://h:3002/grafana"), "http://h:3002/grafana")
        self.assertEqual(_grafana_upstream_base("http://h:3002/grafana/"), "http://h:3002/grafana")

    def test_upstream_base_rejects_garbage(self) -> None:
        """空值与非法 scheme 一律返回空串，交给调用方报错。"""
        for value in ("", "   ", "h:3002", "ftp://h:3002", "http://", None):
            self.assertEqual(_grafana_upstream_base(str(value or "")), "", str(value))

    def test_upstream_base_drops_query_and_fragment(self) -> None:
        """查询串与片段不能进入上游基地址。"""
        self.assertEqual(
            _grafana_upstream_base("http://h:3002/grafana?x=1#frag"),
            "http://h:3002/grafana",
        )

    def test_same_origin_location_strips_foreign_host(self) -> None:
        """跨源绝对跳转改写为 path?query，查询串必须保留。"""
        rewritten = _same_origin_location("http://127.0.0.1:3002/grafana/explore?left=%7B%7D", "opspilot.local")
        self.assertEqual(rewritten, "/grafana/explore?left=%7B%7D")

    def test_same_origin_location_keeps_same_host_and_relative(self) -> None:
        """指向当前 Host 的地址与相对地址原样返回。"""
        self.assertEqual(_same_origin_location("http://opspilot.local/grafana/", "opspilot.local"), "http://opspilot.local/grafana/")
        self.assertEqual(_same_origin_location("/grafana/login", "opspilot.local"), "/grafana/login")
        self.assertEqual(_same_origin_location("", "opspilot.local"), "")

    def test_grafana_login_prefixes_and_sanitizes(self) -> None:
        """登录名统一加 opspilot_ 前缀，非法字符归一，空值兜底。"""
        self.assertEqual(_build_grafana_login("admin"), "opspilot_admin")
        self.assertEqual(_build_grafana_login("zhang san/01"), "opspilot_zhang_san_01")
        self.assertEqual(_build_grafana_login(""), "opspilot_user")
        self.assertEqual(_build_grafana_login("___"), "opspilot_user")

    def test_header_safe_value_falls_back_on_non_latin1(self) -> None:
        """非 latin-1 字符回退到备用值，避免响应直接抛异常。"""
        self.assertEqual(_to_header_safe_value("中文名", fallback_value="fallback"), "fallback")
        self.assertEqual(_to_header_safe_value("ascii", fallback_value="fallback"), "ascii")
        self.assertEqual(_to_header_safe_value("", fallback_value=""), "")

    def test_public_path_allowlist(self) -> None:
        """Grafana 自己就匿名开放的静态资源必须放行，业务接口一律不放。"""
        for path in (
            "public/build/app.js",
            "/public/build/app.js",
            "public/img/grafana_icon.svg",
            "avatar/abc",
            "favicon.ico",
            "robots.txt",
        ):
            self.assertTrue(_is_public_grafana_path(path), path)

        for path in ("", "/", "api/user", "api/health", "login", "logout", "explore", "d/uid/slug"):
            self.assertFalse(_is_public_grafana_path(path), path)


class GrafanaIdentityHeadersTest(unittest.TestCase):
    """auth.proxy 请求头构造。"""

    def test_identity_headers_prefix_login_and_map_role(self) -> None:
        headers = _identity_headers(
            username="admin",
            display_name="系统管理员",
            email="admin@opspilot.local",
            opspilot_role="管理员",
        )
        self.assertEqual(headers["X-WEBAUTH-USER"], "opspilot_admin")
        self.assertEqual(headers["X-WEBAUTH-ROLE"], "Admin")
        self.assertEqual(headers["X-WEBAUTH-EMAIL"], "admin@opspilot.local")
        # 中文名无法进 Header，回退到用户名
        self.assertEqual(headers["X-WEBAUTH-NAME"], "admin")

    def test_identity_headers_name_falls_back_to_username(self) -> None:
        """缺 name 时回退到用户名，空 email 则整个头不下发。"""
        headers = _identity_headers(username="x", display_name="", email="", opspilot_role="只读")
        self.assertEqual(headers["X-WEBAUTH-NAME"], "x")
        self.assertNotIn("X-WEBAUTH-EMAIL", headers)
        self.assertEqual(headers["X-WEBAUTH-ROLE"], "Viewer")

    def test_user_identity_headers_read_live_record(self) -> None:
        """从用户记录取身份：用户名加前缀、角色归一化。"""
        headers = _user_identity_headers(_fake_user(username="viewer01", role="只读", email="v@opspilot.local"))
        self.assertEqual(headers["X-WEBAUTH-USER"], "opspilot_viewer01")
        self.assertEqual(headers["X-WEBAUTH-ROLE"], "Viewer")
        self.assertEqual(headers["X-WEBAUTH-EMAIL"], "v@opspilot.local")

    def test_build_proxy_headers_strips_hop_by_hop_and_sets_forwarded(self) -> None:
        request = SimpleNamespace(
            headers={
                "host": "opspilot.local",
                "connection": "keep-alive",
                "content-length": "10",
                "accept-encoding": "gzip",
                "cookie": "opspilot_grafana_session=abc",
                "x-forwarded-proto": "https",
            },
            url=SimpleNamespace(netloc="opspilot.local", scheme="http"),
            client=SimpleNamespace(host="10.0.0.9"),
        )

        headers = _build_proxy_headers(request, None)

        for hop in ("connection", "content-length", "accept-encoding"):
            self.assertNotIn(hop, headers, hop)
        self.assertEqual(headers["Host"], "opspilot.local")
        self.assertEqual(headers["X-Forwarded-Proto"], "https")
        self.assertEqual(headers["X-Forwarded-Host"], "opspilot.local")
        self.assertEqual(headers["X-Forwarded-For"], "10.0.0.9")
        self.assertEqual(headers["cookie"], "opspilot_grafana_session=abc")
        # 没有身份时不注入任何 X-WEBAUTH-* 头
        self.assertFalse([k for k in headers if k.lower().startswith("x-webauth")])

    def test_build_proxy_headers_merges_identity(self) -> None:
        request = SimpleNamespace(
            headers={"host": "opspilot.local"},
            url=SimpleNamespace(netloc="opspilot.local", scheme="http"),
            client=None,
        )
        headers = _build_proxy_headers(request, _fake_user(username="a", role="管理员"))
        self.assertEqual(headers["X-WEBAUTH-USER"], "opspilot_a")
        self.assertEqual(headers["X-WEBAUTH-ROLE"], "Admin")


class GrafanaSsoLinkRouteTest(unittest.TestCase):
    """sso-link 路由。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = ASGITestClient(fastapi_app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        fastapi_app.dependency_overrides[get_db] = lambda: None
        fastapi_app.dependency_overrides[get_current_user] = lambda: _fake_user()

    def tearDown(self) -> None:
        fastapi_app.dependency_overrides.pop(get_db, None)
        fastapi_app.dependency_overrides.pop(get_current_user, None)

    def test_sso_link_is_same_origin_relative(self) -> None:
        """返回相对地址：不带 scheme/host，票据放在 opspilot_sso 上。"""
        with patch.object(grafana_sso, "read_monitoring_config", return_value=dict(GRAFANA_CFG)):
            r = self.client.get("/api/monitoring/grafana/sso-link")
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["url"]
        self.assertTrue(url.startswith(f"{GRAFANA_PROXY_PREFIX}/?"), url)
        self.assertNotIn("http://", url)
        self.assertNotIn("https://", url)
        self.assertIn("opspilot_sso=", url)
        self.assertTrue(r.json()["expires_at"])

    def test_sso_link_400_when_grafana_not_configured(self) -> None:
        with patch.object(grafana_sso, "read_monitoring_config", return_value={"grafana_url": ""}):
            r = self.client.get("/api/monitoring/grafana/sso-link")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Grafana", r.json()["detail"])


class GrafanaProxyRouteTest(unittest.TestCase):
    """`/grafana/*` 反代路由。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = ASGITestClient(fastapi_app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        self.user = _fake_user(
            username="admin",
            role="管理员",
            email="tester@opspilot.local",
            user_id=7,
            token_version=0,
        )
        fastapi_app.dependency_overrides[get_db] = lambda: _FakeDb([self.user])
        self._cfg = patch.object(grafana_sso, "read_monitoring_config", return_value=dict(GRAFANA_CFG))
        self._cfg.start()

    def tearDown(self) -> None:
        self._cfg.stop()
        fastapi_app.dependency_overrides.pop(get_db, None)

    def _valid_ticket(self, username: str = "admin", role: str = "管理员") -> str:
        """用同一把 SECRET_KEY 签一张合法票据。"""
        payload = {
            "sub": username,
            "name": "tester",
            "email": "tester@opspilot.local",
            "role": role,
            "aud": grafana_sso.GRAFANA_SSO_AUDIENCE,
            "type": grafana_sso.GRAFANA_SSO_TYPE,
            "exp": datetime.utcnow() + timedelta(seconds=60),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def _valid_session(self, user: User | None = None, token_version: int | None = None) -> str:
        """签一张合法的 Grafana 会话 Cookie 值。"""
        target = user or self.user
        payload = {
            "sub": target.username,
            "uid": target.id,
            "tv": target.token_version if token_version is None else token_version,
            "aud": grafana_sso.GRAFANA_SSO_AUDIENCE,
            "type": grafana_sso.GRAFANA_SESSION_TYPE,
            "exp": datetime.utcnow() + timedelta(seconds=600),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def test_proxy_injects_identity_strips_ticket_and_sets_cookie(self) -> None:
        """票据换身份头 + 会话 Cookie，转发 URL 上不能残留票据。"""
        captured: dict = {}

        def _fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            return _FakeUpstreamResponse(200, {"content-type": "application/json"}, b'{"ok":true}')

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get(f"/grafana/api/user?opspilot_sso={self._valid_ticket()}")

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(captured["url"], "http://192.168.3.99:3002/grafana/api/user")
        self.assertNotIn("opspilot_sso", captured["url"])
        self.assertEqual(captured["headers"]["X-WEBAUTH-USER"], "opspilot_admin")
        self.assertEqual(captured["headers"]["X-WEBAUTH-ROLE"], "Admin")
        self.assertEqual(captured["headers"]["X-WEBAUTH-EMAIL"], "tester@opspilot.local")
        self.assertEqual(captured["headers"]["Host"], "testserver")
        # 一次性票据必须换成 8 小时 HttpOnly 会话 Cookie
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn(f"{GRAFANA_SESSION_COOKIE}=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn(f"Path={GRAFANA_PROXY_PREFIX}", set_cookie)

    def test_proxy_forwards_remaining_query(self) -> None:
        """票据之外的查询串要原样带给 Grafana，含 JSON 也要正确编码。"""
        captured: dict = {}

        def _fake_request(method, url, **kwargs):
            captured["url"] = url
            return _FakeUpstreamResponse()

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get(
                f"/grafana/explore?orgId=1&opspilot_sso={self._valid_ticket()}&left=%7B%7D"
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("orgId=1", captured["url"])
        # 解码后再编码，花括号必须重新转义，不能裸奔进 URL
        self.assertIn("left=%7B%7D", captured["url"])
        self.assertNotIn("opspilot_sso", captured["url"])

    def test_proxy_401_without_ticket_or_session(self) -> None:
        """两者都没有直接 401，防止被当成开放反代。"""
        r = self.client.get("/grafana/api/user")
        self.assertEqual(r.status_code, 401)
        self.assertIn("重新进入", r.json()["detail"])

    def test_proxy_rejects_invalid_ticket(self) -> None:
        r = self.client.get("/grafana/api/user?opspilot_sso=not-a-jwt")
        self.assertEqual(r.status_code, 401)

    def test_proxy_rejects_ticket_of_wrong_type(self) -> None:
        """会话 Cookie 的值不能当一次性票据用（防止把它贴到地址栏上）。"""
        r = self.client.get(f"/grafana/api/user?opspilot_sso={self._valid_session()}")
        self.assertEqual(r.status_code, 401)

    def test_proxy_session_cookie_reinjects_identity(self) -> None:
        """会话 Cookie 放行，且身份从库里最新记录重新注入。"""
        captured: dict = {}

        def _fake_request(method, url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return _FakeUpstreamResponse()

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get(
                "/grafana/api/user",
                cookies={GRAFANA_SESSION_COOKIE: self._valid_session()},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["headers"]["X-WEBAUTH-USER"], "opspilot_admin")
        self.assertEqual(captured["headers"]["X-WEBAUTH-ROLE"], "Admin")
        # 会话还在，不再重复种 Cookie
        self.assertNotIn("set-cookie", r.headers)

    def test_proxy_session_cookie_dies_with_token_version(self) -> None:
        """重置密码/禁用账号让 token_version 递增后，会话 Cookie 立即失效。"""
        r = self.client.get(
            "/grafana/api/user",
            cookies={GRAFANA_SESSION_COOKIE: self._valid_session(token_version=99)},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("重新进入", r.json()["detail"])

    def test_proxy_session_cookie_dies_with_disabled_user(self) -> None:
        """账号被禁用后会话 Cookie 立即失效。"""
        disabled = _fake_user(username="admin", role="管理员", user_id=7, status="disabled")
        fastapi_app.dependency_overrides[get_db] = lambda: _FakeDb([disabled])
        r = self.client.get(
            "/grafana/api/user",
            cookies={GRAFANA_SESSION_COOKIE: self._valid_session()},
        )
        self.assertEqual(r.status_code, 401)

    def test_proxy_public_assets_need_no_identity(self) -> None:
        """Grafana 静态资源匿名放行，且不注入任何身份头。"""
        captured: dict = {}

        def _fake_request(method, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            return _FakeUpstreamResponse(200, {"content-type": "application/javascript"}, b"console.log(1)")

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get("/grafana/public/build/app.bbd213ecba15db924b4e.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["url"], "http://192.168.3.99:3002/grafana/public/build/app.bbd213ecba15db924b4e.js")
        self.assertFalse([k for k in captured["headers"] if k.lower().startswith("x-webauth")])

    def test_proxy_session_cookie_resolves_only_its_own_user(self) -> None:
        """库里多人时，会话 Cookie 必须按 uid 精确取人，不能取到第一个。"""
        viewer = _fake_user(
            username="viewer08",
            role="只读",
            email="v8@opspilot.local",
            user_id=8,
        )
        fastapi_app.dependency_overrides[get_db] = lambda: _FakeDb([self.user, viewer])
        captured: dict = {}

        def _fake_request(method, url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return _FakeUpstreamResponse()

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get(
                "/grafana/api/user",
                cookies={GRAFANA_SESSION_COOKIE: self._valid_session(user=viewer)},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["headers"]["X-WEBAUTH-USER"], "opspilot_viewer08")
        self.assertEqual(captured["headers"]["X-WEBAUTH-ROLE"], "Viewer")
        self.assertEqual(captured["headers"]["X-WEBAUTH-EMAIL"], "v8@opspilot.local")

    def test_proxy_404_when_grafana_not_configured(self) -> None:
        self._cfg.stop()
        with patch.object(grafana_sso, "read_monitoring_config", return_value={"grafana_url": ""}):
            r = self.client.get(f"/grafana/api/user?opspilot_sso={self._valid_ticket()}")
        self.assertEqual(r.status_code, 404)

    def test_proxy_rewrites_cross_origin_location(self) -> None:
        """Grafana 的绝对 Location 改写为同源相对地址。"""
        upstream = _FakeUpstreamResponse(
            302,
            {"location": "http://127.0.0.1:3002/grafana/explore?left=x", "content-type": "text/html"},
        )

        def _fake_request(method, url, **kwargs):
            return upstream

        with patch("app.api.grafana_sso.requests.request", side_effect=_fake_request):
            r = self.client.get(
                f"/grafana/d/uid/slug?opspilot_sso={self._valid_ticket()}", follow_redirects=False
            )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/grafana/explore?left=x")

    def test_proxy_502_on_upstream_failure(self) -> None:
        import requests

        with patch("app.api.grafana_sso.requests.request", side_effect=requests.ConnectionError("refused")):
            r = self.client.get(f"/grafana/api/user?opspilot_sso={self._valid_ticket()}")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Grafana", r.json()["detail"])

    def test_proxy_drops_hop_by_hop_response_headers(self) -> None:
        """content-length/content-encoding/set-cookie 不转发，交给本框架重算。"""
        upstream = _FakeUpstreamResponse(
            200,
            {
                "content-length": "99",
                "content-encoding": "gzip",
                "set-cookie": "grafana_session=leaked; Path=/grafana",
                "x-grafana": "1",
            },
        )

        with patch("app.api.grafana_sso.requests.request", return_value=upstream):
            r = self.client.get(f"/grafana/api/user?opspilot_sso={self._valid_ticket()}")
        self.assertEqual(r.status_code, 200)
        lowered = {k.lower() for k in r.headers}
        self.assertNotIn("content-length", lowered)
        self.assertNotIn("content-encoding", lowered)
        self.assertEqual(r.headers.get("x-grafana"), "1")

    def test_proxy_logout_clears_cookie_and_returns_to_opspilot(self) -> None:
        """退出登录：清掉会话 Cookie，把人带回 OpsPilot 监控页。"""
        r = self.client.get(
            "/grafana/logout",
            cookies={GRAFANA_SESSION_COOKIE: self._valid_session()},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/monitoring/metrics")
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn(f"{GRAFANA_SESSION_COOKIE}=", set_cookie)


class GrafanaAuthValidateRouteTest(unittest.TestCase):
    """边缘 Nginx auth_request 兼容入口。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = ASGITestClient(fastapi_app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def _ticket(self, role: str = "只读") -> str:
        payload = {
            "sub": "viewer01",
            "name": "read only",
            "email": "v@opspilot.local",
            "role": role,
            "aud": grafana_sso.GRAFANA_SSO_AUDIENCE,
            "type": grafana_sso.GRAFANA_SSO_TYPE,
            "exp": datetime.utcnow() + timedelta(seconds=60),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def test_auth_validate_returns_headers(self) -> None:
        r = self.client.get(
            "/api/monitoring/grafana/auth-validate",
            headers={"X-OpsPilot-SSO-Token": self._ticket()},
        )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["x-opspilot-user"], "opspilot_viewer01")
        self.assertEqual(r.headers["x-opspilot-role"], "Viewer")

    def test_auth_validate_maps_admin_role(self) -> None:
        r = self.client.get(
            "/api/monitoring/grafana/auth-validate",
            headers={"X-OpsPilot-SSO-Token": self._ticket(role="管理员")},
        )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["x-opspilot-role"], "Admin")

    def test_auth_validate_401_without_token(self) -> None:
        r = self.client.get("/api/monitoring/grafana/auth-validate")
        self.assertEqual(r.status_code, 401)


class GrafanaProxyRouteRegistrationTest(unittest.TestCase):
    """反代路由必须真的挂在 app 上，且不带 get_current_user。"""

    CATCH_ALL = f"{GRAFANA_PROXY_PREFIX}/{{grafana_path:path}}"

    def _catch_all_route(self):
        for route in fastapi_app.routes:
            if getattr(route, "path", "") == self.CATCH_ALL:
                return route
        return None

    def test_catch_all_route_registered(self) -> None:
        self.assertIsNotNone(self._catch_all_route(), "Grafana 反代路由未注册")

    def test_catch_all_supports_write_methods(self) -> None:
        route = self._catch_all_route()
        self.assertIsNotNone(route)
        methods = getattr(route, "methods", set()) or set()
        self.assertLessEqual({"GET", "POST", "PUT", "PATCH", "DELETE"}, methods)

    def test_catch_all_has_no_auth_dependency(self) -> None:
        """反代靠一次性票据或会话 Cookie 放行，不能挂 get_current_user。"""
        route = self._catch_all_route()
        self.assertIsNotNone(route)
        dependant = getattr(route, "dependant", None)
        calls = [dep.call for dep in dependant.dependencies] if dependant else []
        self.assertNotIn(get_current_user, calls)
        self.assertIn(get_db, calls)


if __name__ == "__main__":
    unittest.main()
