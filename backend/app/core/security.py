"""
认证安全依赖：负责解析并校验 Bearer Token，以及高危操作票据的原子消费。
"""
import uuid
from datetime import timedelta

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rbac import (
    ACTION_RISK_LEVELS,
    PERMISSION_DEFAULTS,
    normalize_role_name,
    resolve_effective_permissions,
)
from app.db.database import get_db
from app.models.risk_ticket import RiskAckTicket, risk_ticket_now
from app.models.user import User
from app.services.application_time import now_dt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# 内部一次性票据的 JWT scope 标记（通知 SSE 票据）：带该标记的短时 JWT 只能
# 用于对应通道，不能当长期 access token 用——get_current_user 见到即拒绝，
# 与 _verify_sse_ticket 拒绝无 scope 的 access token 形成双向隔离。
SSE_TICKET_SCOPE = "notifications-sse"


def normalize_user_status(raw_status: str | None) -> str:
    """归一化用户状态。"""
    value = str(raw_status or "").strip().lower()
    return "disabled" if value == "disabled" else "active"


def _parse_token_version(raw: object) -> int:
    """把 JWT 里的 ver claim 安全解析为整数。

    历史签发的 token 没有 ver claim，按 0 处理（与列默认值一致，
    保证升级后旧 token 仍可用）；解析不出数字的脏值按
    "永远不可能等于当前版本"处理（-1），宁可让人重新登录也不能放行。
    """
    if raw is None:
        return 0
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


def _check_token_version(payload: dict, user: User) -> None:
    """校验 JWT 的 ver claim 与用户当前 token_version 是否一致。

    重置密码 / 禁用账号会使 token_version 递增，全部已签发 token 随即失效。
    供 get_current_user 与其他需要手动解码 JWT 的验证路径（如 SSE 票据）复用。
    不一致时抛 401，提示用户重新登录。
    """
    token_ver = _parse_token_version(payload.get("ver") if payload else None)
    current_ver = int(getattr(user, "token_version", 0) or 0)
    if token_ver != current_ver:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    从 Authorization Bearer Token 中提取当前用户。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token 无效或已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise credentials_exception
        # 一次性票据反向隔离：SSE 票据即使未过期也不得冒充 access token。
        if str(payload.get("scope") or "") == SSE_TICKET_SCOPE:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise credentials_exception
    if normalize_user_status(getattr(user, "status", "active")) != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="当前账号已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 会话版本校验：版本号对不上说明密码已重置 / 账号曾被禁用，强制下线。
    _check_token_version(payload, user)

    return user


def require_roles(*allowed_roles: str):
    """
    基于当前用户角色进行权限校验。
    """

    def _role_guard(current_user: User = Depends(get_current_user)) -> User:
        current_role = normalize_role_name(current_user.role)
        normalized_allowed_roles = {normalize_role_name(role) for role in allowed_roles}
        if current_role not in normalized_allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前账号权限不足",
            )
        return current_user

    return _role_guard


def require_permissions(*required_permissions: str, match: str = "all"):
    """
    基于当前用户有效权限进行校验。

    Args:
        *required_permissions: 需要满足的权限键列表。
        match: 匹配模式。`all` 表示全部满足，`any` 表示任意满足。
    """

    if not required_permissions:
        raise ValueError("require_permissions 至少需要一个权限键")
    if match not in {"all", "any"}:
        raise ValueError("require_permissions 的 match 仅支持 all 或 any")

    unknown_keys = [
        key for key in required_permissions if key not in PERMISSION_DEFAULTS
    ]
    if unknown_keys:
        raise ValueError(f"未知权限键: {', '.join(unknown_keys)}")

    def _permission_guard(current_user: User = Depends(get_current_user)) -> User:
        permission_map = resolve_effective_permissions(current_user)
        if match == "all":
            allowed = all(permission_map.get(key, False) for key in required_permissions)
        else:
            allowed = any(permission_map.get(key, False) for key in required_permissions)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前账号无权访问该功能",
            )
        return current_user

    return _permission_guard


