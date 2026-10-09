"""
系统设置相关接口：用户管理与操作审计
"""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.timefmt import to_local_str
from app.models.user import User
from app.models.audit import AuditLog
from app.models.system_config import SystemConfig
from app.core.passwords import hash_password
from app.core.rbac import (
    PERMISSION_CATALOG,
    ROLE_TEMPLATE_CONFIG_KEY,
    build_permission_overrides,
    get_role_permissions,
    is_system_role_template,
    list_role_templates,
    normalize_permission_map,
    parse_permission_overrides,
    resolve_effective_permissions,
    resolve_effective_permissions_from_values,
    summarize_permission_labels,
)
from app.core.security import (
    consume_risk_ticket,
    get_current_user,
    normalize_user_status,
    require_permissions,
    require_risk_ack,
)
from app.core.crypto import encrypt_secret

router = APIRouter()


class ScheduledTaskConfigItem(BaseModel):
    enabled: bool
    interval_seconds: int


class ScheduledTaskConfigUpdate(BaseModel):
    tasks: dict[str, ScheduledTaskConfigItem]


@router.get("/scheduled-tasks")
def list_scheduled_tasks(
    db: Session = Depends(get_db),
    _permission=Depends(require_permissions("module.settings.system")),
):
    """返回任务定义、持久配置与当前后端进程运行状态。"""
    from app.core.scheduler_registry import read_configs, scheduler_manager

    configs, updated_at = read_configs(db)
    rows = scheduler_manager.snapshot()
    for row in rows:
        config = configs[row["key"]]
        row["enabled"] = config["enabled"]
        row["interval_seconds"] = config["interval_seconds"]
        row["config_updated_at"] = updated_at
    return {"tasks": rows, "updated_at": updated_at}


