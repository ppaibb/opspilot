"""应用中心基础 payload 与 URL 校验。"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.json_fields import dump_json_list
from app.schemas.application import ApplicationPayload

from .permissions import _validate_roles

APP_STATUS_VALUES = {"active", "maintenance", "offline"}
APP_ENV_VALUES = {"prod", "test", "dev", "mixed"}
APP_ENTRY_TYPES = {"external", "internal", "grafana", "tool"}


def _dump_json_list(items: list[str] | None) -> str:
    """把字符串列表持久化为规范 JSON 字符串。"""
    return dump_json_list(items)


def _validate_url(raw_url: str, field_name: str, required: bool = False) -> str:
    """校验应用入口和快捷链接 URL，避免危险协议进入页面。"""
    url = (raw_url or "").strip()
    if not url:
        if required:
            raise HTTPException(status_code=400, detail=f"{field_name} 不能为空")
        return ""
    if "\x00" in url:
        raise HTTPException(status_code=400, detail=f"{field_name} 包含非法字符")
    if not (url.startswith("/") or url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail=f"{field_name} 仅支持 http(s) 或站内路径")
    if len(url) > 500:
        raise HTTPException(status_code=400, detail=f"{field_name} 不能超过 500 个字符")
    return url


def _validate_payload(payload: ApplicationPayload) -> dict[str, str]:
    """归一化并校验应用元数据。"""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="应用名称不能为空")
    status = (payload.status or "active").strip()
    environment = (payload.environment or "prod").strip()
    entry_type = (payload.entry_type or "external").strip()
    if status not in APP_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="应用状态仅支持 active/maintenance/offline")
    if environment not in APP_ENV_VALUES:
        raise HTTPException(status_code=400, detail="应用环境仅支持 prod/test/dev/mixed")
    if entry_type not in APP_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail="入口类型仅支持 external/internal/grafana/tool")
    return {
        "name": name,
        "description": (payload.description or "").strip()[:500],
        "category": (payload.category or "业务应用").strip()[:80] or "业务应用",
        "owner": (payload.owner or "").strip()[:80],
        "status": status,
        "environment": environment,
        "run_mode": "external",
        "entry_type": entry_type,
        "entry_url": _validate_url(payload.entry_url, "应用入口", required=False),
        "tags": _dump_json_list((payload.tags or [])[:12]),
        "allowed_roles": _dump_json_list(_validate_roles(payload.allowed_roles)),
        "deploy_roles": _dump_json_list(_validate_roles(payload.deploy_roles)),
        "control_roles": _dump_json_list(_validate_roles(payload.control_roles)),
        "config_roles": _dump_json_list(_validate_roles(payload.config_roles)),
        "remark": (payload.remark or "").strip()[:500],
    }