def consume_risk_ticket(
    request: Request,
    db: Session,
    *,
    permission_key: str,
    action_id: str,
    user: User,
) -> None:
    """原子消费一张高危操作二次确认票据；不满足则一律 403。

    从 `X-Risk-Ticket` 请求头取票据，按「用户 + 权限键 + 动作 + 未消费 + 未过期」
    单条 UPDATE 抢行（防重放）。供 `require_risk_ack` 依赖与"函数体内动态校验"
    两种场景复用——例如用户状态路由只在禁用时才要求票据，启用直接放行，
    这种按入参分支的校验没法用固定的路由依赖表达。

    Args:
        request: FastAPI 请求（只读 `x-risk-ticket` 请求头）。
        db: 数据库会话（消费动作需要落库）。
        permission_key: 路由绑定的权限键，如 "action.settings.users.manage"。
        action_id: 路由绑定的动作标识，如 "disable"。
        user: 当前用户，票据必须与该用户绑定。

    Raises:
        HTTPException: 缺票、票据无效/过期/已消费、权限键或动作不匹配（均 403）。
    """
    ticket_id = str(request.headers.get("x-risk-ticket") or "").strip()
    if not ticket_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少风险确认票据，请先进行二次确认",
        )

    # 原子消费：单条 UPDATE 只在票据完全匹配时抢到行，rowcount==1 才算通过。
    # - 全键票据：permission_key 与 action_id 必须精确匹配当前路由；
    # - 短键票据：action_id 匹配且票据里的短前缀是当前权限键的任一别名
    #   （别名生成逻辑与 GET /risk-levels 完全一致）。
    now_str = risk_ticket_now()
    alias_keys = permission_key_aliases(permission_key)
    matched = (
        db.query(RiskAckTicket)
        .filter(
            RiskAckTicket.id == ticket_id,
            RiskAckTicket.user_id == user.id,
            RiskAckTicket.consumed_at.is_(None),
            RiskAckTicket.expires_at > now_str,
            or_(
                (
                    (RiskAckTicket.short_key.is_(False))
                    & (RiskAckTicket.permission_key == permission_key)
                    & (RiskAckTicket.action_id == action_id)
                ),
                (
                    (RiskAckTicket.short_key.is_(True))
                    & (RiskAckTicket.action_id == action_id)
                    & (RiskAckTicket.permission_key.in_(alias_keys))
                ),
            ),
        )
        .update({"consumed_at": risk_ticket_now()}, synchronize_session=False)
    )
    db.commit()
    if matched != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="风险确认票据无效、已过期或已被使用，请重新进行二次确认",
        )