@router.put("/scheduled-tasks")
async def update_scheduled_tasks(
    payload: ScheduledTaskConfigUpdate,
    db: Session = Depends(get_db),
    _permission=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """保存任务启停/周期并立即协调当前进程中的 runner。"""
    from app.core.scheduler_registry import persist_configs, scheduler_manager, validate_configs

    raw = {key: value.model_dump() for key, value in payload.tasks.items()}
    try:
        configs = validate_configs(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated_at = persist_configs(db, configs)
    await scheduler_manager.apply(configs)
    rows = scheduler_manager.snapshot()
    for row in rows:
        config = configs[row["key"]]
        row["enabled"] = config["enabled"]
        row["interval_seconds"] = config["interval_seconds"]
        row["config_updated_at"] = updated_at
    return {"tasks": rows, "updated_at": updated_at}


# 登录页配置 GET 接口需要在未登录状态下访问，单独拆出公开路由。
public_router = APIRouter()

FEATURE_TOGGLE_CONFIG_KEY = "feature_toggles"
LOGIN_PAGE_CONFIG_KEY = "login_page_config"
DEFAULT_LOGIN_PAGE_CONFIG: dict[str, str | bool] = {
    "hero_title": "广阔天地\n大有作为",
    "hero_subtitle": "—— 数字化工作台 ——",
    "background_image_url": "",
    "sso_enabled": False,
    "sso_button_text": "企业 SSO 登录",
    "sso_hint": "支持 GitHub、GitLab、飞书、钉钉等 OAuth/OIDC 接入，完整回调链路后续启用。",
}
DEFAULT_FEATURE_TOGGLES: dict[str, bool] = {
    "group.assets": True,
    "item.assets.hosts": True,
    "item.assets.vms": True,
    "item.assets.domains": True,
    "item.assets.certificates": True,
    "item.assets.credentials": True,
    "group.ai": True,
    "item.ai.ops": True,
    "item.ai.skills": True,
    "item.ai.orchestration": True,
    "item.ai.llm": True,
    "group.apps": True,
    "item.apps.list": True,
    "item.apps.deployment": True,
    "item.apps.config": True,
    "group.monitoring": True,
    "item.monitoring.live": True,
    "item.monitoring.rules": True,
    "item.monitoring.logs": True,
    "item.monitoring.collector": True,
    "group.tools": True,
    "item.tools.terminal": True,
    "item.tools.scripts": True,
}


class UserResponse(BaseModel):
    id: int
    username: str
    name: str
    email: str
    role: str
    department: str
    status: str
    last_login: str
    mfa_enabled: bool
    permission_count: int
    access_summary: list[str]


class UserCreateRequest(BaseModel):
    username: str
    password: str
    name: str = ""
    email: str = ""
    role: str = "只读"
    department: str = ""
    status: str = "active"
    effective_permissions: dict[str, bool] | None = None


class UserStatusUpdate(BaseModel):
    status: str


class UserPasswordResetRequest(BaseModel):
    new_password: str


class PermissionCatalogItem(BaseModel):
    key: str
    group: str
    title: str
    description: str
    path: str


class RoleTemplateItem(BaseModel):
    role: str
    title: str
    description: str
    permissions: dict[str, bool]
    is_system: bool = False
    editable: bool = False
    deletable: bool = False


class RoleTemplateCreateRequest(BaseModel):
    role: str
    description: str = ""
    permissions: dict[str, bool]


class RoleTemplateUpdateRequest(BaseModel):
    description: str = ""
    permissions: dict[str, bool]


class RBACMetadataResponse(BaseModel):
    permissions: list[PermissionCatalogItem]
    role_templates: list[RoleTemplateItem]


class UserAccessResponse(BaseModel):
    user_id: int
    username: str
    name: str
    role: str
    email: str
    department: str
    role_permissions: dict[str, bool]
    permission_overrides: dict[str, bool]
    effective_permissions: dict[str, bool]


class UserAccessUpdate(BaseModel):
    role: str
    effective_permissions: dict[str, bool]


class AuditLogResponse(BaseModel):
    id: int
    time: str
    user: str
    action: str
    resource: str
    ip: str
    result: str
    risk_level: str = ""
    risk_reason: str = ""


class PVEConfigResponse(BaseModel):
    base_url: str
    token_id: str
    default_node: str
    verify_tls: bool
    has_secret: bool


class PVEConfigUpdate(BaseModel):
    base_url: str
    token_id: str
    token_secret: str = ""
    default_node: str = ""
    verify_tls: bool = True


class VMwareConfigResponse(BaseModel):
    base_url: str
    username: str
    default_datacenter: str
    verify_tls: bool
    has_secret: bool


class VMwareConfigUpdate(BaseModel):
    base_url: str
    username: str
    password: str = ""
    default_datacenter: str = ""
    verify_tls: bool = True


class LoginPageConfigResponse(BaseModel):
    hero_title: str
    hero_subtitle: str
    background_image_url: str
    sso_enabled: bool
    sso_button_text: str
    sso_hint: str
    updated_at: str = ""


class LoginPageConfigUpdate(BaseModel):
    hero_title: str = ""
    hero_subtitle: str = ""
    background_image_url: str = ""
    sso_enabled: bool = False
    sso_button_text: str = ""
    sso_hint: str = ""


class FeatureToggleResponse(BaseModel):
    toggles: dict[str, bool]
    updated_at: str


class FeatureToggleUpdate(BaseModel):
    toggles: dict[str, bool]


def _safe_load_json_dict(raw_value: str | None) -> dict:
    """
    安全解析配置 JSON，确保始终返回字典。

    说明：
    - 历史脏数据可能写入了 JSON null / 数组 / 字符串；
    - 系统设置读取逻辑依赖 dict.get(...)，因此这里统一做类型兜底；
    - 解析失败或类型不匹配时返回空字典，避免接口 500。
    """
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_custom_role_templates(db: Session) -> dict[str, dict]:
    """读取权限组持久化配置。

    说明：
    - 当前配置同时承载“自定义权限组”与“系统预设组覆盖项”；
    - 系统预设组的组名仍由代码定义，只允许覆盖说明和权限内容；
    - 删除系统预设组仍由后端显式禁止。
    """
    row = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == ROLE_TEMPLATE_CONFIG_KEY)
        .first()
    )
    payload = _safe_load_json_dict(row.config_value if row else "")
    result: dict[str, dict] = {}
    for role_key, meta in payload.items():
        if not isinstance(role_key, str):
            continue
        role_name = role_key.strip()
        if not role_name:
            continue
        if not isinstance(meta, dict):
            continue
        permissions = normalize_permission_map(meta.get("permissions") or {})
        result[role_name] = {
            "title": role_name,
            "description": str(meta.get("description") or ""),
            "permissions": permissions,
        }
    return result


