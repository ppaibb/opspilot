"""
认证与授权系统 (已迁移至强关系型数据库与环境密钥)
遵循 Google Python Style Guide
"""
from datetime import datetime, timedelta
from typing import Optional
from jose import jwt
from fastapi import APIRouter, HTTPException, Request, status, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.config import settings
from app.core.login_rate_limit import LoginRateLimiter
from app.core.passwords import verify_password
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
from app.core.security import get_current_user, normalize_user_status

router = APIRouter()

_LOGIN_RATE_LIMITER = LoginRateLimiter(
    max_attempts=settings.LOGIN_MAX_FAILED_ATTEMPTS,
    window_seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS,
    lock_seconds=settings.LOGIN_LOCK_SECONDS,
)

# ===== 数据模型 =====
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """构建安全的 JWT 授权凭证"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def _request_source_ip(request: Request) -> str:
    """提取登录来源 IP，优先使用反向代理透传的首个来源地址。"""
    headers = getattr(request, "headers", {}) or {}
    forwarded_for = str(headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    client = getattr(request, "client", None)
    return client.host if client else "-"


def run_login_flow(req: LoginRequest, *, request: Request, db: Session):
    """执行登录主流程，拆出同步函数便于安全契约测试。"""
    source_ip = _request_source_ip(request)
    rate_key = _LOGIN_RATE_LIMITER.build_key(req.username, source_ip)
    allowed, lock_reason = _LOGIN_RATE_LIMITER.check_allowed(rate_key)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=lock_reason)

    # 从数据库检索用户
    user = db.query(User).filter(User.username == req.username).first()
    
    if not user or not verify_password(req.password, user.hashed_password):
        _LOGIN_RATE_LIMITER.record_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if normalize_user_status(getattr(user, "status", "active")) != "active":
        _LOGIN_RATE_LIMITER.record_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="当前账号已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 密码正确，生成 Token 并返回
    _LOGIN_RATE_LIMITER.record_success(rate_key)
    # ver = 会话版本号：与 users.token_version 配套，重置密码/禁用后旧 token 全部失效。
    token = create_access_token(
        data={"sub": req.username, "role": user.role, "ver": int(getattr(user, "token_version", 0) or 0)}
    )
    user.last_login_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "department": user.department,
            "status": normalize_user_status(getattr(user, "status", "active")),
            "permissions": resolve_effective_permissions(user),
        }
    }


# ===== 登录接口 =====
@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    接收用户凭证并提供标准的 Bearer Token。
    """
    return run_login_flow(req, request=request, db=db)

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

    risk_level = max_risk_level(
        [get_risk_level(perm, action) for perm, action in pairs]
    )
    ticket = create_risk_ticket(
        db,
        user_id=current_user.id,
        permission_key=permission_key,
        action_id=action_id,
        short_key=short_key,
        resource_id=req.resource_id,
    )
    return {"ticket": ticket.id, "expires_at": ticket.expires_at, "risk_level": risk_level}