def require_risk_ack(permission_key: str, action_id: str):
    """
    校验高危操作二次确认票据（强制执行，不再直通放行）。

    流程：前端先调 `POST /api/auth/risk-challenge` 换取一次性票据，
    再把票据放进 `X-Risk-Ticket` 请求头发起业务请求。票据与当前用户绑定、
    120 秒过期、数据库层原子消费（防重放），且 permission/action 必须与
    当前路由一致。

    Args:
        permission_key: 路由绑定的权限键，如 "action.assets.clusters.manage"。
        action_id: 路由绑定的动作标识，如 "delete"。
    """

    def _guard(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        consume_risk_ticket(
            request,
            db,
            permission_key=permission_key,
            action_id=action_id,
            user=current_user,
        )
        return current_user

    # 元数据标注：把 (权限键, 动作) 钉在守卫函数对象上，供"守卫注册表"
    # 契约测试与审计反射核对路由实际绑定的 action 字符串（闭包参数本身不可见）。
    _guard.risk_permission_key = permission_key
    _guard.risk_action_id = action_id
    return _guard


# ---------------------------------------------------------------------------
# 高危票据：权限键别名与风险等级工具（auth /risk-challenge 与消费端共用）
# ---------------------------------------------------------------------------

# 风险等级严重度排序，短键多匹配时取最高级。
_RISK_SEVERITY = {"low": 0, "medium": 1, "high": 2}


def permission_key_aliases(permission_key: str) -> set[str]:
    """生成权限键的全部短键别名，与 GET /risk-levels 的别名逻辑保持一致。

    例："action.assets.clusters.manage" →
    {"action.assets.clusters.manage", "manage", "clusters", "clusters.manage"}；
    三段键（action.wireguard.manage）与 finance 这类对照表短键取首段：
    "action.finance.accounts.manage" → 追加 {"finance", "accounts", …}，
    保证 "wireguard:delete" / "apps:delete" / "finance:delete" 能换出票。
    """
    key = str(permission_key or "").strip()
    parts = key.split(".")
    aliases = {key}
    if parts:
        aliases.add(parts[-1])
    if len(parts) >= 3:
        # 首段（wireguard/apps/finance/db…）：三段键的第三段只是重复的
        # "manage"，没有命名空间信息；四段键补首段可让对照表短键统一走
        # 「业务域:动作」形态。消费端与签发端共用本函数，匹配天然一致。
        aliases.add(parts[1])
        aliases.add(parts[2])
        if len(parts) >= 4:
            aliases.add(f"{parts[2]}.{parts[3]}")
    return {alias for alias in aliases if alias}


def _perm_matches_action_key(perm: str, prefix: str, action: str) -> bool:
    """判断 (perm, action) 会不会在 /risk-levels 里生成 prefix:action 这个短键。"""
    return f"{prefix}:{action}" in {
        f"{alias}:{action}" for alias in permission_key_aliases(perm)
    }


def resolve_action_key_pairs(action_key: str) -> list[tuple[str, str]]:
    """把前端短键（如 "clusters:delete"）反解析为匹配的 (perm, action) 全键对。

    反向套用 get_risk_levels 的别名生成逻辑：先在高危动作注册表
    （ACTION_RISK_LEVELS）里找；找不到时退而求其次在全部已注册权限键里找
    ——部分路由（节点 drain、标签编辑等）挂了风险确认但动作未注册风险等级，
    这些动作也必须能换取票据，否则对应路由会被永久 403。
    """
    text = str(action_key or "").strip()
    if ":" not in text:
        return []
    prefix, _, action = text.rpartition(":")
    prefix = prefix.strip()
    action = action.strip()
    if not prefix or not action:
        return []

    registered = [
        (perm, registered_action)
        for perm, registered_action in ACTION_RISK_LEVELS
        if registered_action == action and _perm_matches_action_key(perm, prefix, action)
    ]
    if registered:
        return registered
    # 动作未注册风险等级（get_risk_level 视为 low），但权限键真实存在。
    return [
        (perm, action)
        for perm in PERMISSION_DEFAULTS
        if _perm_matches_action_key(perm, prefix, action)
    ]


def max_risk_level(levels: list[str]) -> str:
    """取一批风险等级里的最高级；空列表视为 low。"""
    best = "low"
    for level in levels:
        if _RISK_SEVERITY.get(level, 0) > _RISK_SEVERITY.get(best, 0):
            best = level
    return best


def create_risk_ticket(
    db: Session,
    *,
    user_id: int,
    permission_key: str,
    action_id: str,
    short_key: bool,
    resource_id: str | None = None,
    ttl_seconds: int = 120,
) -> RiskAckTicket:
    """签发一张一次性风险确认票据，并顺带清理已过期的历史票据。"""
    now_str = risk_ticket_now()
    # 清理过期票据：票据体量小，挑战时顺带打扫即可，无需后台任务。
    db.query(RiskAckTicket).filter(RiskAckTicket.expires_at <= now_str).delete(
        synchronize_session=False
    )
    expires = (now_dt() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    ticket = RiskAckTicket(
        id=str(uuid.uuid4()),
        user_id=user_id,
        permission_key=permission_key,
        action_id=action_id,
        resource_id=(str(resource_id).strip() or None) if resource_id else None,
        short_key=short_key,
        created_at=now_str,
        expires_at=expires,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket
