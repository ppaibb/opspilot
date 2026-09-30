"""运维工具接口（批量执行与批量模板）。

架构说明：
1. 当前开放 Ansible ad-hoc、Playbook、Shell 模板与模板管理能力，不引入调度器；
2. 目标主机从资产管理读取，认证信息复用资产 SSH 字段与凭证中心；
3. Ansible / Playbook 入口固定走后端 subprocess（shell=False），Shell 模板走 Paramiko SSH exec_command。

安全审计：
1. Ansible / Playbook 前端不能拼接命令行，仅可提交模块、参数或 YAML 内容，最终命令由后端构造；
2. Ansible 执行只允许白名单模块（ping/command/shell/setup），降低误用面；
3. 临时 inventory 与私钥文件执行后删除，不长期落盘；
4. Shell 模板只允许具备批量执行动作权限的用户执行，并继续校验主机凭证授权；
5. 写操作接口受动作权限控制（配置保存、执行任务），并记录审计日志。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.core.rbac import list_role_templates
from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.asset import Asset
from app.models.audit import AuditLog
from app.models.credential import Credential
from app.models.system_config import SystemConfig
from app.models.user import User
from app.api.assets import run_ssh_command
from app.services.credential_access import credential_visible_to_user, filter_credentials_for_user

router = APIRouter()

ANSIBLE_CONFIG_KEY = "ops_tools_ansible_config"
ANSIBLE_HISTORY_KEY = "ops_tools_ansible_history"
ANSIBLE_TEMPLATES_KEY = "ops_tools_ansible_templates"
ALLOWED_ANSIBLE_MODULES = {"ping", "command", "shell", "setup"}
ALLOWED_TEMPLATE_KINDS = {"ansible", "shell", "playbook"}
MAX_OUTPUT_LENGTH = 12000
MAX_HISTORY_SIZE = 80
MAX_TEMPLATE_COUNT = 100
MAX_TEMPLATE_ASSET_COUNT = 200
MAX_PLAYBOOK_LENGTH = 30000
MAX_TEMPLATE_CONTENT_LENGTH = MAX_PLAYBOOK_LENGTH

DEFAULT_ANSIBLE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "executable": "ansible",
    "forks": 10,
    "timeout_seconds": 120,
    "host_key_checking": False,
}


class AnsibleConfigResponse(BaseModel):
    """Ansible 配置读取响应。"""

    enabled: bool
    executable: str
    forks: int
    timeout_seconds: int
    host_key_checking: bool
    ansible_available: bool
    ansible_version: str
    last_error: str
    updated_at: str


class AnsibleConfigUpdate(BaseModel):
    """Ansible 配置保存请求。"""

    enabled: bool = True
    executable: str = "ansible"
    forks: int = Field(default=10, ge=1, le=100)
    timeout_seconds: int = Field(default=120, ge=10, le=3600)
    host_key_checking: bool = False


class AnsibleTargetItem(BaseModel):
    """Ansible 可执行目标。"""

    asset_id: str
    name: str
    ip: str
    ssh_user: str
    ssh_port: int
    provider: str
    region: str
    online: bool
    auth_mode: str
    ansible_ready: bool
    auth_notice: str


class AnsibleTargetsResponse(BaseModel):
    """Ansible 目标列表响应。"""

    targets: list[AnsibleTargetItem]


class AnsibleHistoryItem(BaseModel):
    """Ansible 执行历史条目。"""

    run_id: str
    created_at: str
    user: str
    module: str
    target_count: int
    duration_ms: int
    ok: bool
    code: int
    stdout: str = ""
    stderr: str = ""
    command_preview: str = ""
    notice: str = ""


class AnsibleHistoryResponse(BaseModel):
    """Ansible 执行历史响应。"""

    items: list[AnsibleHistoryItem]


class AnsibleTemplateItem(BaseModel):
    """批量模板条目。"""

    template_id: str
    kind: str
    name: str
    description: str
    module: str
    module_args: str
    become: bool
    become_user: str
    run_all: bool
    asset_ids: list[str]
    allowed_roles: list[str]
    created_at: str
    updated_at: str


class AnsibleTemplateListResponse(BaseModel):
    """批量模板列表响应。"""

    items: list[AnsibleTemplateItem]


class AnsibleTemplateUpsertRequest(BaseModel):
    """批量模板创建/更新请求。"""

    kind: str = Field(default="ansible", max_length=20)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    module: str = Field(default="ping", max_length=32)
    module_args: str = Field(default="", max_length=MAX_TEMPLATE_CONTENT_LENGTH)
    become: bool = False
    become_user: str = Field(default="root", max_length=64)
    run_all: bool = False
    asset_ids: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)


class RoleOptionItem(BaseModel):
    """模板授权可选权限组。"""

    role: str
    title: str
    description: str


class AnsibleTemplateRunRequest(BaseModel):
    """模板执行请求。"""

    template_id: str = Field(min_length=1, max_length=64)
    run_all_override: bool | None = None
    asset_ids_override: list[str] = Field(default_factory=list)


class AnsibleRunRequest(BaseModel):
    """Ansible 执行请求。"""

    run_all: bool = False
    asset_ids: list[str] = Field(default_factory=list)
    module: str = "ping"
    module_args: str = Field(default="", max_length=2000)
    become: bool = False
    become_user: str = Field(default="root", max_length=64)


class AnsiblePlaybookRunRequest(BaseModel):
    """Ansible Playbook 直接执行请求。"""

    run_all: bool = False
    asset_ids: list[str] = Field(default_factory=list)
    playbook: str = Field(min_length=1, max_length=MAX_PLAYBOOK_LENGTH)


class AnsibleRunResponse(BaseModel):
    """Ansible 执行响应。"""

    ok: bool
    run_id: str
    module: str
    target_count: int
    duration_ms: int
    code: int
    stdout: str
    stderr: str
    command_preview: str
    notice: str


class SimpleOkResponse(BaseModel):
    """通用成功响应。"""

    ok: bool = True


def _now_str() -> str:
    """返回本地时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON 字典。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_load_json_list(raw_value: str | None) -> list[dict[str, Any]]:
    """安全解析 JSON 列表。"""
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            result.append(item)
    return result