def save_custom_role_templates(db: Session, templates: dict[str, dict]) -> None:
    """保存权限组持久化配置。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = json.dumps(templates, ensure_ascii=False)
    row = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == ROLE_TEMPLATE_CONFIG_KEY)
        .first()
    )
    if not row:
        row = SystemConfig(
            config_key=ROLE_TEMPLATE_CONFIG_KEY,
            config_value=content,
            updated_at=now_str,
        )
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()


def validate_user_password(raw_password: str) -> str:
    """校验用户口令复杂度。"""
    password = str(raw_password or "")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码长度不能少于 8 位")
    return password


def normalize_user_email(raw_email: str | None) -> str:
    """归一化邮箱。"""
    return str(raw_email or "").strip().lower()


def serialize_user_row(row: User) -> dict:
    """统一序列化用户列表响应。"""
    effective_permissions = resolve_effective_permissions(row)
    return {
        "id": row.id,
        "username": row.username,
        "name": row.name or row.username,
        "email": row.email or "",
        "role": row.role or "只读",
        "department": row.department or "-",
        "status": normalize_user_status(getattr(row, "status", "active")),
        "last_login": str(getattr(row, "last_login_at", "") or "-"),
        "mfa_enabled": False,
        "permission_count": sum(effective_permissions.values()),
        "access_summary": summarize_permission_labels(effective_permissions),
    }


@router.get("/users", response_model=list[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users")),
):
    users = db.query(User).order_by(User.id.asc()).all()
    return [serialize_user_row(row) for row in users]


@router.post("/users", response_model=UserResponse)
def create_user(
    payload: UserCreateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    """创建新用户，并允许在创建阶段附带动作级权限覆盖。"""
    username = (payload.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if db.query(User.id).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    role_name = (payload.role or "").strip() or "只读"
    if role_name not in [item["role"] for item in list_role_templates()]:
        raise HTTPException(status_code=400, detail=f"未知角色模板: {role_name}")

    email = normalize_user_email(payload.email)
    if email and db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="邮箱已存在")

    status_value = normalize_user_status(payload.status)
    password = validate_user_password(payload.password)
    normalized_effective = resolve_effective_permissions_from_values(
        role_name,
        json.dumps(payload.effective_permissions or {}, ensure_ascii=False),
    )
    overrides = build_permission_overrides(role_name, normalized_effective)

    row = User(
        username=username,
        name=(payload.name or "").strip() or username,
        # 空邮箱存 NULL：email 列有唯一索引，空串会让"第二个无邮箱用户"必然撞
        # ix_users_email 唯一约束 500（PostgreSQL 唯一索引对 NULL 不去重）。
        email=email or None,
        role=role_name,
        department=(payload.department or "").strip(),
        status=status_value,
        permission_overrides=json.dumps(overrides, ensure_ascii=False) if overrides else "",
        hashed_password=hash_password(password),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return serialize_user_row(row)


@router.get("/audits", response_model=list[AuditLogResponse])
def get_audits(
    limit: int = 500,
    offset: int = 0,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.audit")),
):
    # 分页参数：默认行为与历史版本一致（最新 500 条）；limit 上限 1000 防止全量拉爆
    limit = max(1, min(int(limit or 500), 1000))
    offset = max(0, int(offset or 0))
    logs = (
        db.query(AuditLog)
        .order_by(AuditLog.time.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": log.id,
            # AuditLog.time 以 datetime.utcnow() 落库（naive UTC），统一走 to_local_str
            # 按 UTC 解释转本地展示；不能写 astimezone(timezone.utc)——那会把 naive 值
            # 当成"本地时间"再转 UTC，在 UTC+8 机器上显示偏差数小时。
            "time": to_local_str(log.time) or "-",
            "user": log.user,
            "action": log.action,
            "resource": log.resource,
            "ip": log.ip,
            "result": log.result,
            "risk_level": log.risk_level or "",
            "risk_reason": log.risk_reason or "",
        }
        for log in logs
    ]


def read_pve_config(db: Session) -> dict:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == "pve_config").first()
    if not row:
        return {}
    return _safe_load_json_dict(row.config_value)


def read_vmware_config(db: Session) -> dict:
    """读取 vCenter 连接配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == "vmware_config").first()
    if not row:
        return {}
    return _safe_load_json_dict(row.config_value)


def read_feature_toggles(db: Session) -> tuple[dict[str, bool], str]:
    """
    读取功能开关并与默认值合并：
    - 未配置项按默认值处理（默认全开）；
    - 仅允许白名单键，避免脏数据污染配置。
    """
    row = db.query(SystemConfig).filter(SystemConfig.config_key == FEATURE_TOGGLE_CONFIG_KEY).first()
    merged = {**DEFAULT_FEATURE_TOGGLES}
    if not row or not row.config_value:
        return merged, ""
    try:
        payload = json.loads(row.config_value)
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in DEFAULT_FEATURE_TOGGLES:
                continue
            if isinstance(value, bool):
                merged[key] = value
    return merged, str(row.updated_at or "")


def read_login_page_config(db: Session) -> tuple[dict[str, str | bool], str]:
    """读取登录页展示配置。

    登录页在未登录状态访问，所以读取接口需要公开；这里仅返回展示文本和背景图 URL，
    不返回任何 SSO 密钥、Client Secret 等敏感内容。
    """
    row = db.query(SystemConfig).filter(SystemConfig.config_key == LOGIN_PAGE_CONFIG_KEY).first()
    merged = {**DEFAULT_LOGIN_PAGE_CONFIG}
    if row and row.config_value:
        payload = _safe_load_json_dict(row.config_value)
        for key in DEFAULT_LOGIN_PAGE_CONFIG:
            if key not in payload:
                continue
            if key == "sso_enabled":
                merged[key] = bool(payload[key])
            else:
                merged[key] = str(payload[key] or "")
    return merged, str(row.updated_at or "") if row else ""


def normalize_login_page_payload(payload: LoginPageConfigUpdate) -> dict[str, str | bool]:
    """规范化登录页配置，避免超长文本和非 http(s) 背景地址进入前端。"""
    hero_title = (payload.hero_title or "").strip()[:80] or str(DEFAULT_LOGIN_PAGE_CONFIG["hero_title"])
    hero_subtitle = (payload.hero_subtitle or "").strip()[:80] or str(DEFAULT_LOGIN_PAGE_CONFIG["hero_subtitle"])
    background_image_url = (payload.background_image_url or "").strip()[:1000]
    if background_image_url and not background_image_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="背景图地址仅支持 http:// 或 https://")
    sso_button_text = (payload.sso_button_text or "").strip()[:40] or str(DEFAULT_LOGIN_PAGE_CONFIG["sso_button_text"])
    sso_hint = (payload.sso_hint or "").strip()[:180] or str(DEFAULT_LOGIN_PAGE_CONFIG["sso_hint"])
    return {
        "hero_title": hero_title,
        "hero_subtitle": hero_subtitle,
        "background_image_url": background_image_url,
        "sso_enabled": bool(payload.sso_enabled),
        "sso_button_text": sso_button_text,
        "sso_hint": sso_hint,
    }


