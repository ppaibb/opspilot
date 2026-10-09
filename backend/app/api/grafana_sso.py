"""
Grafana 免密跳转与身份桥接。

架构说明（后端代理模式）：
1. 前端调用 `/api/monitoring/grafana/sso-link`，用当前登录用户换一枚 90 秒 SSO Token；
2. 前端跳到**同源**的 `/grafana/<目标页>?opspilot_sso=<token>`；
3. 本文件的 `grafana_proxy_router` 校验 Token，把用户身份写成 Grafana `auth.proxy`
   需要的 `X-WEBAUTH-USER / NAME / EMAIL / ROLE` 头，再转发给 Grafana，同时给浏览器
   种一枚 8 小时的 HttpOnly 会话 Cookie（`opspilot_grafana_session`）；
4. 后续请求靠这枚 Cookie 认人，身份头**每次请求都重新注入**——Grafana 的
   `auth.proxy` 本来就逐请求解析 `X-WEBAUTH-*`，不需要依赖它自己的会话 Cookie；
5. Grafana 的静态资源（`/public/**`）它本身就对匿名开放，这些请求是浏览器解析
   index.html 后自动发起的、不可能带上票据，因此单独放行；
6. 既没有票据也没有会话 Cookie 的非静态请求一律 401，避免把 Grafana 暴露成开放反代。

为什么走后端代理而不是让浏览器直连 Grafana：
1. 监控节点常与 OpsPilot 分离部署，浏览器未必能直连 Grafana 地址，而后端一定可以；
2. `vm_url` / `grafana_url` 只存在后端 SystemConfig，前端拿不到内网地址；
3. Token 校验与身份映射集中在后端，和仓库既有「前端不直连 VM/VLogs/Grafana」一致。

另保留 `auth-validate` 接口：如果部署环境更希望由边缘 Nginx 做 `auth_request`
（见 `deploy/monitoring/nginx-monitoring.conf`），仍可用原来的链路，两个入口
共用同一套 Token 与身份映射逻辑。

安全审计：
1. 不把 Grafana 账号密码下发给前端；
2. SSO Token 用 OpsPilot SECRET_KEY 签名，默认 90 秒过期，转发前从查询串剥离，
   不会进入 Grafana 页面地址与访问日志；
3. 会话 Cookie 里只放 `uid + token_version`，身份信息每次请求都回库重取，
   改角色即时生效；用户被禁用或重置密码（`token_version` 递增）后，Cookie 立即失效；
4. 用户真实权限仍留在 OpsPilot，本文件只做 Grafana 身份桥接（管理员→Admin，
   其余→Viewer）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from urllib.parse import quote, urlencode, urlparse, urlunparse

import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.monitoring import read_monitoring_config
from app.core.config import settings
from app.core.rbac import normalize_role_name
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.user import User

router = APIRouter()

# Grafana UI 反代：与 Grafana 子路径部署约定一致，浏览器侧入口固定为 /grafana。
GRAFANA_PROXY_PREFIX = "/grafana"
GRAFANA_SUB_PATH = "/grafana"

GRAFANA_SSO_AUDIENCE = "grafana-sso"
GRAFANA_SSO_TYPE = "grafana-sso"
GRAFANA_SESSION_TYPE = "grafana-session"
# 地址栏里的票据越短命越好：它会被复制、会进浏览器历史。
GRAFANA_SSO_EXPIRE_SECONDS = 90
# 换出来的会话 Cookie 是 HttpOnly 的，可以放宽到一次班次的时长。
GRAFANA_SESSION_EXPIRE_SECONDS = 8 * 60 * 60
GRAFANA_SESSION_COOKIE = "opspilot_grafana_session"

# OpsPilot 角色名 → Grafana 角色。库里存的是中文展示名，admin/administrator
# 是部署环境常见英文别名，统一走 normalize_role_name 归一化后再比较。
GRAFANA_ADMIN_ROLE_NAME = "管理员"

# Grafana 自己就对匿名开放的静态资源（容器内实测 `/public/build/*.js` 不带任何
# 请求头也是 200）。它们是浏览器解析 index.html 之后自动发起的，压根不可能带上
# 一次性票据，所以必须单独放行——否则首屏 CSS/JS 全部 401，Grafana 直接白屏。
# 这些路径不含任何业务数据，放行不会比 Grafana 本身暴露得更多。
GRAFANA_PUBLIC_PATH_PREFIXES = ("public/", "avatar/")
GRAFANA_PUBLIC_PATHS = frozenset({"favicon.ico", "robots.txt"})

# 会话失效时统一提示。文案里要点明「从 OpsPilot 监控页面重新进入」，
# 因为用户此时看到的是 Grafana 的界面，容易以为要去 Grafana 登录。
_PROXY_SESSION_EXPIRED_DETAIL = "Grafana 会话已失效，请从 OpsPilot 监控页面重新进入"

# 反代不转发的头：逐跳头 + 由 requests 自行计算的头。
# accept-encoding 一并剥掉，避免 requests 解压 gzip 后 content-encoding 头对不上。
HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "content-encoding",
    # Set-Cookie 名义上不是逐跳头，但 requests 会把上游多个 Set-Cookie 逗号
    # 连接成同一个值，原样回给浏览器只会得到一棵坏 Cookie。Grafana 自己的会话
    # Cookie 对代理也没有用处——身份头每次请求都会重新注入，见 proxy_grafana。
    "set-cookie",
}


class GrafanaSSOLinkResponse(BaseModel):
    """Grafana 跳转链接响应。"""

    url: str
    expires_at: str


def _to_header_safe_value(raw_value: str, fallback_value: str = "") -> str:
    """将任意字符串转换为可安全写入 HTTP Header 的值。

    说明：
    1. HTTP Header 在 Starlette 中最终会按 latin-1 编码；
    2. 中文等非 latin-1 字符会导致响应直接抛异常；
    3. 这里优先保留原值，无法编码时自动回退到备用值，避免 Grafana SSO 校验链路中断。
    """
    value = str(raw_value or "").strip()
    if not value:
        value = str(fallback_value or "").strip()
    if not value:
        return ""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        fallback = str(fallback_value or "").strip()
        if not fallback:
            return ""
        try:
            fallback.encode("latin-1")
            return fallback
        except UnicodeEncodeError:
            return ""


def _build_grafana_login(raw_username: str) -> str:
    """为 Grafana 生成独立登录名，避免与 Grafana 本地账号撞名。

    设计原因：
    1. 如果直接把 OpsPilot 用户名写入 `X-WEBAUTH-USER`，当用户名恰好为 `admin`
       时，会直接命中 Grafana 内置管理员账号；
    2. 这里统一追加 `opspilot_` 前缀，并把非常规字符归一为下划线；
    3. 即使后续启用更多 OpsPilot 用户，也不会污染或覆盖 Grafana 本地账号体系。
    """
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(raw_username or "").strip())
    normalized = normalized.strip("._-")
    if not normalized:
        normalized = "user"
    return f"opspilot_{normalized}"


def _map_grafana_role(opspilot_role: str | None) -> str:
    """把 OpsPilot 角色映射为 Grafana 角色。

    管理员映射为 Grafana Admin（可管理数据源/看板），其余一律 Viewer：
    Grafana 侧只做可视化承载，真正的写权限仍由 OpsPilot 的 RBAC 收口。
    """
    return "Admin" if normalize_role_name(opspilot_role) == GRAFANA_ADMIN_ROLE_NAME else "Viewer"


def _grafana_upstream_base(grafana_url: str) -> str:
    """规范化后端访问 Grafana 的基地址，统一带上 `/grafana` 子路径。

    用户可能填写 `http://host:3002` 或 `http://host:3002/grafana`，两种都要能拼出
    `http://host:3002/grafana`；查询串与片段一律丢弃。
    """
    base = str(grafana_url or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith(GRAFANA_SUB_PATH):
        path = path[: -len(GRAFANA_SUB_PATH)]
    return urlunparse((parsed.scheme, parsed.netloc, f"{path}{GRAFANA_SUB_PATH}", "", "", ""))


def _create_grafana_sso_token(user: User, expire_at: datetime) -> str:
    """创建短期 Grafana SSO Token（只放在地址栏里，随用随抛）。

    载荷里带上姓名/邮箱/角色，是为了让边缘 Nginx 那条 `auth-validate` 链路
    （不查库）也能算出身份头；后端反代链路会再回库取一次最新值，以数据库为准。
    """
    payload = {
        "sub": str(user.username or ""),
        "name": str(user.name or user.username or ""),
        "email": str(user.email or ""),
        "role": str(user.role or ""),
        "aud": GRAFANA_SSO_AUDIENCE,
        "type": GRAFANA_SSO_TYPE,
        "exp": expire_at,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _create_grafana_session_token(user: User) -> str:
    """创建 Grafana 会话 Token（只放在 HttpOnly Cookie 里）。

    载荷刻意不含姓名/邮箱/角色：这些每次都回库取最新的，避免用户改角色后
    Grafana 侧还按旧角色放行。`token_version` 用来在禁用账号/重置密码时
    立刻作废全部已签发会话，与 OpsPilot 主登录体系同一套机制。
    """
    payload = {
        "sub": str(user.username or ""),
        "uid": user.id,
        "tv": int(user.token_version or 0),
        "aud": GRAFANA_SSO_AUDIENCE,
        "type": GRAFANA_SESSION_TYPE,
        "exp": datetime.utcnow() + timedelta(seconds=GRAFANA_SESSION_EXPIRE_SECONDS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decode_grafana_sso_token(raw_token: str, expected_type: str) -> dict:
    """解码并校验 Grafana 登录类 Token，类型不符一律拒绝。"""
    try:
        payload = jwt.decode(
            raw_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=GRAFANA_SSO_AUDIENCE,
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Grafana 登录凭证非法或已过期") from exc
    if payload.get("type") != expected_type:
        raise HTTPException(status_code=401, detail="Grafana 登录凭证类型非法")
    return payload


def _identity_headers(*, username: str, display_name: str, email: str, opspilot_role: str) -> dict[str, str]:
    """把 OpsPilot 用户身份翻译成 Grafana auth.proxy 需要的请求头。

    入参统一传**原始用户名**，`opspilot_` 前缀在这里统一加，避免调用方重复拼接。
    """
    headers = {
        "X-WEBAUTH-USER": _to_header_safe_value(
            _build_grafana_login(username),
            fallback_value=_build_grafana_login(display_name) or "opspilot_user",
        ),
        "X-WEBAUTH-ROLE": _map_grafana_role(opspilot_role),
    }
    display = _to_header_safe_value(display_name, fallback_value=str(username or ""))
    if display:
        headers["X-WEBAUTH-NAME"] = display
    mail = _to_header_safe_value(email, fallback_value="")
    if mail:
        headers["X-WEBAUTH-EMAIL"] = mail
    return headers


def _user_identity_headers(user: User) -> dict[str, str]:
    """用数据库里的最新用户记录生成身份头（角色改名、改邮箱即时生效）。"""
    return _identity_headers(
        username=str(user.username or ""),
        display_name=str(user.name or user.username or ""),
        email=str(user.email or ""),
        opspilot_role=str(user.role or ""),
    )


def _same_origin_location(location: str, request_host: str) -> str:
    """把 Grafana 的绝对跳转地址改写成同源相对地址。

    Grafana 的 `root_url` 常配成容器内地址（如 `http://127.0.0.1:3002/grafana/`），
    直接透传会把浏览器带去它本地的 3002 端口。这里只保留 path?query，让浏览器继续
    走在同源反代后面；指向当前 Host 的相对/同源地址不动。
    """
    value = str(location or "").strip()
    if not value:
        return value
    parsed = urlparse(value)
    if not parsed.netloc:
        return value
    if parsed.netloc == request_host:
        return value
    return urlunparse(("", "", parsed.path, parsed.params, parsed.query, parsed.fragment)) or "/"


def _build_proxy_headers(request: Request, user: User | None) -> dict[str, str]:
    """构造转发给 Grafana 的请求头。"""
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_REQUEST_HEADERS
    }
    # 让 Grafana 看到浏览器真实的 Host 与协议，root_url 用 %(domain)s 之类占位符时
    # 才能拼出浏览器可用的地址。
    headers["Host"] = str(request.headers.get("host") or request.url.netloc)
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme)
    headers["X-Forwarded-Proto"] = forwarded_proto
    headers["X-Forwarded-Host"] = str(request.headers.get("host") or request.url.netloc)
    headers["X-Forwarded-For"] = str(request.headers.get("x-forwarded-for") or (request.client.host if request.client else ""))
    if user is not None:
        headers.update(_user_identity_headers(user))
    return headers


@router.get("/api/monitoring/grafana/sso-link", response_model=GrafanaSSOLinkResponse)
def get_grafana_sso_link(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """生成 Grafana 免密登录链接。

    返回**同源相对地址**：浏览器从哪个 origin 打开本系统，就从哪个 origin 进
    Grafana，由后端反代完成身份注入，因此 Grafana 地址无需暴露给浏览器。
    """
    cfg = read_monitoring_config(db)
    if not _grafana_upstream_base(str(cfg.get("grafana_url") or "")):
        raise HTTPException(
            status_code=400,
            detail="请先在“系统设置 - 系统管理 - 监控连接”中填写 Grafana 地址",
        )
    # 这里单独计算一次过期时间，避免前端展示时间与 Token 实际时间出现明显偏差。
    expire_at = datetime.utcnow() + timedelta(seconds=GRAFANA_SSO_EXPIRE_SECONDS)
    token = _create_grafana_sso_token(current_user, expire_at)
    login_url = f"{GRAFANA_PROXY_PREFIX}/?opspilot_sso={quote(token, safe='')}"
    return {
        "url": login_url,
        "expires_at": expire_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/monitoring/grafana/auth-validate")
def validate_grafana_sso_token(
    x_opspilot_sso_token: str | None = Header(default=None),
):
    """供边缘 Nginx `auth_request` 调用，返回 Grafana 认证所需头信息。"""
    raw_token = str(x_opspilot_sso_token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="缺少 Grafana SSO Token")
    payload = _decode_grafana_sso_token(raw_token, GRAFANA_SSO_TYPE)
    identity = _identity_headers(
        username=str(payload.get("sub") or ""),
        display_name=str(payload.get("name") or payload.get("sub") or ""),
        email=str(payload.get("email") or ""),
        opspilot_role=str(payload.get("role") or ""),
    )
    return Response(
        status_code=204,
        headers={
            "X-OpsPilot-User": identity["X-WEBAUTH-USER"],
            "X-OpsPilot-Name": identity["X-WEBAUTH-NAME"],
            "X-OpsPilot-Email": identity.get("X-WEBAUTH-EMAIL", ""),
            "X-OpsPilot-Role": identity["X-WEBAUTH-ROLE"],
        },
    )


grafana_proxy_router = APIRouter(prefix=GRAFANA_PROXY_PREFIX)


def _is_public_grafana_path(grafana_path: str) -> bool:
    """判断是否是 Grafana 自己就允许匿名访问的静态资源路径。"""
    normalized = str(grafana_path or "").strip().strip("/").lower()
    if not normalized:
        return False
    if normalized in GRAFANA_PUBLIC_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in GRAFANA_PUBLIC_PATH_PREFIXES)


def _resolve_proxy_identity(db: Session, request: Request) -> tuple[User, str | None]:
    """解析本次请求的 Grafana 身份，解析不出来一律 401。

    返回 `(当前用户, 需要种给浏览器的会话 Cookie)`。

    身份来源：
    1. 地址栏里的 90 秒票据 —— 校验通过后顺手换一枚 8 小时会话 Cookie；
    2. 会话 Cookie —— 校验 `uid + token_version`，防止禁用账号/重置密码后继续放行。

    没有票据也没有 Cookie 时**必须** 401：否则匿名请求会被原样转发给 Grafana，
    本接口就真成了开放反代。
    """
    raw_ticket = str(request.query_params.get("opspilot_sso") or "").strip()
    if raw_ticket:
        payload = _decode_grafana_sso_token(raw_ticket, GRAFANA_SSO_TYPE)
        user = db.query(User).filter(User.username == str(payload.get("sub") or "")).first()
        if user is None or str(user.status or "") != "active":
            raise HTTPException(status_code=401, detail="Grafana 登录用户不存在或已禁用")
        return user, _create_grafana_session_token(user)

    raw_session = str(request.cookies.get(GRAFANA_SESSION_COOKIE) or "").strip()
    if raw_session:
        payload = _decode_grafana_sso_token(raw_session, GRAFANA_SESSION_TYPE)
        try:
            user_id = int(payload.get("uid"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail=_PROXY_SESSION_EXPIRED_DETAIL) from exc
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or str(user.status or "") != "active":
            raise HTTPException(status_code=401, detail=_PROXY_SESSION_EXPIRED_DETAIL)
        # token_version 允许为 0，不能用 `or` 兜底，否则永远对不上。
        try:
            session_version = int(payload.get("tv"))
        except (TypeError, ValueError):
            session_version = -1
        if int(user.token_version or 0) != session_version:
            raise HTTPException(status_code=401, detail=_PROXY_SESSION_EXPIRED_DETAIL)
        return user, None

    raise HTTPException(status_code=401, detail="Grafana 入口已失效，请从 OpsPilot 监控页面重新进入")


@grafana_proxy_router.api_route(
    "/{grafana_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_grafana(
    grafana_path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """把同源 `/grafana/*` 请求反代到 Grafana，并按请求注入身份头。

    放行条件（满足其一）：
    1. 静态资源路径 —— Grafana 本身就允许匿名取，放行且不注入身份；
    2. 查询串带 `opspilot_sso` 短期票据 —— 校验通过后写入 `X-WEBAUTH-*` 头，
       并给浏览器换一枚 8 小时会话 Cookie；
    3. 已带我们自己的会话 Cookie —— 同样重新注入身份头。
    其余请求一律 401，避免本接口被当成开放反代使用。
    """
    cfg = read_monitoring_config(db)
    upstream = _grafana_upstream_base(str(cfg.get("grafana_url") or ""))
    if not upstream:
        raise HTTPException(status_code=404, detail="未配置 Grafana 地址，无法进入 Grafana")

    normalized_path = str(grafana_path or "").strip().strip("/")

    # Grafana 的「退出登录」指向 /grafana/logout。我们没有 Grafana 自己的会话
    # 可退，清掉自己的 Cookie 后把人带回 OpsPilot 监控页——SSO 的入口本来就在那边。
    # 放在身份解析之前：退出登录不该要求会话仍然有效。
    if normalized_path.lower() == "logout":
        logout_response = RedirectResponse(url="/monitoring/metrics", status_code=302)
        logout_response.delete_cookie(GRAFANA_SESSION_COOKIE, path=GRAFANA_PROXY_PREFIX)
        logout_response.delete_cookie("grafana_session", path=GRAFANA_SUB_PATH)
        return logout_response

    if _is_public_grafana_path(normalized_path):
        user: User | None = None
        session_cookie: str | None = None
    else:
        user, session_cookie = _resolve_proxy_identity(db, request)
    # 转发前剥掉一次性票据，避免 Token 泄漏到 Grafana 地址栏与访问日志。
    # query_params 取出来的已是解码值，必须重新 urlencode：Grafana Explore 的
    # left={"datasource":...} 带花引号与 &，原样拼接会把查询串结构打乱。
    forwarded_query = urlencode(
        [(key, value) for key, value in request.query_params.multi_items() if key != "opspilot_sso"]
    )
    target_url = f"{upstream}/{grafana_path.lstrip('/')}"
    if forwarded_query:
        target_url = f"{target_url}?{forwarded_query}"

    body = await request.body()
    timeout_seconds = int(cfg.get("timeout_seconds") or 15)
    try:
        upstream_response = requests.request(
            request.method,
            target_url,
            headers=_build_proxy_headers(request, user),
            data=body if body else None,
            stream=True,
            timeout=(5, max(timeout_seconds, 5)),
            verify=bool(cfg.get("verify_tls", True)),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Grafana 连接失败：{exc}") from exc

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_RESPONSE_HEADERS
    }
    location = upstream_response.headers.get("location")
    if location:
        response_headers["location"] = _same_origin_location(location, str(request.headers.get("host") or ""))

    def _iter_upstream():
        try:
            for chunk in upstream_response.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream_response.close()

    response = StreamingResponse(
        _iter_upstream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
    if session_cookie:
        # 只在换票那一次种 Cookie；SameSite=Lax 保证 window.open 的顶级导航也带得上。
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme)
        response.set_cookie(
            GRAFANA_SESSION_COOKIE,
            session_cookie,
            max_age=GRAFANA_SESSION_EXPIRE_SECONDS,
            path=GRAFANA_PROXY_PREFIX,
            httponly=True,
            samesite="lax",
            secure=forwarded_proto.lower() == "https",
        )
    return response
