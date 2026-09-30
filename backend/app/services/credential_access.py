"""凭证授权组访问控制服务。

设计目标：
1. 统一解析 credentials.allowed_roles；
2. 为所有“使用凭证”的后端接口提供二次校验；
3. 避免只靠前端筛选导致普通用户绕过授权组直接传 cred_id。
"""
from __future__ import annotations

import json
from typing import Iterable

from fastapi import HTTPException

from app.core.rbac import resolve_effective_permissions
from app.models.credential import Credential
from app.models.user import User


def parse_allowed_roles(raw_value: str | None) -> list[str]:
    """解析凭证授权权限组。

    空值表示所有权限组可用；非法历史数据按空处理，避免影响旧凭证可用性。
    """
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def user_can_manage_credentials(user: User | None) -> bool:
    """判断用户是否拥有凭证管理权限。"""
    if not user:
        return False
    return bool(resolve_effective_permissions(user).get("action.assets.credentials.manage"))


def credential_visible_to_user(credential: Credential, user: User | None) -> bool:
    """判断某个凭证对当前用户是否可见/可用。"""
    if user_can_manage_credentials(user):
        return True
    allowed_roles = parse_allowed_roles(getattr(credential, "allowed_roles", "") or "")
    return not allowed_roles or str(getattr(user, "role", "") or "") in allowed_roles


def filter_credentials_for_user(
    credentials: Iterable[Credential],
    user: User | None,
) -> list[Credential]:
    """过滤出当前用户可用的凭证列表。"""
    return [item for item in credentials if credential_visible_to_user(item, user)]


def ensure_credential_access(
    credential: Credential | None,
    user: User | None,
    *,
    not_found_message: str = "凭证不存在",
    denied_message: str = "当前账号无权使用该凭证",
) -> Credential:
    """校验凭证存在且当前用户可用。"""
    if not credential:
        raise HTTPException(status_code=404, detail=not_found_message)
    if not credential_visible_to_user(credential, user):
        raise HTTPException(status_code=403, detail=denied_message)
    return credential