def _get_system_config_row(db: Session, key: str) -> SystemConfig | None:
    """读取系统配置行。"""
    return db.query(SystemConfig).filter(SystemConfig.config_key == key).first()


def _save_system_config(db: Session, key: str, value: dict[str, Any] | list[dict[str, Any]]) -> SystemConfig:
    """保存系统配置（upsert）。"""
    payload = json.dumps(value, ensure_ascii=False)
    row = _get_system_config_row(db, key)
    if row:
        row.config_value = payload
    else:
        row = SystemConfig(config_key=key, config_value=payload)
        db.add(row)
    db.flush()
    db.refresh(row)
    return row


def _normalize_ansible_config(raw: dict[str, Any]) -> dict[str, Any]:
    """归一化 Ansible 配置。"""
    merged = {**DEFAULT_ANSIBLE_CONFIG, **raw}
    executable = str(merged.get("executable") or "ansible").strip() or "ansible"
    forks = int(merged.get("forks") or 10)
    timeout_seconds = int(merged.get("timeout_seconds") or 120)
    return {
        "enabled": bool(merged.get("enabled")),
        "executable": executable,
        "forks": min(max(forks, 1), 100),
        "timeout_seconds": min(max(timeout_seconds, 10), 3600),
        "host_key_checking": bool(merged.get("host_key_checking")),
    }


def _read_ansible_config(db: Session) -> tuple[dict[str, Any], str]:
    """读取 Ansible 配置。"""
    row = _get_system_config_row(db, ANSIBLE_CONFIG_KEY)
    payload = _safe_load_json_dict(row.config_value if row else "")
    normalized = _normalize_ansible_config(payload)
    return normalized, str(row.updated_at if row else "")


def _read_ansible_history(db: Session) -> list[dict[str, Any]]:
    """读取执行历史。"""
    row = _get_system_config_row(db, ANSIBLE_HISTORY_KEY)
    return _safe_load_json_list(row.config_value if row else "")


def _append_ansible_history(db: Session, item: dict[str, Any]) -> None:
    """追加执行历史并裁剪容量。"""
    history = _read_ansible_history(db)
    history.insert(0, item)
    _save_system_config(db, ANSIBLE_HISTORY_KEY, history[:MAX_HISTORY_SIZE])


