"""
认证与授权系统 (已迁移至强关系型数据库与环境密钥)
遵循 Google Python Style Guide
"""
from datetime import datetime
import ipaddress
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, status, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.config import settings
from app.core.login_rate_limit import DatabaseLoginRateLimiter
from app.core.passwords import hash_password, verify_password
from app.core.tokens import create_access_token
from app.core.rbac import (
    ACTION_RISK_LEVELS,
    PERMISSION_DEFAULTS,
    get_risk_level,
    resolve_effective_permissions,
)
from app.core.security import (
    create_risk_ticket,
    max_risk_level,
    resolve_action_key_pairs,
)
from app.db.database import get_db
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.core.security import get_current_user, normalize_user_status
from app.services.refresh_tokens import (
    RefreshTokenRejected,
    issue_refresh_token,
    refresh_token_digest,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter()

_LOGIN_RATE_LIMITER = DatabaseLoginRateLimiter(
    max_attempts=settings.LOGIN_MAX_FAILED_ATTEMPTS,
    window_seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS,
    lock_seconds=settings.LOGIN_LOCK_SECONDS,
)

# 用户不存在时仍执行同等成本的 bcrypt 校验，降低用户名枚举的时序侧信道。
DUMMY_PASSWORD_HASH = hash_password("opspilot-dummy-password-never-used")

# ===== 数据模型 =====
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str

def _request_source_ip(request: Request) -> str:
    """仅在直连来源属于可信代理网段时读取 X-Forwarded-For。"""
    headers = getattr(request, "headers", {}) or {}
    client = getattr(request, "client", None)
    direct_ip = str(client.host if client else "-").strip()
    try:
        address = ipaddress.ip_address(direct_ip)
        trusted = any(
            address in ipaddress.ip_network(item.strip(), strict=False)
            for item in settings.TRUSTED_PROXY_CIDRS.split(",")
            if item.strip()
        )
    except ValueError:
        trusted = False
    if trusted:
        forwarded_for = str(headers.get("x-forwarded-for") or "").strip()
        candidate = forwarded_for.split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return direct_ip or "-"


def _rate_check(key: str, db: Session) -> tuple[bool, str]:
    """兼容旧测试注入的内存限流器，生产默认使用数据库限流器。"""
    try:
        return _LOGIN_RATE_LIMITER.check_allowed(key, db=db)
    except TypeError:
        return _LOGIN_RATE_LIMITER.check_allowed(key)


def _rate_failure(key: str, db: Session) -> None:
    try:
        _LOGIN_RATE_LIMITER.record_failure(key, db=db)
    except TypeError:
        _LOGIN_RATE_LIMITER.record_failure(key)


def _rate_success(key: str, db: Session) -> None:
    try:
        _LOGIN_RATE_LIMITER.record_success(key, db=db)
    except TypeError:
        _LOGIN_RATE_LIMITER.record_success(key)


def _user_payload(user: User) -> dict:
    """构造登录/刷新共用的非敏感用户响应。"""
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "department": user.department,
        "status": normalize_user_status(getattr(user, "status", "active")),
        "permissions": resolve_effective_permissions(user),
    }


