"""
Grafana 免密跳转与身份校验接口。

架构说明：
1. 前端先调用本文件中的受保护接口，获取一次性 Grafana 跳转链接；
2. 浏览器跳到监控节点上的 `/grafana/login?opspilot_sso=...`；
3. 监控节点 Nginx 通过 `auth_request` 调回本文件的校验接口；
4. 校验接口返回 `X-OpsPilot-User / Email / Name` 等响应头；
5. Nginx 再把这些头转成 Grafana `auth.proxy` 需要的 `X-WEBAUTH-*` 头。

安全审计：
1. 不把 Grafana 账号密码下发给前端；
2. SSO Token 使用 OpsPilot SECRET_KEY 签名，默认有效期极短；
3. 校验接口只接受短期 Token，不依赖浏览器 Bearer Token，便于 Nginx 子请求调用；
4. 用户真实权限仍留在 OpsPilot，本文件只负责 Grafana Viewer 级别的身份桥接。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from urllib.parse import quote, urlparse, urlunparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.monitoring import read_monitoring_config
from app.core.config import settings
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.user import User

router = APIRouter()

GRAFANA_SSO_AUDIENCE = "grafana-sso"
GRAFANA_SSO_EXPIRE_SECONDS = 90
INTERNAL_GRAFANA_HOSTS = {"grafana", "opspilot-grafana"}


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


def _create_grafana_sso_token(user: User, expire_at: datetime) -> str:
    """创建短期 Grafana SSO Token。"""
    payload = {
        "sub": str(user.username or ""),
        "grafana_login": _build_grafana_login(str(user.username or "")),
        "name": str(user.name or user.username or ""),
        "email": str(user.email or ""),
        "role": str(user.role or ""),
        "aud": GRAFANA_SSO_AUDIENCE,
        "type": "grafana-sso",
        "exp": expire_at,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decode_grafana_sso_token(raw_token: str) -> dict:
    """解码并校验 Grafana SSO Token。"""
    try:
        payload = jwt.decode(
            raw_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=GRAFANA_SSO_AUDIENCE,
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Grafana SSO Token 非法或已过期") from exc
    if payload.get("type") != "grafana-sso":
        raise HTTPException(status_code=401, detail="Grafana SSO Token 类型非法")
    return payload


def _browser_reachable_grafana_url(raw_url: str, request: Request) -> str:
    """
    生成浏览器可访问的 Grafana 根地址。

    部署后系统配置里可能保存的是 Docker 内网地址，例如
    `http://opspilot-grafana:3000/grafana`。这个地址后端容器能访问，
    但浏览器无法解析。遇到内网容器名时，统一回退到当前 OpsPilot
    外部访问域名下的 `/grafana` 前缀；实际反代由部署层负责。
    """
    value = str(raw_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.hostname not in INTERNAL_GRAFANA_HOSTS:
        return value
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or str(request.headers.get("host") or "").strip()
    scheme = forwarded_proto or request.url.scheme
    if not host:
        return value
    return urlunparse((scheme, host, "/grafana", "", "", "")).rstrip("/")


@router.get("/api/monitoring/grafana/sso-link", response_model=GrafanaSSOLinkResponse)
def get_grafana_sso_link(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """生成 Grafana 免密登录链接。"""
    cfg = read_monitoring_config(db)
    grafana_url = _browser_reachable_grafana_url(str(cfg.get("grafana_url") or ""), request)
    if not grafana_url:
        raise HTTPException(
            status_code=400,
            detail="请先在“系统设置 - 系统管理 - 监控连接”中填写 Grafana 地址",
        )
    # 这里单独计算一次过期时间，避免前端展示时间与 Token 实际时间出现明显偏差。
    expire_at = datetime.utcnow() + timedelta(seconds=GRAFANA_SSO_EXPIRE_SECONDS)
    token = _create_grafana_sso_token(current_user, expire_at)
    login_url = f"{grafana_url}/login?opspilot_sso={quote(token, safe='')}"
    return {
        "url": login_url,
        "expires_at": expire_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/monitoring/grafana/auth-validate")
def validate_grafana_sso_token(
    x_opspilot_sso_token: str | None = Header(default=None),
):
    """供监控节点 Nginx `auth_request` 调用，返回 Grafana 认证所需头信息。"""
    raw_token = str(x_opspilot_sso_token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="缺少 Grafana SSO Token")
    payload = _decode_grafana_sso_token(raw_token)
    username = _to_header_safe_value(
        str(payload.get("grafana_login") or ""),
        fallback_value=_build_grafana_login(str(payload.get("sub") or "")),
    )
    display_name = _to_header_safe_value(
        str(payload.get("name") or ""),
        fallback_value=str(payload.get("sub") or ""),
    )
    email = _to_header_safe_value(
        str(payload.get("email") or ""),
        fallback_value="",
    )
    role = "Admin" if str(payload.get("role") or "").strip().lower() == "admin" else "Viewer"
    return Response(
        status_code=204,
        headers={
            "X-OpsPilot-User": username,
            "X-OpsPilot-Name": display_name,
            "X-OpsPilot-Email": email,
            "X-OpsPilot-Role": role,
        },
    )