def _normalize_asset_ids(asset_ids: list[str]) -> list[str]:
    """归一化资产 ID 列表并去重。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in asset_ids:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
        if len(normalized) >= MAX_TEMPLATE_ASSET_COUNT:
            break
    return normalized


def _normalize_allowed_roles(raw_roles: list[str] | None, *, strict: bool = True) -> list[str]:
    """归一化模板授权权限组。

    空列表代表不限制模板可见范围；非空列表必须来自系统权限组，避免前端写入
    不存在的角色名导致后续授权判断失真。
    """
    valid_roles = {str(item["role"]) for item in list_role_templates()}
    normalized = list(dict.fromkeys([
        str(item or "").strip()
        for item in (raw_roles or [])
        if str(item or "").strip()
    ]))
    unknown_roles = [item for item in normalized if item not in valid_roles]
    if unknown_roles and strict:
        raise HTTPException(status_code=400, detail=f"未知权限组: {', '.join(unknown_roles)}")
    return [item for item in normalized if item in valid_roles]


def _template_visible_to_user(item: dict[str, Any], user: User) -> bool:
    """判断当前用户是否可见或可执行该模板。

    模板授权是模板自身的第一层范围控制；真正执行时还会继续校验页面动作权限、
    目标主机可见性、SSH 凭证授权组和 Ansible 白名单模块。
    """
    roles = _normalize_allowed_roles(item.get("allowed_roles") or [], strict=False)
    if not roles:
        return True
    return str(user.role or "").strip() in roles


def _normalize_template_row(raw: dict[str, Any]) -> dict[str, Any]:
    """归一化模板行结构，避免历史脏数据影响读取。"""
    raw_module = str(raw.get("module") or "ping").strip().lower()
    # 兼容过渡期数据：前端已经提供“Shell 模板”入口，但旧后端不会保存 kind。
    # 这类记录通常表现为 module=shell，按 SSH Shell 模板处理更符合用户预期。
    raw_kind = str(raw.get("kind") or "").strip().lower()
    inferred_kind = "shell" if raw_module == "shell" else "ansible"
    if raw_module == "playbook":
        inferred_kind = "playbook"
    template_kind = raw_kind or inferred_kind
    if template_kind not in ALLOWED_TEMPLATE_KINDS:
        template_kind = "ansible"

    module_name = raw_module
    if template_kind == "shell":
        module_name = "shell"
    elif template_kind == "playbook":
        module_name = "playbook"
    elif module_name not in ALLOWED_ANSIBLE_MODULES:
        module_name = "ping"

    created_at = str(raw.get("created_at") or "")
    updated_at = str(raw.get("updated_at") or created_at or _now_str())
    template_id = str(raw.get("template_id") or "").strip()
    if not template_id:
        template_id = f"TPL-{uuid.uuid4().hex[:8].upper()}"
    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        name = template_id

    return {
        "template_id": template_id,
        "kind": template_kind,
        "name": name,
        "description": str(raw.get("description") or "").strip()[:200],
        "module": module_name,
        "module_args": str(raw.get("module_args") or "").strip()[:MAX_TEMPLATE_CONTENT_LENGTH],
        "become": bool(raw.get("become")),
        "become_user": str(raw.get("become_user") or "root").strip()[:64] or "root",
        "run_all": bool(raw.get("run_all")),
        "asset_ids": _normalize_asset_ids(raw.get("asset_ids") or []),
        "allowed_roles": _normalize_allowed_roles(raw.get("allowed_roles") or [], strict=False),
        "created_at": created_at or _now_str(),
        "updated_at": updated_at,
    }


def _read_ansible_templates(db: Session) -> list[dict[str, Any]]:
    """读取模板配置。"""
    row = _get_system_config_row(db, ANSIBLE_TEMPLATES_KEY)
    raw_items = _safe_load_json_list(row.config_value if row else "")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        item = _normalize_template_row(raw)
        template_id = str(item["template_id"])
        if template_id in seen_ids:
            continue
        normalized.append(item)
        seen_ids.add(template_id)
        if len(normalized) >= MAX_TEMPLATE_COUNT:
            break
    return normalized


def _save_ansible_templates(db: Session, items: list[dict[str, Any]]) -> None:
    """保存模板配置。"""
    _save_system_config(db, ANSIBLE_TEMPLATES_KEY, items[:MAX_TEMPLATE_COUNT])


def _template_dict_to_model(item: dict[str, Any]) -> AnsibleTemplateItem:
    """将模板字典转换为响应模型。"""
    return AnsibleTemplateItem(
        template_id=str(item.get("template_id") or ""),
        kind=str(item.get("kind") or "ansible"),
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        module=str(item.get("module") or "ping"),
        module_args=str(item.get("module_args") or ""),
        become=bool(item.get("become")),
        become_user=str(item.get("become_user") or "root"),
        run_all=bool(item.get("run_all")),
        asset_ids=_normalize_asset_ids(item.get("asset_ids") or []),
        allowed_roles=_normalize_allowed_roles(item.get("allowed_roles") or [], strict=False),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
    )


def _build_template_payload(payload: AnsibleTemplateUpsertRequest) -> dict[str, Any]:
    """构建模板数据，并进行白名单校验。"""
    template_kind = str(payload.kind or "ansible").strip().lower()
    if template_kind not in ALLOWED_TEMPLATE_KINDS:
        raise HTTPException(status_code=400, detail="模板类型仅支持 Ansible、Shell 或 Playbook。")

    module_name = str(payload.module or "").strip().lower()
    if template_kind == "shell":
        module_name = "shell"
    elif template_kind == "playbook":
        module_name = "playbook"
    elif module_name not in ALLOWED_ANSIBLE_MODULES:
        raise HTTPException(
            status_code=400,
            detail=f"模板模块不在白名单中，当前仅支持：{', '.join(sorted(ALLOWED_ANSIBLE_MODULES))}",
        )
    return {
        "kind": template_kind,
        "name": str(payload.name or "").strip(),
        "description": str(payload.description or "").strip(),
        "module": module_name,
        "module_args": str(payload.module_args or "").strip()[:MAX_TEMPLATE_CONTENT_LENGTH],
        "become": bool(payload.become),
        "become_user": str(payload.become_user or "root").strip() or "root",
        "run_all": bool(payload.run_all),
        "asset_ids": _normalize_asset_ids(payload.asset_ids),
        "allowed_roles": _normalize_allowed_roles(payload.allowed_roles),
    }


def _find_template_index(items: list[dict[str, Any]], template_id: str) -> int:
    """查找模板下标。"""
    for index, item in enumerate(items):
        if str(item.get("template_id") or "") == template_id:
            return index
    return -1


def _check_ansible_available(executable: str) -> tuple[bool, str, str]:
    """检测 Ansible 可用性。"""
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return False, "", output or "Ansible 检测失败"
        first_line = output.splitlines()[0] if output else ""
        return True, first_line, ""
    except FileNotFoundError:
        return False, "", f"未找到可执行文件：{executable}"
    except subprocess.TimeoutExpired:
        return False, "", "Ansible 版本检测超时"


def _asset_online(asset: Asset) -> bool:
    """判断资产在线状态。"""
    return str(asset.type or "").strip().lower() == "healthy"


def _asset_identity(asset: Asset) -> str:
    """返回资产唯一标识（优先 instance_id，兜底数据库主键）。"""
    return str(asset.instance_id or f"asset-{asset.id}")


def _decode_asset_password(asset: Asset) -> str:
    """解密资产密码。"""
    raw = str(asset.ssh_password or "").strip()
    if not raw:
        return ""
    return decrypt_secret(raw)


def _resolve_asset_auth(asset: Asset, credential_map: dict[str, Credential]) -> tuple[str, str, str]:
    """解析资产认证信息。

    返回：
    1. 私钥内容（若有）；
    2. 密码内容（若有）；
    3. 认证说明文案。
    """
    password = _decode_asset_password(asset)
    private_key = ""
    notice = ""

    credential_ref = str(asset.credential_ref or "").strip()
    if credential_ref:
        credential = credential_map.get(credential_ref)
        if credential:
            content = decrypt_secret(str(credential.content or ""))
            cred_type = str(credential.type or "").strip().lower()
            if "private key" in content.lower() or ("ssh" in cred_type and "key" in cred_type):
                private_key = content.strip()
                notice = f"凭证 {credential_ref}（SSH Key）"
            elif "password" in cred_type and content.strip():
                password = content.strip()
                notice = f"凭证 {credential_ref}（Password）"

    if private_key:
        return private_key, "", notice or "SSH 私钥"
    if password:
        return "", password, notice or "SSH 密码"
    return "", "", "未配置可用 SSH 认证"


def _build_target_items(db: Session, current_user: User | None = None) -> list[AnsibleTargetItem]:
    """构建前端目标列表。"""
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    cred_refs = {
        str(item.credential_ref or "").strip()
        for item in assets
        if str(item.credential_ref or "").strip()
    }
    credential_rows = (
        db.query(Credential).filter(Credential.cred_id.in_(list(cred_refs))).all()
        if cred_refs
        else []
    )
    denied_refs = {
        str(item.cred_id)
        for item in credential_rows
        if not credential_visible_to_user(item, current_user)
    }
    credential_rows = filter_credentials_for_user(credential_rows, current_user)
    credential_map = {str(item.cred_id): item for item in credential_rows}

    result: list[AnsibleTargetItem] = []
    for asset in assets:
        credential_ref = str(asset.credential_ref or "").strip()
        private_key, password, auth_notice = _resolve_asset_auth(asset, credential_map)
        if credential_ref in denied_refs:
            auth_notice = "当前账号无权使用关联 SSH 凭证"
        auth_mode = "none"
        if private_key:
            auth_mode = "key"
        elif password:
            auth_mode = "password"
        result.append(
            AnsibleTargetItem(
                asset_id=_asset_identity(asset),
                name=str(asset.name or "-"),
                ip=str(asset.ip or "-"),
                ssh_user=str(asset.ssh_user or "root"),
                ssh_port=int(asset.ssh_port or 22),
                provider=str(asset.provider or "local"),
                region=str(asset.region or "local"),
                online=_asset_online(asset),
                auth_mode=auth_mode,
                ansible_ready=bool(private_key or password),
                auth_notice=auth_notice,
            )
        )
    return result


def _run_ansible_command(
    args: list[str],
    timeout_seconds: int,
    host_key_checking: bool,
) -> dict[str, Any]:
    """执行 Ansible 命令。

    安全约束：
    1. 不允许 shell=True；
    2. 统一设置超时，防止任务失控；
    3. 输出内容截断，避免响应体过大。
    """
    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "True" if host_key_checking else "False"
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
            env=env,
        )
        return {
            "ok": completed.returncode == 0,
            "code": int(completed.returncode),
            "stdout": str(completed.stdout or "")[-MAX_OUTPUT_LENGTH:],
            "stderr": str(completed.stderr or "")[-MAX_OUTPUT_LENGTH:],
        }
    except FileNotFoundError:
        return {"ok": False, "code": 127, "stdout": "", "stderr": "未找到 ansible 可执行文件"}
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "code": 124,
            "stdout": "",
            "stderr": f"执行超过超时时间（{timeout_seconds}s）",
        }


def _resolve_ansible_playbook_executable(ansible_executable: str) -> str:
    """从 ansible 可执行文件推断 ansible-playbook 命令。"""
    executable = str(ansible_executable or "ansible").strip() or "ansible"
    normalized = executable.replace("\\", "/")
    if normalized.endswith("ansible-playbook"):
        return executable
    if normalized.endswith("/ansible"):
        return f"{executable[:-len('ansible')]}ansible-playbook"
    if executable == "ansible":
        return "ansible-playbook"
    return "ansible-playbook"


def _append_audit_log(
    db: Session,
    request: Request,
    current_user: User,
    resource: str,
    result: str,
) -> None:
    """写入审计日志。"""
    client_ip = request.client.host if request.client else "-"
    db.add(
        AuditLog(
            user=str(current_user.name or current_user.username or "未知用户"),
            action="Ansible执行",
            resource=resource[:255],
            ip=str(client_ip or "-"),
            result=result,
        )
    )
    db.flush()


@router.get(
    "/ansible/config",
    response_model=AnsibleConfigResponse,
    dependencies=[Depends(require_permissions("module.tools.scripts"))],
)
def get_ansible_config(db: Session = Depends(get_db)) -> AnsibleConfigResponse:
    """读取 Ansible 配置与运行状态。"""
    config, updated_at = _read_ansible_config(db)
    available, version, last_error = _check_ansible_available(config["executable"])
    return AnsibleConfigResponse(
        enabled=bool(config["enabled"]),
        executable=str(config["executable"]),
        forks=int(config["forks"]),
        timeout_seconds=int(config["timeout_seconds"]),
        host_key_checking=bool(config["host_key_checking"]),
        ansible_available=available,
        ansible_version=version,
        last_error=last_error,
        updated_at=updated_at,
    )


@router.put(
    "/ansible/config",
    response_model=AnsibleConfigResponse,
    dependencies=[Depends(require_permissions("action.tools.scripts.manage"))],
)
def update_ansible_config(
    payload: AnsibleConfigUpdate,
    db: Session = Depends(get_db),
) -> AnsibleConfigResponse:
    """保存 Ansible 配置。"""
    saved = _save_system_config(
        db,
        ANSIBLE_CONFIG_KEY,
        {
            "enabled": payload.enabled,
            "executable": payload.executable.strip() or "ansible",
            "forks": payload.forks,
            "timeout_seconds": payload.timeout_seconds,
            "host_key_checking": payload.host_key_checking,
        },
    )
    config, _ = _read_ansible_config(db)
    available, version, last_error = _check_ansible_available(config["executable"])
    return AnsibleConfigResponse(
        enabled=bool(config["enabled"]),
        executable=str(config["executable"]),
        forks=int(config["forks"]),
        timeout_seconds=int(config["timeout_seconds"]),
        host_key_checking=bool(config["host_key_checking"]),
        ansible_available=available,
        ansible_version=version,
        last_error=last_error,
        updated_at=str(saved.updated_at),
    )


@router.get(
    "/ansible/targets",
    response_model=AnsibleTargetsResponse,
    dependencies=[Depends(require_permissions("module.tools.scripts"))],
)
def get_ansible_targets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnsibleTargetsResponse:
    """读取 Ansible 可执行目标。"""
    return AnsibleTargetsResponse(targets=_build_target_items(db, current_user))


@router.get(
    "/ansible/history",
    response_model=AnsibleHistoryResponse,
    dependencies=[Depends(require_permissions("module.tools.scripts"))],
)
def get_ansible_history(db: Session = Depends(get_db)) -> AnsibleHistoryResponse:
    """读取 Ansible 最近执行历史。"""
    raw_items = _read_ansible_history(db)
    result: list[AnsibleHistoryItem] = []
    for item in raw_items:
        result.append(
            AnsibleHistoryItem(
                run_id=str(item.get("run_id") or ""),
                created_at=str(item.get("created_at") or ""),
                user=str(item.get("user") or "-"),
                module=str(item.get("module") or "-"),
                target_count=int(item.get("target_count") or 0),
                duration_ms=int(item.get("duration_ms") or 0),
                ok=bool(item.get("ok")),
                code=int(item.get("code") or 0),
                stdout=str(item.get("stdout") or ""),
                stderr=str(item.get("stderr") or ""),
                command_preview=str(item.get("command_preview") or ""),
                notice=str(item.get("notice") or ""),
            )
        )
    return AnsibleHistoryResponse(items=result[:30])


@router.get(
    "/ansible/templates",
    response_model=AnsibleTemplateListResponse,
    dependencies=[Depends(require_permissions("module.tools.scripts"))],
)
def get_ansible_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnsibleTemplateListResponse:
    """读取 Ansible 模板列表。"""
    items = [
        item
        for item in _read_ansible_templates(db)
        if _template_visible_to_user(item, current_user)
    ]
    return AnsibleTemplateListResponse(items=[_template_dict_to_model(item) for item in items])


@router.get(
    "/ansible/role-options",
    response_model=list[RoleOptionItem],
    dependencies=[Depends(require_permissions("module.tools.scripts"))],
)
def get_ansible_role_options() -> list[RoleOptionItem]:
    """返回批量模板授权可选权限组。"""
    return [
        RoleOptionItem(
            role=str(item["role"]),
            title=str(item.get("title") or item["role"]),
            description=str(item.get("description") or ""),
        )
        for item in list_role_templates()
    ]


@router.post(
    "/ansible/templates",
    response_model=AnsibleTemplateItem,
    dependencies=[Depends(require_permissions("action.tools.scripts.manage"))],
)
def create_ansible_template(
    payload: AnsibleTemplateUpsertRequest,
    db: Session = Depends(get_db),
) -> AnsibleTemplateItem:
    """新增模板。"""
    items = _read_ansible_templates(db)
    if len(items) >= MAX_TEMPLATE_COUNT:
        raise HTTPException(status_code=400, detail=f"模板数量已达上限（{MAX_TEMPLATE_COUNT}）。")

    template_payload = _build_template_payload(payload)
    now_str = _now_str()
    item = {
        "template_id": f"TPL-{uuid.uuid4().hex[:8].upper()}",
        **template_payload,
        "created_at": now_str,
        "updated_at": now_str,
    }
    items.insert(0, item)
    _save_ansible_templates(db, items)
    db.commit()
    return _template_dict_to_model(item)


@router.put(
    "/ansible/templates/{template_id}",
    response_model=AnsibleTemplateItem,
    dependencies=[Depends(require_permissions("action.tools.scripts.manage"))],
)
def update_ansible_template(
    template_id: str,
    payload: AnsibleTemplateUpsertRequest,
    db: Session = Depends(get_db),
) -> AnsibleTemplateItem:
    """更新模板。"""
    template_id = str(template_id or "").strip()
    if not template_id:
        raise HTTPException(status_code=400, detail="模板 ID 不能为空。")

    items = _read_ansible_templates(db)
    index = _find_template_index(items, template_id)
    if index < 0:
        raise HTTPException(status_code=404, detail="模板不存在或已删除。")

    previous = items[index]
    template_payload = _build_template_payload(payload)
    item = {
        "template_id": template_id,
        **template_payload,
        "created_at": str(previous.get("created_at") or _now_str()),
        "updated_at": _now_str(),
    }
    items[index] = item
    _save_ansible_templates(db, items)
    db.commit()
    return _template_dict_to_model(item)


@router.delete(
    "/ansible/templates/{template_id}",
    response_model=SimpleOkResponse,
    dependencies=[Depends(require_permissions("action.tools.scripts.manage"))],
)
def delete_ansible_template(template_id: str, db: Session = Depends(get_db)) -> SimpleOkResponse:
    """删除模板。"""
    template_id = str(template_id or "").strip()
    if not template_id:
        raise HTTPException(status_code=400, detail="模板 ID 不能为空。")

    items = _read_ansible_templates(db)
    index = _find_template_index(items, template_id)
    if index < 0:
        raise HTTPException(status_code=404, detail="模板不存在或已删除。")
    items.pop(index)
    _save_ansible_templates(db, items)
    db.commit()
    return SimpleOkResponse(ok=True)


def _trim_template_output(text: str) -> str:
    """裁剪模板执行输出，避免一次批量任务把响应和历史撑爆。"""
    raw = text or ""
    if len(raw) <= MAX_OUTPUT_LENGTH:
        return raw
    return raw[:MAX_OUTPUT_LENGTH] + "\n... output truncated ..."


def _select_template_target_assets(
    db: Session,
    run_all: bool,
    asset_ids: list[str],
) -> list[Asset]:
    """按模板目标规则选择资产。"""
    selected_assets = db.query(Asset).all()
    if run_all:
        return selected_assets

    selected_ids = {
        str(item).strip()
        for item in asset_ids
        if str(item).strip()
    }
    if not selected_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个目标主机。")
    return [item for item in selected_assets if _asset_identity(item) in selected_ids]


def _load_authorized_credential_map(
    db: Session,
    target_assets: list[Asset],
    current_user: User,
) -> dict[str, Credential]:
    """读取目标主机关联凭证，并确保当前用户有权使用。"""
    credential_refs = {
        str(item.credential_ref or "").strip()
        for item in target_assets
        if str(item.credential_ref or "").strip()
    }
    credential_rows = (
        db.query(Credential).filter(Credential.cred_id.in_(list(credential_refs))).all()
        if credential_refs
        else []
    )
    denied_refs = [
        str(item.cred_id)
        for item in credential_rows
        if not credential_visible_to_user(item, current_user)
    ]
    if denied_refs:
        raise HTTPException(status_code=403, detail="当前账号无权使用部分目标主机的 SSH 凭证")

    return {
        str(item.cred_id): item
        for item in filter_credentials_for_user(credential_rows, current_user)
    }


def _execute_shell_template(
    *,
    command: str,
    run_all: bool,
    asset_ids: list[str],
    request: Request,
    db: Session,
    current_user: User,
    run_source: str,
) -> AnsibleRunResponse:
    """通过 SSH 顺序执行 Shell 模板。

    Shell 模板用于“不需要 Ansible 环境”的场景，仍复用主机凭证授权校验和审计。
    这里不做 shell 拼接，脚本原文只作为远端 `exec_command` 内容发送。
    """
    shell_text = str(command or "").strip()
    if not shell_text:
        raise HTTPException(status_code=400, detail="Shell 模板内容不能为空。")
    if "\x00" in shell_text:
        raise HTTPException(status_code=400, detail="Shell 模板内容包含非法字符。")

    config, _ = _read_ansible_config(db)
    timeout_seconds = int(config.get("timeout_seconds") or 120)
    target_assets = _select_template_target_assets(db, run_all, asset_ids)
    if not target_assets:
        raise HTTPException(status_code=404, detail="未找到可执行的目标主机。")

    credential_map = _load_authorized_credential_map(db, target_assets, current_user)
    run_id = f"SH-{uuid.uuid4().hex[:8].upper()}"
    started_at = time.perf_counter()
    stdout_blocks: list[str] = []
    stderr_blocks: list[str] = []
    executed_count = 0
    failed_count = 0
    first_nonzero_code = 0
    skipped_assets: list[str] = []

    try:
        for asset in target_assets:
            private_key, password, _ = _resolve_asset_auth(asset, credential_map)
            asset_name = str(asset.name or asset.instance_id or asset.ip or "unknown")
            if not private_key and not password:
                skipped_assets.append(asset_name)
                continue

            executed_count += 1
            ok, message, out, err, code = run_ssh_command(
                str(asset.ip or ""),
                int(asset.ssh_port or 22),
                str(asset.ssh_user or "root"),
                shell_text,
                timeout_seconds,
                password,
                private_key,
            )
            header = f"[{asset_name} {asset.ssh_user or 'root'}@{asset.ip}:{asset.ssh_port or 22}]"
            stdout_blocks.append(f"{header}\n{out or message}".rstrip())
            if err:
                stderr_blocks.append(f"{header}\n{err}".rstrip())
            if not ok or int(code or 0) != 0:
                failed_count += 1
                if first_nonzero_code == 0:
                    first_nonzero_code = int(code or -1)

        if executed_count == 0:
            raise HTTPException(status_code=400, detail="选中主机均未配置可用 SSH 认证。")

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        ok = failed_count == 0
        notice_parts: list[str] = []
        if skipped_assets:
            notice_parts.append(f"跳过 {len(skipped_assets)} 台未配置 SSH 认证的主机")
        notice_parts.append("Shell 模板执行完成" if ok else f"Shell 模板执行失败（{failed_count} 台失败）")
        notice = "，".join(notice_parts)
        stdout_text = _trim_template_output("\n\n".join(stdout_blocks))
        stderr_text = _trim_template_output("\n\n".join(stderr_blocks))
        preview_line = shell_text.splitlines()[0] if shell_text.splitlines() else shell_text
        command_preview = f"ssh <{executed_count} hosts> -- {preview_line[:120]}"

        _append_ansible_history(
            db,
            {
                "run_id": run_id,
                "created_at": _now_str(),
                "user": str(current_user.name or current_user.username or "未知用户"),
                "module": "shell-template",
                "target_count": executed_count,
                "duration_ms": duration_ms,
                "ok": ok,
                "code": first_nonzero_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "command_preview": command_preview,
                "notice": notice,
            },
        )
        _append_audit_log(
            db,
            request,
            current_user,
            resource=f"/api/tools/templates/run?source={run_source}&kind=shell&targets={executed_count}",
            result="success" if ok else "failed",
        )
        db.commit()
        return AnsibleRunResponse(
            ok=ok,
            run_id=run_id,
            module="shell",
            target_count=executed_count,
            duration_ms=duration_ms,
            code=first_nonzero_code,
            stdout=stdout_text,
            stderr=stderr_text,
            command_preview=command_preview,
            notice=notice,
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Shell 模板执行失败：{str(exc)}")


def _execute_ansible_task(
    payload: AnsibleRunRequest,
    request: Request,
    db: Session,
    current_user: User,
    run_source: str = "manual",
) -> AnsibleRunResponse:
    """执行一次 Ansible ad-hoc 任务（公共执行器）。"""
    config, _ = _read_ansible_config(db)
    if not config.get("enabled"):
        raise HTTPException(status_code=400, detail="Ansible 执行入口已被禁用，请先在配置中开启。")

    module_name = str(payload.module or "").strip().lower()
    if module_name not in ALLOWED_ANSIBLE_MODULES:
        raise HTTPException(
            status_code=400,
            detail=f"模块不在白名单中，当前仅支持：{', '.join(sorted(ALLOWED_ANSIBLE_MODULES))}",
        )

    selected_assets = db.query(Asset).all()
    if payload.run_all:
        target_assets = selected_assets
    else:
        selected_ids = {
            str(item).strip()
            for item in payload.asset_ids
            if str(item).strip()
        }
        if not selected_ids:
            raise HTTPException(status_code=400, detail="请选择至少一个目标主机。")
        target_assets = [item for item in selected_assets if _asset_identity(item) in selected_ids]

    if not target_assets:
        raise HTTPException(status_code=404, detail="未找到可执行的目标主机。")

    credential_refs = {
        str(item.credential_ref or "").strip()
        for item in target_assets
        if str(item.credential_ref or "").strip()
    }
    credential_rows = (
        db.query(Credential).filter(Credential.cred_id.in_(list(credential_refs))).all()
        if credential_refs
        else []
    )
    denied_refs = [
        str(item.cred_id)
        for item in credential_rows
        if not credential_visible_to_user(item, current_user)
    ]
    if denied_refs:
        raise HTTPException(status_code=403, detail="当前账号无权使用部分目标主机的 SSH 凭证")
    credential_rows = filter_credentials_for_user(credential_rows, current_user)
    credential_map = {str(item.cred_id): item for item in credential_rows}

    temp_dir = Path(tempfile.mkdtemp(prefix="opspilot_ansible_"))
    run_id = f"ANS-{uuid.uuid4().hex[:8].upper()}"
    started_at = time.perf_counter()
    try:
        hosts_payload: dict[str, dict[str, Any]] = {}
        skipped_assets: list[str] = []
        for asset in target_assets:
            private_key, password, _ = _resolve_asset_auth(asset, credential_map)
            if not private_key and not password:
                skipped_assets.append(str(asset.name or asset.instance_id or "unknown"))
                continue

            host_alias = f"asset_{asset.id}"
            host_vars: dict[str, Any] = {
                "ansible_host": str(asset.ip or ""),
                "ansible_port": int(asset.ssh_port or 22),
                "ansible_user": str(asset.ssh_user or "root"),
            }
            if private_key:
                key_file = temp_dir / f"{host_alias}.key"
                key_file.write_text(private_key, encoding="utf-8")
                try:
                    os.chmod(key_file, 0o600)
                except Exception:
                    # Windows 环境可能不支持 chmod 语义，这里忽略不影响功能。
                    pass
                host_vars["ansible_ssh_private_key_file"] = str(key_file)
            elif password:
                host_vars["ansible_password"] = password
                host_vars["ansible_ssh_pass"] = password
            hosts_payload[host_alias] = host_vars

        if not hosts_payload:
            raise HTTPException(status_code=400, detail="选中主机均未配置可用 SSH 认证。")

        inventory_path = temp_dir / "inventory.json"
        inventory_path.write_text(
            json.dumps({"all": {"hosts": hosts_payload}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        args = [
            str(config["executable"]),
            "all",
            "-i",
            str(inventory_path),
            "-m",
            module_name,
            "-o",
            "-f",
            str(config["forks"]),
        ]
        module_args = str(payload.module_args or "").strip()
        if module_args:
            args.extend(["-a", module_args])
        if payload.become:
            args.append("-b")
            become_user = str(payload.become_user or "").strip()
            if become_user:
                args.extend(["--become-user", become_user])

        execution = _run_ansible_command(
            args=args,
            timeout_seconds=int(config["timeout_seconds"]),
            host_key_checking=bool(config["host_key_checking"]),
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        notice_parts: list[str] = []
        if skipped_assets:
            notice_parts.append(f"跳过 {len(skipped_assets)} 台未配置 SSH 认证的主机")
        if execution["ok"]:
            notice_parts.append("Ansible 执行完成")
        else:
            notice_parts.append("Ansible 执行失败")
        notice = "，".join(notice_parts)
        command_preview = " ".join(args)

        history_item = {
            "run_id": run_id,
            "created_at": _now_str(),
            "user": str(current_user.name or current_user.username or "未知用户"),
            "module": module_name,
            "target_count": len(hosts_payload),
            "duration_ms": duration_ms,
            "ok": bool(execution["ok"]),
            "code": int(execution["code"]),
            "stdout": str(execution["stdout"] or ""),
            "stderr": str(execution["stderr"] or ""),
            "command_preview": command_preview,
            "notice": notice,
        }
        _append_ansible_history(db, history_item)
        _append_audit_log(
            db,
            request,
            current_user,
            resource=(
                f"/api/tools/ansible/run?source={run_source}&module={module_name}&targets={len(hosts_payload)}"
            ),
            result="success" if execution["ok"] else "failed",
        )
        db.commit()
        return AnsibleRunResponse(
            ok=bool(execution["ok"]),
            run_id=run_id,
            module=module_name,
            target_count=len(hosts_payload),
            duration_ms=duration_ms,
            code=int(execution["code"]),
            stdout=str(execution["stdout"] or ""),
            stderr=str(execution["stderr"] or ""),
            command_preview=command_preview,
            notice=notice,
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ansible 执行失败：{str(exc)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post(
    "/ansible/run",
    response_model=AnsibleRunResponse,
    dependencies=[Depends(require_permissions("action.tools.scripts.execute")), Depends(require_risk_ack("action.tools.scripts.execute", "execute"))],
)
def run_ansible_task(
    payload: AnsibleRunRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnsibleRunResponse:
    """执行一次 Ansible ad-hoc 任务。"""
    return _execute_ansible_task(
        payload=payload,
        request=request,
        db=db,
        current_user=current_user,
        run_source="manual",
    )


@router.post(
    "/ansible/run-playbook",
    response_model=AnsibleRunResponse,
    dependencies=[Depends(require_permissions("action.tools.scripts.execute")), Depends(require_risk_ack("action.tools.scripts.execute", "execute"))],
)
def run_ansible_playbook(
    payload: AnsiblePlaybookRunRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnsibleRunResponse:
    """执行用户提交的 Ansible Playbook 文本。"""
    config, _ = _read_ansible_config(db)
    if not config.get("enabled"):
        raise HTTPException(status_code=400, detail="Ansible 执行入口已被禁用，请先在配置中开启。")

    playbook_text = str(payload.playbook or "").strip()
    if not playbook_text:
        raise HTTPException(status_code=400, detail="Playbook 内容不能为空。")
    if "\x00" in playbook_text:
        raise HTTPException(status_code=400, detail="Playbook 内容包含非法字符。")

    selected_assets = db.query(Asset).all()
    if payload.run_all:
        target_assets = selected_assets
    else:
        selected_ids = {str(item).strip() for item in payload.asset_ids if str(item).strip()}
        if not selected_ids:
            raise HTTPException(status_code=400, detail="请选择至少一个目标主机。")
        target_assets = [item for item in selected_assets if _asset_identity(item) in selected_ids]
    if not target_assets:
        raise HTTPException(status_code=404, detail="未找到可执行的目标主机。")

    credential_refs = {
        str(item.credential_ref or "").strip()
        for item in target_assets
        if str(item.credential_ref or "").strip()
    }
    credential_rows = (
        db.query(Credential).filter(Credential.cred_id.in_(list(credential_refs))).all()
        if credential_refs
        else []
    )
    if any(not credential_visible_to_user(item, current_user) for item in credential_rows):
        raise HTTPException(status_code=403, detail="当前账号无权使用部分目标主机的 SSH 凭证")
    credential_map = {str(item.cred_id): item for item in filter_credentials_for_user(credential_rows, current_user)}

    temp_dir = Path(tempfile.mkdtemp(prefix="opspilot_ansible_playbook_"))
    run_id = f"ANS-{uuid.uuid4().hex[:8].upper()}"
    started_at = time.perf_counter()
    try:
        hosts_payload: dict[str, dict[str, Any]] = {}
        skipped_assets: list[str] = []
        for asset in target_assets:
            private_key, password, _ = _resolve_asset_auth(asset, credential_map)
            if not private_key and not password:
                skipped_assets.append(str(asset.name or asset.instance_id or "unknown"))
                continue

            host_alias = f"asset_{asset.id}"
            host_vars: dict[str, Any] = {
                "ansible_host": str(asset.ip or ""),
                "ansible_port": int(asset.ssh_port or 22),
                "ansible_user": str(asset.ssh_user or "root"),
            }
            if private_key:
                key_file = temp_dir / f"{host_alias}.key"
                key_file.write_text(private_key, encoding="utf-8")
                try:
                    os.chmod(key_file, 0o600)
                except Exception:
                    pass
                host_vars["ansible_ssh_private_key_file"] = str(key_file)
            elif password:
                host_vars["ansible_password"] = password
                host_vars["ansible_ssh_pass"] = password
            hosts_payload[host_alias] = host_vars

        if not hosts_payload:
            raise HTTPException(status_code=400, detail="选中主机均未配置可用 SSH 认证。")

        inventory_path = temp_dir / "inventory.json"
        inventory_path.write_text(
            json.dumps({"all": {"hosts": hosts_payload}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        playbook_path = temp_dir / "playbook.yml"
        playbook_path.write_text(playbook_text, encoding="utf-8")

        args = [
            _resolve_ansible_playbook_executable(str(config["executable"])),
            "-i",
            str(inventory_path),
            str(playbook_path),
            "-f",
            str(config["forks"]),
        ]
        execution = _run_ansible_command(
            args=args,
            timeout_seconds=int(config["timeout_seconds"]),
            host_key_checking=bool(config["host_key_checking"]),
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        notice_parts: list[str] = []
        if skipped_assets:
            notice_parts.append(f"跳过 {len(skipped_assets)} 台未配置 SSH 认证的主机")
        notice_parts.append("Playbook 执行完成" if execution["ok"] else "Playbook 执行失败")
        notice = "，".join(notice_parts)
        command_preview = " ".join(args)

        _append_ansible_history(
            db,
            {
                "run_id": run_id,
                "created_at": _now_str(),
                "user": str(current_user.name or current_user.username or "未知用户"),
                "module": "playbook",
                "target_count": len(hosts_payload),
                "duration_ms": duration_ms,
                "ok": bool(execution["ok"]),
                "code": int(execution["code"]),
                "stdout": str(execution["stdout"] or ""),
                "stderr": str(execution["stderr"] or ""),
                "command_preview": command_preview,
                "notice": notice,
            },
        )
        _append_audit_log(
            db,
            request,
            current_user,
            resource=f"/api/tools/ansible/run-playbook?targets={len(hosts_payload)}",
            result="success" if execution["ok"] else "failed",
        )
        db.commit()
        return AnsibleRunResponse(
            ok=bool(execution["ok"]),
            run_id=run_id,
            module="playbook",
            target_count=len(hosts_payload),
            duration_ms=duration_ms,
            code=int(execution["code"]),
            stdout=str(execution["stdout"] or ""),
            stderr=str(execution["stderr"] or ""),
            command_preview=command_preview,
            notice=notice,
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Playbook 执行失败：{str(exc)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post(
    "/ansible/run-template",
    response_model=AnsibleRunResponse,
    dependencies=[Depends(require_permissions("action.tools.scripts.execute")), Depends(require_risk_ack("action.tools.scripts.execute", "execute"))],
)
def run_ansible_template(
    payload: AnsibleTemplateRunRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnsibleRunResponse:
    """执行模板。"""
    template_id = str(payload.template_id or "").strip()
    items = _read_ansible_templates(db)
    index = _find_template_index(items, template_id)
    if index < 0:
        raise HTTPException(status_code=404, detail="模板不存在或已删除。")

    template = _normalize_template_row(items[index])
    if not _template_visible_to_user(template, current_user):
        raise HTTPException(status_code=403, detail="当前权限组无权使用该模板。")
    run_all = template["run_all"] if payload.run_all_override is None else bool(payload.run_all_override)
    asset_ids = template["asset_ids"]
    if not run_all and payload.asset_ids_override:
        asset_ids = _normalize_asset_ids(payload.asset_ids_override)

    if str(template.get("kind") or "ansible") == "shell":
        return _execute_shell_template(
            command=str(template["module_args"]),
            run_all=run_all,
            asset_ids=asset_ids,
            request=request,
            db=db,
            current_user=current_user,
            run_source=f"template:{template_id}",
        )
    if str(template.get("kind") or "ansible") == "playbook":
        return run_ansible_playbook(
            payload=AnsiblePlaybookRunRequest(
                run_all=run_all,
                asset_ids=asset_ids,
                playbook=str(template["module_args"]),
            ),
            request=request,
            db=db,
            current_user=current_user,
        )

    return _execute_ansible_task(
        payload=AnsibleRunRequest(
            run_all=run_all,
            asset_ids=asset_ids,
            module=str(template["module"]),
            module_args=str(template["module_args"]),
            become=bool(template["become"]),
            become_user=str(template["become_user"]),
        ),
        request=request,
        db=db,
        current_user=current_user,
        run_source=f"template:{template_id}",
    )