def run_login_flow(req: LoginRequest, *, request: Request, db: Session):
    """执行登录主流程，拆出同步函数便于安全契约测试。"""
    source_ip = _request_source_ip(request)
    rate_key = _LOGIN_RATE_LIMITER.build_key(req.username, source_ip)
    allowed, lock_reason = _rate_check(rate_key, db)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=lock_reason)

    # 从数据库检索用户
    user = db.query(User).filter(User.username == req.username).first()
    
    password_ok = verify_password(
        req.password,
        user.hashed_password if user else DUMMY_PASSWORD_HASH,
    )
    if not user or not password_ok:
        _rate_failure(rate_key, db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if normalize_user_status(getattr(user, "status", "active")) != "active":
        _rate_failure(rate_key, db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="当前账号已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 密码正确，生成 Token 并返回
    _rate_success(rate_key, db)
    # ver = 会话版本号：与 users.token_version 配套，重置密码/禁用后旧 token 全部失效。
    token = create_access_token(
        data={"sub": req.username, "role": user.role, "ver": int(getattr(user, "token_version", 0) or 0)}
    )
    refresh_token = issue_refresh_token(db, user)
    user.last_login_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.commit()
    return {
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


# ===== 登录接口 =====
@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    接收用户凭证并提供标准的 Bearer Token。
    """
    return run_login_flow(req, request=request, db=db)


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(req: RefreshRequest, db: Session = Depends(get_db)):
    """单次轮换 refresh token，并签发新的 access token。"""
    record = db.query(RefreshToken).filter(
        RefreshToken.token_hash == refresh_token_digest(req.refresh_token)
    ).first()
    user = db.query(User).filter(User.id == record.user_id).first() if record else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效")
    try:
        next_refresh = rotate_refresh_token(db, req.refresh_token, user)
    except RefreshTokenRejected as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    access = create_access_token(
        {"sub": user.username, "role": user.role, "ver": int(user.token_version or 0)}
    )
    return {
        "access_token": access,
        "refresh_token": next_refresh,
        "token_type": "bearer",
        "user": _user_payload(user),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_session(req: RefreshRequest, db: Session = Depends(get_db)):
    """撤销 refresh token 所属的完整会话族。"""
    revoke_refresh_token(db, req.refresh_token)
    return None

# ===== Token 验证接口（用于前端免密登录保护） =====
@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """
    返回当前 Token 对应的用户信息。
    """
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "valid": True,
        "name": current_user.name,
        "email": current_user.email,
        "department": current_user.department,
        "status": normalize_user_status(getattr(current_user, "status", "active")),
        "permissions": resolve_effective_permissions(current_user),
    }


# ===== 风险等级查询接口 =====
@router.get("/risk-levels")
async def get_risk_levels(current_user: User = Depends(get_current_user)):
    """
    返回所有已注册的高/中风险操作及其等级。

    前端在首次加载时调用，后续缓存。
    响应格式：{ "levels": { "credentials:export": "high", ... } }
    键为 "权限键后缀:动作标识" 的简写。
    """
    levels: dict[str, str] = {}
    for (perm, action), level in ACTION_RISK_LEVELS.items():
        # Original short key (buggy but kept for compatibility)
        short_key = f"{perm.split('.')[-1]}:{action}"
        levels[short_key] = level

        # Full key
        levels[f"{perm}:{action}"] = level

        # Frontend suffix matching (e.g. 'domains.manage', 'certificates', 'vms.power')
        parts = perm.split('.')
        if len(parts) >= 3:
            # 首段别名（与 permission_key_aliases 保持一致）：
            # 'wireguard:delete' / 'finance:create' / 'apps:delete' 这类
            # 对照表短键也能查到风险等级。
            levels[f"{parts[1]}:{action}"] = level
            levels[f"{parts[2]}:{action}"] = level
            if len(parts) >= 4:
                levels[f"{parts[2]}.{parts[3]}:{action}"] = level

    return {"levels": levels}


# ===== 高危操作二次确认票据 =====
class RiskChallengeRequest(BaseModel):
    """换取 X-Risk-Ticket 票据的请求体（二选一）。

    - action_key：前端短键（GET /risk-levels 返回的键），如 "clusters:delete"；
    - permission_key + action_id：全键精确指定，如
      ("action.assets.clusters.manage", "delete")。
    """

    permission_key: Optional[str] = None
    action_id: Optional[str] = None
    action_key: Optional[str] = None
    resource_id: Optional[str] = None
    http_method: Optional[str] = None
    route_template: Optional[str] = None
    request_digest: Optional[str] = None


@router.post("/risk-challenge")
async def risk_challenge(
    req: RiskChallengeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    为当前用户签发一张一次性的高危操作确认票据（TTL 120 秒）。

    票据与用户、权限键、动作标识绑定，业务请求需放在 `X-Risk-Ticket`
    请求头中，由 require_risk_ack 在数据库层原子消费（防重放）。
    返回 {"ticket", "expires_at", "risk_level"}；短键多匹配时 risk_level
    取最高级。
    """
    pairs: list[tuple[str, str]] = []
    short_key = False

    action_key = (req.action_key or "").strip()
    if action_key:
        pairs = resolve_action_key_pairs(action_key)
        if not pairs:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"无法识别的风险动作: {action_key}",
            )
        short_key = True
        permission_key = action_key.rpartition(":")[0].strip()
        action_id = action_key.rpartition(":")[2].strip()
    else:
        permission_key = (req.permission_key or "").strip()
        action_id = (req.action_id or "").strip()
        if not permission_key or not action_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="必须提供 action_key，或同时提供 permission_key 与 action_id",
            )
        if permission_key not in PERMISSION_DEFAULTS:
            # 权限键必须真实存在于 RBAC 目录，防止给任意字符串签发票据。
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"未注册的权限键: {permission_key}",
            )
        pairs = [(permission_key, action_id)]

    # 风险确认票据只能为调用者真实拥有的权限签发。票据是二次确认凭据，
    # 不能被当作绕过 RBAC 的授权凭据；短键可能匹配多组权限，因此保留
    # 至少一组已授权匹配，消费端还会针对目标路由的全键再次校验。
    permission_map = resolve_effective_permissions(current_user)
    authorized_pairs = [
        (permission, action)
        for permission, action in pairs
        if permission_map.get(permission, False)
    ]
    if not authorized_pairs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前账号无权确认该高危操作",
        )
    if short_key and len(authorized_pairs) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="风险动作短键解析不唯一，请使用完整权限键确认",
        )
    if short_key:
        # 兼容前端短键输入，但数据库只保存唯一规范化全键，消费端不再做
        # 宽泛别名匹配，避免 assets:delete 一类短键跨资源域复用。
        permission_key, action_id = authorized_pairs[0]
        short_key = False

    risk_level = max_risk_level(
        [get_risk_level(perm, action) for perm, action in authorized_pairs]
    )
    ticket = create_risk_ticket(
        db,
        user_id=current_user.id,
        permission_key=permission_key,
        action_id=action_id,
        short_key=short_key,
        resource_id=req.resource_id,
        http_method=req.http_method,
        route_template=req.route_template,
        request_digest=req.request_digest,
    )
    return {"ticket": ticket.id, "expires_at": ticket.expires_at, "risk_level": risk_level}
