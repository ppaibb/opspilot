"""应用中心权限与角色范围判断。"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.core.json_fields import normalize_json_list
from app.core.rbac import list_role_templates, normalize_role_name, resolve_effective_permissions
from app.models.application import Application
from app.models.user import User

APP_ACTION_PERMISSION_MAP = {
    "deploy": "action.apps.deploy",
    "control": "action.apps.control",
    "config": "action.apps.config.manage",
}
APP_ACTION_ROLE_FIELD_MAP = {
    "view": "allowed_roles",
    "deploy": "deploy_roles",
    "control": "control_roles",
    "config": "config_roles",
}


def _load_json_list(raw_value: str | None) -> list[str]:
    """安全解析 JSON 字符串数组，兼容 ORM 已经返回 list 的新字段。"""
    return normalize_json_list(raw_value)


def _can_manage_apps(user: User) -> bool:
    """判断用户是否拥有应用目录管理权限。"""
    return bool(resolve_effective_permissions(user).get("action.apps.manage"))


def _can_bypass_application_scope(user: User) -> bool:
    """判断用户是否可绕过单个应用的权限组范围。

    应用范围用于把同一套全局动作权限继续收窄到具体应用。
    因此只有系统管理员能全局兜底，普通目录维护者仍需落在应用可见范围内。
    """
    return normalize_role_name(str(user.role or "")) == "管理员"


def _validate_roles(roles: list[str] | None) -> list[str]:
    """校验应用可见/动作权限组是否存在于系统角色模板中。"""
    valid_roles = {item["role"] for item in list_role_templates()}
    normalized = list(dict.fromkeys([str(item or "").strip() for item in (roles or []) if str(item or "").strip()]))
    unknown = [role for role in normalized if role not in valid_roles]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知权限组: {', '.join(unknown)}")
    return normalized


def _application_action_role_allowed(app: Any, role: str, action: str) -> bool:
    """判断用户角色是否落在当前应用的动作范围内。"""
    action_name = str(action or "view").strip()
    field_name = APP_ACTION_ROLE_FIELD_MAP.get(action_name, "allowed_roles")
    roles = _load_json_list(getattr(app, field_name, "") or "")
    if action_name != "view" and not roles:
        roles = _load_json_list(getattr(app, "allowed_roles", "") or "")
    if not roles:
        return True
    return str(role or "").strip() in roles


def _application_action_allowed(app: Application, user: User, action: str) -> bool:
    """判断用户是否允许对当前应用执行指定动作。"""
    if _can_bypass_application_scope(user):
        return True
    permissions = resolve_effective_permissions(user)
    permission_key = APP_ACTION_PERMISSION_MAP.get(str(action or "").strip())
    if permission_key and not permissions.get(permission_key):
        return False
    return _application_action_role_allowed(app, str(user.role or "").strip(), action)


def _ensure_application_action(app: Application, user: User, action: str) -> None:
    """应用级动作权限检查，不通过时统一返回 403。"""
    if not _application_action_allowed(app, user, action):
        raise HTTPException(status_code=403, detail="当前权限组无权操作该应用")


def _application_capabilities(app: Application, user: User) -> dict[str, bool]:
    """返回前端可消费的应用动作能力。"""
    return {
        "can_manage": _can_manage_apps(user),
        "can_deploy": _application_action_allowed(app, user, "deploy"),
        "can_control": _application_action_allowed(app, user, "control"),
        "can_config": _application_action_allowed(app, user, "config"),
    }


def _application_visible_to_user(app: Application, user: User) -> bool:
    """判断应用目录项是否对当前用户可见。"""
    if _can_bypass_application_scope(user):
        return True
    allowed_roles = _load_json_list(app.allowed_roles)
    if not allowed_roles:
        return True
    return str(user.role or "").strip() in allowed_roles