@public_router.get("/login-page", response_model=LoginPageConfigResponse)
def get_login_page_config(db: Session = Depends(get_db)):
    config, updated_at = read_login_page_config(db)
    return {**config, "updated_at": updated_at}


@router.put("/login-page", response_model=LoginPageConfigResponse)
def save_login_page_config(
    payload: LoginPageConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    config = normalize_login_page_payload(payload)
    from datetime import datetime

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = db.query(SystemConfig).filter(SystemConfig.config_key == LOGIN_PAGE_CONFIG_KEY).first()
    content = json.dumps(config, ensure_ascii=False)
    if not row:
        row = SystemConfig(config_key=LOGIN_PAGE_CONFIG_KEY, config_value=content, updated_at=now_str)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()
    return {**config, "updated_at": now_str}


@router.get("/pve-config", response_model=PVEConfigResponse)
def get_pve_config(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    cfg = read_pve_config(db)
    return {
        "base_url": str(cfg.get("base_url") or ""),
        "token_id": str(cfg.get("token_id") or ""),
        "default_node": str(cfg.get("default_node") or ""),
        "verify_tls": bool(cfg.get("verify_tls", True)),
        "has_secret": bool(str(cfg.get("token_secret") or "").strip()),
    }


@router.put("/pve-config", response_model=PVEConfigResponse)
def save_pve_config(
    payload: PVEConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    base_url = (payload.base_url or "").strip()
    token_id = (payload.token_id or "").strip()
    token_secret = (payload.token_secret or "").strip()
    default_node = (payload.default_node or "").strip()
    verify_tls = bool(payload.verify_tls)

    if not base_url:
        raise HTTPException(status_code=400, detail="PVE 地址不能为空")
    if not token_id:
        raise HTTPException(status_code=400, detail="Token ID 不能为空")

    row = db.query(SystemConfig).filter(SystemConfig.config_key == "pve_config").first()
    # 统一使用安全解析，防止历史配置为 JSON null 导致 .get() 异常。
    current = _safe_load_json_dict(row.config_value if row else "")

    encrypted_secret = str(current.get("token_secret") or "")
    if token_secret:
        encrypted_secret = encrypt_secret(token_secret)
    if not encrypted_secret:
        raise HTTPException(status_code=400, detail="Token Secret 不能为空")

    content = {
        "base_url": base_url,
        "token_id": token_id,
        "token_secret": encrypted_secret,
        "default_node": default_node,
        "verify_tls": verify_tls,
    }

    if not row:
        row = SystemConfig(config_key="pve_config", config_value=json.dumps(content, ensure_ascii=False))
        db.add(row)
    else:
        row.config_value = json.dumps(content, ensure_ascii=False)

    from datetime import datetime
    row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.commit()

    return {
        "base_url": base_url,
        "token_id": token_id,
        "default_node": default_node,
        "verify_tls": verify_tls,
        "has_secret": bool(encrypted_secret),
    }


@router.get("/vmware-config", response_model=VMwareConfigResponse)
def get_vmware_config(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    cfg = read_vmware_config(db)
    return {
        "base_url": str(cfg.get("base_url") or ""),
        "username": str(cfg.get("username") or ""),
        "default_datacenter": str(cfg.get("default_datacenter") or ""),
        "verify_tls": bool(cfg.get("verify_tls", True)),
        "has_secret": bool(str(cfg.get("password") or "").strip()),
    }


@router.put("/vmware-config", response_model=VMwareConfigResponse)
def save_vmware_config(
    payload: VMwareConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """保存 vCenter 连接配置。

    密码只在后端加密保存；GET 接口只返回 has_secret，不回显密文或明文。
    """
    base_url = (payload.base_url or "").strip()
    username = (payload.username or "").strip()
    password = (payload.password or "").strip()
    default_datacenter = (payload.default_datacenter or "").strip()
    verify_tls = bool(payload.verify_tls)

    if not base_url:
        raise HTTPException(status_code=400, detail="vCenter 地址不能为空")
    if not username:
        raise HTTPException(status_code=400, detail="vCenter 用户名不能为空")

    row = db.query(SystemConfig).filter(SystemConfig.config_key == "vmware_config").first()
    current = _safe_load_json_dict(row.config_value if row else "")

    encrypted_secret = str(current.get("password") or "")
    if password:
        encrypted_secret = encrypt_secret(password)
    if not encrypted_secret:
        raise HTTPException(status_code=400, detail="vCenter 密码不能为空")

    content = {
        "base_url": base_url,
        "username": username,
        "password": encrypted_secret,
        "default_datacenter": default_datacenter,
        "verify_tls": verify_tls,
    }

    if not row:
        row = SystemConfig(config_key="vmware_config", config_value=json.dumps(content, ensure_ascii=False))
        db.add(row)
    else:
        row.config_value = json.dumps(content, ensure_ascii=False)

    row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.commit()

    return {
        "base_url": base_url,
        "username": username,
        "default_datacenter": default_datacenter,
        "verify_tls": verify_tls,
        "has_secret": bool(encrypted_secret),
    }


@router.get("/feature-toggles", response_model=FeatureToggleResponse)
def get_feature_toggles(db: Session = Depends(get_db)):
    toggles, updated_at = read_feature_toggles(db)
    return {
        "toggles": toggles,
        "updated_at": updated_at,
    }


@router.put("/feature-toggles", response_model=FeatureToggleResponse)
def save_feature_toggles(
    payload: FeatureToggleUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    row = db.query(SystemConfig).filter(SystemConfig.config_key == FEATURE_TOGGLE_CONFIG_KEY).first()
    current, _ = read_feature_toggles(db)

    incoming = payload.toggles or {}
    for key in incoming.keys():
        if key not in DEFAULT_FEATURE_TOGGLES:
            raise HTTPException(status_code=400, detail=f"未知功能开关键: {key}")

    merged = {**current}
    for key, value in incoming.items():
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"开关键 {key} 的值必须是布尔值")
        merged[key] = value

    content = json.dumps(merged, ensure_ascii=False)
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not row:
        row = SystemConfig(config_key=FEATURE_TOGGLE_CONFIG_KEY, config_value=content, updated_at=now_str)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()

    return {
        "toggles": merged,
        "updated_at": now_str,
    }


@router.get("/rbac/metadata", response_model=RBACMetadataResponse)
def get_rbac_metadata(
    _=Depends(get_current_user),
):
    """返回前端所需的权限目录与角色模板。

    登录引导（AuthGuard bootstrap）对所有已登录用户都会调用本接口来解析
    有效权限、渲染菜单/按钮，因此只要求"已登录"而非用户管理权限——
    否则任何非管理员角色登录后初始化必然失败（普通账号进不了系统）。
    数据本身不含敏感信息；授权管理页面另有页面级权限守卫。
    """
    return {
        "permissions": PERMISSION_CATALOG,
        "role_templates": list_role_templates(),
    }


@router.post("/rbac/role-templates", response_model=RoleTemplateItem)
def create_role_template(
    payload: RoleTemplateCreateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    """创建自定义权限组。"""
    role_name = (payload.role or "").strip()
    if not role_name:
        raise HTTPException(status_code=400, detail="权限组名称不能为空")
    if role_name in [item["role"] for item in list_role_templates()]:
        raise HTTPException(status_code=400, detail="权限组名称已存在")

    custom_templates = read_custom_role_templates(db)
    custom_templates[role_name] = {
        "title": role_name,
        "description": str(payload.description or "").strip(),
        "permissions": normalize_permission_map(payload.permissions or {}),
    }
    save_custom_role_templates(db, custom_templates)

    for item in list_role_templates():
        if item["role"] == role_name:
            return item
    raise HTTPException(status_code=500, detail="权限组创建成功，但回读失败")


@router.put("/rbac/role-templates/{role_name}", response_model=RoleTemplateItem)
def update_role_template(
    role_name: str,
    payload: RoleTemplateUpdateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    """更新权限组。

    说明：
    - 自定义权限组：允许更新说明与权限；
    - 系统预设权限组：允许覆盖说明与权限，但不允许改组名和删除。
    """
    template_name = (role_name or "").strip()
    if not template_name:
        raise HTTPException(status_code=400, detail="权限组名称不能为空")
    if template_name not in [item["role"] for item in list_role_templates()]:
        raise HTTPException(status_code=404, detail="权限组不存在")

    custom_templates = read_custom_role_templates(db)
    custom_templates[template_name] = {
        "title": template_name,
        "description": str(payload.description or "").strip(),
        "permissions": normalize_permission_map(payload.permissions or {}),
    }
    save_custom_role_templates(db, custom_templates)

    for item in list_role_templates():
        if item["role"] == template_name:
            return item
    raise HTTPException(status_code=500, detail="权限组更新成功，但回读失败")


@router.delete("/rbac/role-templates/{role_name}")
def delete_role_template(
    role_name: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    """删除自定义权限组。"""
    template_name = (role_name or "").strip()
    if not template_name:
        raise HTTPException(status_code=400, detail="权限组名称不能为空")
    if is_system_role_template(template_name):
        raise HTTPException(status_code=400, detail="系统预设权限组不允许删除")

    in_use = db.query(User.id).filter(User.role == template_name).first()
    if in_use:
        raise HTTPException(status_code=400, detail="该权限组仍有用户绑定，无法删除")

    custom_templates = read_custom_role_templates(db)
    if template_name not in custom_templates:
        raise HTTPException(status_code=404, detail="权限组不存在")
    del custom_templates[template_name]
    save_custom_role_templates(db, custom_templates)
    return {"success": True, "message": f"权限组 [{template_name}] 已删除"}


@router.get("/users/{user_id}/access", response_model=UserAccessResponse)
def get_user_access(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """读取用户的有效权限视图。

    登录引导会对**自己**调用本接口解析权限矩阵，所以"查自己"只需登录；
    查他人要求用户管理权限（不能用 require_permissions 依赖表达这种二分逻辑，
    挂上去会让普通用户的 bootstrap 必然 403 → 权限矩阵为空 → 全站按钮消失）。
    """
    if current_user.id != user_id and not resolve_effective_permissions(current_user).get(
        "module.settings.users"
    ):
        raise HTTPException(status_code=403, detail="只能查看自己的权限视图")
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    role_name = (row.role or "只读").strip() or "只读"
    return {
        "user_id": row.id,
        "username": row.username,
        "name": row.name or row.username,
        "role": role_name,
        "email": row.email or "",
        "department": row.department or "",
        "role_permissions": get_role_permissions(role_name),
        "permission_overrides": parse_permission_overrides(row.permission_overrides or ""),
        "effective_permissions": resolve_effective_permissions(row),
    }


@router.put("/users/{user_id}/status", response_model=UserResponse)
def update_user_status(
    user_id: int,
    payload: UserStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    """启用或禁用用户。

    高危票据按目标状态动态校验（不能用固定路由依赖表达）：
    禁用是高危动作（action.settings.users.manage + disable，medium），
    必须先经二次确认换票；启用只是恢复访问，不要求票据——旧版挂固定
    require_risk_ack 会让"启用一个被误禁的人"也被索要 disable 票，与动作
    语义不符。
    """
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")

    target_status = normalize_user_status(payload.status)
    if current_user.id == row.id and target_status == "disabled":
        raise HTTPException(status_code=400, detail="不允许禁用当前登录账号")

    if target_status == "disabled":
        consume_risk_ticket(
            request,
            db,
            permission_key="action.settings.users.manage",
            action_id="disable",
            user=current_user,
        )

    row.status = target_status
    # 禁用即递增会话版本号：该账号全部已签发 token 立即失效（启用不递增，
    # 被禁用前的登录态本就应作废，恢复后需重新登录）。
    if target_status == "disabled":
        row.token_version = (getattr(row, "token_version", 0) or 0) + 1
    db.commit()
    db.refresh(row)
    return serialize_user_row(row)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _risk=Depends(require_risk_ack("action.settings.users.manage", "delete")),
):
    """删除用户（高危，需二次确认票据）。

    删除是硬删除：审计日志引用该用户的历史记录以用户名字符串保留（audit_logs.user
    冗余存名而非外键），但该账号无法再登录、不出现在用户列表。
    保护规则：不能删自己；不能删最后一个管理员账号（防止把系统锁死在门外）。
    """
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if current_user.id == row.id:
        raise HTTPException(status_code=400, detail="不允许删除当前登录账号")
    if (row.role or "") == "管理员":
        admin_count = db.query(User).filter(User.role == "管理员", User.status == "active").count()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="系统至少保留一个可用的管理员账号")

    username = row.username
    display_name = row.name or username
    db.delete(row)
    db.add(
        AuditLog(
            user=str(current_user.name or current_user.username or "未知用户"),
            action="删除用户",
            resource=f"/settings/users/{user_id}:{username}"[:255],
            ip=str(request.client.host if request.client else "-"),
            result="success",
            risk_level="high",
            risk_reason="删除用户账号（不可恢复）",
        )
    )
    db.commit()
    return {"message": f"用户 {display_name} 已删除", "id": user_id}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    payload: UserPasswordResetRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
    _risk=Depends(require_risk_ack("action.settings.users.manage", "reset-password")),
):
    """重置用户密码（高危：重置即踢掉对方全部会话，需二次确认）。"""
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")

    row.hashed_password = hash_password(validate_user_password(payload.new_password))
    # 密码已变，递增会话版本号踢掉该用户全部旧 token（防止旧凭据继续可用）。
    row.token_version = (getattr(row, "token_version", 0) or 0) + 1
    db.commit()
    return {"success": True, "message": f"已重置用户 [{row.username}] 的密码"}


@router.put("/users/{user_id}/access", response_model=UserAccessResponse)
def update_user_access(
    user_id: int,
    payload: UserAccessUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.users", "action.settings.users.manage")),
):
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")

    role_name = (payload.role or "").strip()
    if not role_name:
        raise HTTPException(status_code=400, detail="角色不能为空")
    role_permissions = get_role_permissions(role_name)
    if role_name not in [item["role"] for item in list_role_templates()]:
        raise HTTPException(status_code=400, detail=f"未知角色模板: {role_name}")

    effective_permissions = payload.effective_permissions or {}
    for key, value in effective_permissions.items():
        if key not in role_permissions:
            raise HTTPException(status_code=400, detail=f"未知权限键: {key}")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"权限 {key} 的值必须是布尔值")

    normalized_effective = resolve_effective_permissions_from_values(
        role_name,
        json.dumps(effective_permissions, ensure_ascii=False),
    )
    overrides = build_permission_overrides(role_name, normalized_effective)

    row.role = role_name
    row.permission_overrides = json.dumps(overrides, ensure_ascii=False) if overrides else ""
    db.commit()
    db.refresh(row)

    return {
        "user_id": row.id,
        "username": row.username,
        "name": row.name or row.username,
        "role": row.role,
        "email": row.email or "",
        "department": row.department or "",
        "role_permissions": get_role_permissions(row.role or "只读"),
        "permission_overrides": parse_permission_overrides(row.permission_overrides or ""),
        "effective_permissions": resolve_effective_permissions(row),
    }
