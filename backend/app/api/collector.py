"""
Alloy Collector Fleet API。

控制台接口使用用户登录态；Agent 接口使用独立 Agent Token，避免把
浏览器 JWT 扩散到主机侧采集器。
"""
from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.monitoring import read_monitoring_config
from app.api.monitoring.constants import ALLOY_DEFAULT_POLICY_PREFIX
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.audit import AuditLog
from app.models.collector import (
    CollectorAgent,
    CollectorConfigVersion,
    CollectorGroup,
    CollectorPolicy,
    CollectorRegistrationToken,
    CollectorRelease,
)
from app.models.user import User
from app.services.collector_bootstrap import render_alloy_install_script
from app.services.collector_config import (
    CollectorConfigError,
    config_hash,
    render_alloy_config,
    validate_label_key,
    wrap_alloy_config_as_module,
)

router = APIRouter(
    dependencies=[
        Depends(get_current_user),
        Depends(require_permissions("module.monitoring.collector")),
    ]
)
agent_router = APIRouter()


def now_ts() -> str:
    """返回统一字符串时间，沿用现有接口风格。"""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def hash_token(raw_token: str) -> str:
    """计算 Token 哈希。数据库只保存哈希，不保存明文。"""
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


def verify_token(raw_token: str, token_hash: str) -> bool:
    """使用常量时间比较，降低时序侧信道风险。"""
    return hmac.compare_digest(hash_token(raw_token), str(token_hash or ""))


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _load_json_dict(raw_value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw_value or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_json_dict(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _append_collector_audit_log(
    db: Session,
    *,
    username: str,
    action: str,
    resource: str,
    result: str = "success",
) -> None:
    """记录 Collector 控制面关键操作，避免采集策略变更无审计。"""
    db.add(
        AuditLog(
            user=(username or "未知用户")[:100],
            action=action[:100],
            resource=resource[:255],
            ip="-",
            result=result[:20],
        )
    )


class RegisterTokenCreateRequest(BaseModel):
    """一次性注册令牌创建请求。"""

    description: str = ""
    expire_minutes: int = Field(default=60, ge=5, le=1440)


class RegisterTokenCreateResponse(BaseModel):
    """一次性注册令牌响应。明文 token 只返回这一次。"""

    token_id: str
    token: str
    expires_at: str


class AgentRegisterRequest(BaseModel):
    """Agent 首次注册请求。"""

    register_token: str
    agent_id: str = ""
    hostname: str = ""
    ip: str = ""
    os_type: str = "linux"
    labels: dict[str, str] = Field(default_factory=dict)
    asset_id: str = ""


class AgentRegisterResponse(BaseModel):
    """Agent 注册响应。明文 agent_token 只返回这一次。"""

    agent_id: str
    agent_token: str


class AgentHeartbeatRequest(BaseModel):
    """Agent 心跳请求。"""

    status: str = "online"
    current_version_id: str = ""
    last_error: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class AgentAckRequest(BaseModel):
    """Agent 配置加载确认。"""

    version_id: str
    ok: bool
    message: str = ""


class CollectorPolicyRequest(BaseModel):
    """控制台采集策略请求。"""

    name: str
    scope_type: str = "global"
    scope_ref: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    logs: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    publish: bool = False


class PublishPolicyRequest(BaseModel):
    """策略发布请求。"""

    target_type: str = "agent"
    target_ref: str = ""


class RollbackRequest(BaseModel):
    """发布回滚请求。"""

    version_id: str = ""


class CollectorGroupRequest(BaseModel):
    """Agent 分组请求，使用简单标签等值选择器。"""

    name: str
    selector: dict[str, str] = Field(default_factory=dict)
    description: str = ""


def _serialize_agent(
    row: CollectorAgent,
    *,
    groups: list[CollectorGroup] | None = None,
    policy_by_version: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """序列化 Agent，并补充控制台展示需要的分组和生效策略。"""
    matched_groups = _matching_group_summaries(row, groups or [])
    effective_policy = (policy_by_version or {}).get(row.current_version_id, {})
    status = row.status or "offline"
    # 心跳新鲜度兜底：online 但超过 180 秒无心跳 → 展示为 offline。
    # 防止失败安装残留的 agent 记录 / 进程死亡后状态悬挂在 online 误导排障。
    # last_seen 由 collector.now_ts() 写入，为 UTC ISO+Z 格式（历史行可能是本地时间串）。
    if status == "online":
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz

            seen = _dt.fromisoformat((row.last_seen_at or "").replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.astimezone()
            if _dt.now(_tz.utc) - seen > _td(seconds=180):
                status = "offline"
        except (ValueError, TypeError):
            status = "offline"
    return {
        "agent_id": row.agent_id,
        "asset_id": row.asset_id,
        "hostname": row.hostname,
        "ip": row.ip,
        "os_type": row.os_type,
        "labels": _load_json_dict(row.labels_json),
        "groups": matched_groups,
        "effective_policy": effective_policy,
        "status": status,
        "current_version_id": row.current_version_id,
        "last_seen_at": row.last_seen_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_policy(row: CollectorPolicy) -> dict[str, Any]:
    return {
        "policy_id": row.policy_id,
        "name": row.name,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "metrics": _load_json_dict(row.metrics_json),
        "logs": _load_json_dict(row.logs_json),
        "labels": _load_json_dict(row.labels_json),
        "redaction": _load_json_dict(row.redaction_json),
        "enabled": row.enabled == "true",
        "is_default": str(row.policy_id or "").startswith(ALLOY_DEFAULT_POLICY_PREFIX),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_group(row: CollectorGroup) -> dict[str, Any]:
    return {
        "group_id": row.group_id,
        "name": row.name,
        "selector": _load_json_dict(row.selector_json),
        "description": row.description,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _normalize_group_selector(selector: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in (selector or {}).items():
        try:
            key = validate_label_key(str(raw_key))
        except CollectorConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        value = str(raw_value or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"分组标签 {key} 的匹配值不能为空")
        if len(value) > 128:
            raise HTTPException(status_code=400, detail=f"分组标签 {key} 的匹配值不能超过 128 字符")
        normalized[key] = value
    if not normalized:
        raise HTTPException(status_code=400, detail="分组至少需要一个标签选择条件")
    return dict(sorted(normalized.items()))


def _get_agent_from_headers(
    db: Session,
    agent_id: str,
    agent_token: str,
) -> CollectorAgent:
    row = db.query(CollectorAgent).filter(CollectorAgent.agent_id == agent_id).first()
    if not row or not verify_token(agent_token, row.token_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent Token 非法或已失效",
        )
    if row.status == "disabled":
        raise HTTPException(status_code=403, detail="Agent 已禁用")
    return row


def _current_agent(
    x_opspilot_agent_id: str = Header(default="", alias="X-OpsPilot-Agent-Id"),
    x_opspilot_agent_token: str = Header(default="", alias="X-OpsPilot-Agent-Token"),
    db: Session = Depends(get_db),
) -> CollectorAgent:
    if not x_opspilot_agent_id or not x_opspilot_agent_token:
        raise HTTPException(status_code=401, detail="缺少 Agent 身份头")
    return _get_agent_from_headers(db, x_opspilot_agent_id, x_opspilot_agent_token)


@router.get("/agents")
def list_agents(db: Session = Depends(get_db)) -> dict[str, Any]:
    """查询 Collector Agent 列表。"""
    rows = db.query(CollectorAgent).order_by(CollectorAgent.id.desc()).all()
    groups = db.query(CollectorGroup).order_by(CollectorGroup.id.asc()).all()
    policy_by_version = _policy_summaries_by_version(db, [row.current_version_id for row in rows])
    return {"agents": [_serialize_agent(row, groups=groups, policy_by_version=policy_by_version) for row in rows]}


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """从纳管列表中删除一个 Collector Agent。

    仅清理 OpsPilot 侧的 agent 记录(心跳/状态/Token),不会远程卸载目标机上的 alloy 进程;
    若该机器的 alloy 仍在运行,会在下次注册时重新出现。删除是可逆的运维动作。
    """
    row = db.query(CollectorAgent).filter(CollectorAgent.agent_id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    db.delete(row)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="删除 Collector Agent",
        resource=f"agent {agent_id}",
    )
    db.commit()
    return {"ok": True, "agent_id": agent_id}



@router.post("/agents/register-token", response_model=RegisterTokenCreateResponse)
def create_register_token(
    payload: RegisterTokenCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> RegisterTokenCreateResponse:
    """生成一次性 Agent 注册令牌。"""
    token_id = _new_id("rtok")
    raw_token = f"opspilot_reg_{secrets.token_urlsafe(32)}"
    expires_at = (datetime.utcnow() + timedelta(minutes=payload.expire_minutes)).isoformat(timespec="seconds") + "Z"
    db.add(
        CollectorRegistrationToken(
            token_id=token_id,
            token_hash=hash_token(raw_token),
            description=payload.description[:255],
            expires_at=expires_at,
            used_at="",
            created_by=current_user.username,
            created_at=now_ts(),
        )
    )
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="生成 Alloy Agent 注册令牌",
        resource=f"token {token_id}",
    )
    db.commit()
    return RegisterTokenCreateResponse(token_id=token_id, token=raw_token, expires_at=expires_at)


@router.get("/groups")
def list_groups(db: Session = Depends(get_db)) -> dict[str, Any]:
    """查询 Agent 分组。"""
    rows = db.query(CollectorGroup).order_by(CollectorGroup.id.desc()).all()
    return {"groups": [_serialize_group(row) for row in rows]}


@router.post("/groups")
def create_group(
    payload: CollectorGroupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """创建 Agent 分组。"""
    group_id = _new_id("grp")
    now = now_ts()
    row = CollectorGroup(
        group_id=group_id,
        name=payload.name[:120],
        selector_json=_dump_json_dict(_normalize_group_selector(payload.selector)),
        description=payload.description[:500],
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="创建 Alloy Agent 分组",
        resource=f"group {group_id}",
    )
    db.commit()
    return {"group": _serialize_group(row)}


@router.put("/groups/{group_id}")
def update_group(
    group_id: str,
    payload: CollectorGroupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """更新 Agent 分组。"""
    row = db.query(CollectorGroup).filter(CollectorGroup.group_id == group_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Agent 分组不存在")
    row.name = payload.name[:120]
    row.selector_json = _dump_json_dict(_normalize_group_selector(payload.selector))
    row.description = payload.description[:500]
    row.updated_at = now_ts()
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="更新 Alloy Agent 分组",
        resource=f"group {group_id}",
    )
    db.commit()
    return {"group": _serialize_group(row)}


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """删除 Agent 分组；Agent 本身只按标签动态匹配，不需要清理关联表。"""
    row = db.query(CollectorGroup).filter(CollectorGroup.group_id == group_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Agent 分组不存在")
    db.delete(row)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="删除 Alloy Agent 分组",
        resource=f"group {group_id}",
    )
    db.commit()
    return {"ok": True, "group_id": group_id}


@agent_router.post("/register", response_model=AgentRegisterResponse)
def register_agent(payload: AgentRegisterRequest, db: Session = Depends(get_db)) -> AgentRegisterResponse:
    """Agent 使用一次性注册令牌换取长期 Token。"""
    token_row = (
        db.query(CollectorRegistrationToken)
        .filter(CollectorRegistrationToken.used_at == "")
        .all()
    )
    matched = next((row for row in token_row if verify_token(payload.register_token, row.token_hash)), None)
    if not matched:
        raise HTTPException(status_code=401, detail="注册令牌非法或已使用")
    if matched.expires_at and matched.expires_at < now_ts():
        raise HTTPException(status_code=401, detail="注册令牌已过期")

    agent_id = (payload.agent_id or _new_id("agent")).strip()
    raw_agent_token = f"opspilot_agent_{secrets.token_urlsafe(32)}"
    now = now_ts()
    # 同一资产重装/换 ID 重注册时，清理同 asset_id 的其它旧记录，避免 fleet 里僵尸堆积
    # （一台主机一个有效 agent；旧记录的 token 已随安装脚本重写，保留只会误导排障）
    stale_rows = (
        db.query(CollectorAgent)
        .filter(CollectorAgent.asset_id == (payload.asset_id or "").strip()[:50])
        .filter(CollectorAgent.agent_id != agent_id)
        .all()
    )
    for stale in stale_rows:
        db.delete(stale)
    existing = db.query(CollectorAgent).filter(CollectorAgent.agent_id == agent_id).first()
    if existing:
        existing.token_hash = hash_token(raw_agent_token)
        existing.hostname = payload.hostname[:120]
        existing.ip = payload.ip[:64]
        existing.os_type = payload.os_type[:30] or "linux"
        existing.labels_json = _dump_json_dict(payload.labels)
        existing.asset_id = payload.asset_id[:50]
        existing.status = "online"
        existing.last_seen_at = now
        existing.updated_at = now
    else:
        db.add(
            CollectorAgent(
                agent_id=agent_id,
                asset_id=payload.asset_id[:50],
                hostname=payload.hostname[:120],
                ip=payload.ip[:64],
                os_type=payload.os_type[:30] or "linux",
                labels_json=_dump_json_dict(payload.labels),
                status="online",
                token_hash=hash_token(raw_agent_token),
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    matched.used_at = now
    db.commit()
    return AgentRegisterResponse(agent_id=agent_id, agent_token=raw_agent_token)


@agent_router.get("/install-script")
def get_agent_install_script() -> Response:
    """返回不含密钥的 Linux Alloy Agent 安装脚本。"""
    return Response(
        content=render_alloy_install_script(),
        media_type="text/x-shellscript; charset=utf-8",
    )


@agent_router.get("/binary")
def get_agent_binary(arch: str = "amd64") -> Response:
    """流式返回本机 Alloy 二进制的 gzip（局域网快速通道）。

    与 install-script 同属免鉴权 agent_router；二进制非机密。
    后端镜像无二进制或架构不符时返回 404，安装脚本据此回退到 apt/yum。
    """
    from app.services.collector_bootstrap import (
        build_alloy_binary_gzip,
        resolve_alloy_binary_for_arch,
    )

    path = resolve_alloy_binary_for_arch(arch)
    if not path:
        raise HTTPException(
            status_code=404,
            detail="当前后端环境无匹配架构的 Alloy 二进制，请使用软件包安装",
        )
    gz_path = build_alloy_binary_gzip(path)
    return FileResponse(
        gz_path,
        media_type="application/gzip",
        filename="alloy.gz",
    )


@agent_router.get("/config")
def get_agent_config(
    agent: CollectorAgent = Depends(_current_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Agent 拉取当前发布配置。"""
    if not agent.current_version_id:
        return {"version_id": "", "config_hash": "", "alloy_config": ""}
    version = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.version_id == agent.current_version_id)
        .first()
    )
    if not version:
        return {"version_id": "", "config_hash": "", "alloy_config": ""}
    return {
        "version_id": version.version_id,
        "config_hash": version.config_hash,
        "alloy_config": version.alloy_config,
    }


@agent_router.get("/module")
def get_agent_module(
    agent: CollectorAgent = Depends(_current_agent),
    db: Session = Depends(get_db),
) -> Response:
    """返回 Alloy `import.http` 可加载的远程 module。

    该接口替代旧 `opspilot-alloy-sync` 拉配置脚本：Alloy 自己定期请求本接口，
    请求成功即可视为 Agent 在线。没有已发布版本时返回空 module，保证 Alloy
    启动不会因为配置尚未发布而失败。
    """
    now = now_ts()
    agent.status = "online"
    agent.last_seen_at = now
    agent.updated_at = now
    body = 'declare "main" {\n}\n'
    if agent.current_version_id:
        version = (
            db.query(CollectorConfigVersion)
            .filter(CollectorConfigVersion.version_id == agent.current_version_id)
            .first()
        )
        if version:
            body = wrap_alloy_config_as_module(version.alloy_config)
            agent.last_error = ""
    db.commit()
    return Response(content=body, media_type="text/plain; charset=utf-8")


@agent_router.post("/heartbeat")
def heartbeat_agent(
    payload: AgentHeartbeatRequest,
    agent: CollectorAgent = Depends(_current_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Agent 上报在线状态。"""
    agent.status = payload.status[:20] or "online"
    # 心跳不应无条件覆盖 current_version_id：
    # 安装后的同步服务会高频上报在线状态，配置版本只应由发布动作或 ACK 更新，
    # 否则会出现“刚发布就被下一次心跳清空”的竞态。
    if payload.current_version_id:
        agent.current_version_id = payload.current_version_id[:50]
    agent.last_error = payload.last_error[:1000]
    if payload.labels:
        agent.labels_json = _dump_json_dict(payload.labels)
    agent.last_seen_at = now_ts()
    agent.updated_at = agent.last_seen_at
    db.commit()
    return {"ok": True}


@agent_router.post("/ack")
def ack_agent_config(
    payload: AgentAckRequest,
    agent: CollectorAgent = Depends(_current_agent),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Agent 确认配置加载结果。"""
    agent.current_version_id = payload.version_id[:50]
    agent.last_error = "" if payload.ok else payload.message[:1000]
    agent.status = "online" if payload.ok else "error"
    agent.last_seen_at = now_ts()
    agent.updated_at = agent.last_seen_at
    db.commit()
    return {"ok": True}


@router.get("/policies")
def list_policies(db: Session = Depends(get_db)) -> dict[str, Any]:
    """查询采集策略。"""
    rows = db.query(CollectorPolicy).order_by(CollectorPolicy.id.desc()).all()
    return {"policies": [_serialize_policy(row) for row in rows]}


@router.post("/policies")
def create_policy(
    payload: CollectorPolicyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """创建采集策略并生成草稿配置版本。"""
    policy_id = _new_id("pol")
    now = now_ts()
    row = CollectorPolicy(
        policy_id=policy_id,
        name=payload.name[:120],
        scope_type=payload.scope_type[:20],
        scope_ref=payload.scope_ref[:80],
        metrics_json=_dump_json_dict(payload.metrics),
        logs_json=_dump_json_dict(payload.logs),
        labels_json=_dump_json_dict(payload.labels),
        redaction_json=_dump_json_dict(payload.redaction),
        enabled="true" if payload.enabled else "false",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    version = _create_config_version(db, row, current_user.username)
    db.flush()
    release = None
    if payload.publish:
        release = _publish_version_to_policy_scope(db, row, version, current_user.username)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="创建采集策略" + ("并发布" if payload.publish else ""),
        resource=f"policy {policy_id}",
    )
    db.commit()
    result = {"policy": _serialize_policy(row), "version": _serialize_version(version)}
    if release is not None:
        result["release"] = _serialize_release(release)
    return result


@router.put("/policies/{policy_id}")
def update_policy(
    policy_id: str,
    payload: CollectorPolicyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """更新采集策略并生成新的草稿版本。"""
    row = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="采集策略不存在")
    row.name = payload.name[:120]
    row.scope_type = payload.scope_type[:20]
    row.scope_ref = payload.scope_ref[:80]
    row.metrics_json = _dump_json_dict(payload.metrics)
    row.logs_json = _dump_json_dict(payload.logs)
    row.labels_json = _dump_json_dict(payload.labels)
    row.redaction_json = _dump_json_dict(payload.redaction)
    row.enabled = "true" if payload.enabled else "false"
    row.updated_at = now_ts()
    version = _create_config_version(db, row, current_user.username)
    db.flush()
    release = None
    if payload.publish:
        release = _publish_version_to_policy_scope(db, row, version, current_user.username)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="更新采集策略" + ("并发布" if payload.publish else ""),
        resource=f"policy {policy_id}",
    )
    db.commit()
    result = {"policy": _serialize_policy(row), "version": _serialize_version(version)}
    if release is not None:
        result["release"] = _serialize_release(release)
    return result


@router.delete("/policies/{policy_id}")
def delete_policy(
    policy_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """删除采集策略（方案A：占用保护 + 级联清理）。

    若仍有 Agent 正在使用该策略的某个配置版本(current_version_id 命中)，
    则拒绝删除并返回占用该策略的 Agent，提示先把这些 Agent 切到其他策略；
    无占用时连同该策略的所有配置版本与发布记录一并清理。删除是可逆运维动作。
    """
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="采集策略不存在")

    versions = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.policy_id == policy_id)
        .all()
    )
    version_ids = {v.version_id for v in versions}

    # 占用保护：任一版本仍被 Agent 引用则拦截
    if version_ids:
        in_use_agents = (
            db.query(CollectorAgent)
            .filter(CollectorAgent.current_version_id.in_(version_ids))
            .all()
        )
        if in_use_agents:
            blockers = [
                {"agent_id": a.agent_id, "hostname": a.hostname, "ip": a.ip}
                for a in in_use_agents
            ]
            names = "、".join(a["hostname"] or a["agent_id"] for a in blockers[:5])
            extra = f" 等 {len(blockers)} 个" if len(blockers) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"策略仍被 {names}{extra} Agent 使用，请先把这些 Agent 切换到其他策略再删除。",
                    "agents": blockers,
                },
            )

    # 级联清理：先删该策略所有版本派生的发布记录，再删版本，最后删策略
    if version_ids:
        db.query(CollectorRelease).filter(
            CollectorRelease.version_id.in_(version_ids)
        ).delete(synchronize_session=False)
        db.query(CollectorConfigVersion).filter(
            CollectorConfigVersion.policy_id == policy_id
        ).delete(synchronize_session=False)
    db.delete(policy)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="删除采集策略",
        resource=f"policy {policy_id}",
    )
    db.commit()
    return {"ok": True, "policy_id": policy_id, "removed_versions": len(version_ids)}


@router.get("/policies/{policy_id}/versions")
def list_policy_versions(policy_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """查询指定策略的配置版本，按最新版本优先返回。"""
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="采集策略不存在")
    rows = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.policy_id == policy_id)
        .order_by(CollectorConfigVersion.version.desc())
        .all()
    )
    return {"policy": _serialize_policy(policy), "versions": [_serialize_version(row) for row in rows]}


@router.post("/policies/{policy_id}/publish")
def publish_policy(
    policy_id: str,
    payload: PublishPolicyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """发布最新策略版本到 Agent 或分组。"""
    version = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.policy_id == policy_id)
        .order_by(CollectorConfigVersion.version.desc())
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="配置版本不存在")
    target_ref = payload.target_ref or _policy_target_ref(db, policy_id)
    release = CollectorRelease(
        release_id=_new_id("rel"),
        version_id=version.version_id,
        target_type=payload.target_type[:20],
        target_ref=target_ref[:80],
        status="stable",
        started_at=now_ts(),
        finished_at=now_ts(),
        summary_json=_dump_json_dict({"success": 0, "failed": 0, "skipped": 0}),
    )
    version.status = "published"
    db.add(release)
    _apply_release_to_agents(db, payload.target_type, target_ref, version.version_id)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="发布采集策略",
        resource=f"policy {policy_id} version {version.version_id} -> {payload.target_type}:{target_ref}",
    )
    db.commit()
    return {"release": _serialize_release(release), "version": _serialize_version(version)}


@router.get("/releases")
def list_releases(db: Session = Depends(get_db)) -> dict[str, Any]:
    """查询采集策略发布记录，并带出版本所属策略摘要。"""
    rows = db.query(CollectorRelease).order_by(CollectorRelease.id.desc()).all()
    version_ids = [row.version_id for row in rows]
    policy_by_version = _policy_summaries_by_version(db, version_ids)
    return {"releases": [_serialize_release_with_policy(row, policy_by_version) for row in rows]}


@router.post("/releases/{release_id}/rollback")
def rollback_release(
    release_id: str,
    payload: RollbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.monitoring.collector.manage")),
) -> dict[str, Any]:
    """将发布目标回滚到指定版本或清空当前版本。"""
    release = db.query(CollectorRelease).filter(CollectorRelease.release_id == release_id).first()
    if not release:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    release.status = "rolled_back"
    release.finished_at = now_ts()
    target_version_id = payload.version_id[:50]
    _apply_release_to_agents(db, release.target_type, release.target_ref, target_version_id)
    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="回滚采集发布",
        resource=f"release {release_id} -> version {target_version_id or 'empty'}",
    )
    db.commit()
    return {"release": _serialize_release(release), "target_version_id": target_version_id}


@router.get("/config-versions/{version_id}/diff")
def diff_config_version(version_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """返回指定版本与上一版本的配置差异。"""
    current = db.query(CollectorConfigVersion).filter(CollectorConfigVersion.version_id == version_id).first()
    if not current:
        raise HTTPException(status_code=404, detail="配置版本不存在")
    previous = (
        db.query(CollectorConfigVersion)
        .filter(
            CollectorConfigVersion.policy_id == current.policy_id,
            CollectorConfigVersion.version < current.version,
        )
        .order_by(CollectorConfigVersion.version.desc())
        .first()
    )
    before = previous.alloy_config.splitlines(keepends=True) if previous else []
    after = current.alloy_config.splitlines(keepends=True)
    return {
        "version_id": version_id,
        "diff": "".join(difflib.unified_diff(before, after, fromfile="previous", tofile="current")),
    }


def _create_config_version(db: Session, policy: CollectorPolicy, username: str) -> CollectorConfigVersion:
    cfg = read_monitoring_config(db)
    metrics = _load_json_dict(policy.metrics_json)
    logs = _load_json_dict(policy.logs_json)
    labels = _load_json_dict(policy.labels_json)
    try:
        alloy_config = render_alloy_config(
            agent_id=policy.scope_ref or policy.policy_id,
            metrics=metrics,
            logs=logs,
            labels=labels,
            remote_write_url=_resolve_remote_write_url(cfg),
            vlogs_url=str(cfg.get("vlogs_ingest_url") or cfg.get("vlogs_url") or "http://victorialogs:9428"),
        )
    except CollectorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    latest = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.policy_id == policy.policy_id)
        .order_by(CollectorConfigVersion.version.desc())
        .first()
    )
    version_no = int(latest.version if latest else 0) + 1
    row = CollectorConfigVersion(
        version_id=_new_id("cfg"),
        policy_id=policy.policy_id,
        version=version_no,
        config_hash=config_hash(alloy_config),
        alloy_config=alloy_config,
        status="draft",
        created_by=username,
        created_at=now_ts(),
    )
    db.add(row)
    return row


def _serialize_version(row: CollectorConfigVersion) -> dict[str, Any]:
    return {
        "version_id": row.version_id,
        "policy_id": row.policy_id,
        "version": row.version,
        "config_hash": row.config_hash,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def _serialize_release(row: CollectorRelease) -> dict[str, Any]:
    return {
        "release_id": row.release_id,
        "version_id": row.version_id,
        "target_type": row.target_type,
        "target_ref": row.target_ref,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "summary": _load_json_dict(row.summary_json),
    }


def _serialize_release_with_policy(
    row: CollectorRelease,
    policy_by_version: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """序列化发布记录，并补充该版本对应的策略名称。"""
    payload = _serialize_release(row)
    payload["policy"] = policy_by_version.get(row.version_id, {})
    return payload


def _policy_target_ref(db: Session, policy_id: str) -> str:
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    return str(policy.scope_ref if policy else "")


def _publish_version_to_policy_scope(
    db: Session,
    policy: CollectorPolicy,
    version: CollectorConfigVersion,
    username: str,
) -> CollectorRelease:
    """把指定版本按策略自身的 scope 发布到生效 Agent。

    用于"编辑保存即下发"：agent 策略发到 scope_ref 指向的单台 Agent，
    group 策略发到命中分组的 Agent 集合，global 发到全部 Agent。
    """
    target_type = (policy.scope_type or "global").strip() or "global"
    target_ref = (policy.scope_ref or "").strip()
    release = CollectorRelease(
        release_id=_new_id("rel"),
        version_id=version.version_id,
        target_type=target_type[:20],
        target_ref=target_ref[:80],
        status="stable",
        started_at=now_ts(),
        finished_at=now_ts(),
        summary_json=_dump_json_dict({"success": 0, "failed": 0, "skipped": 0}),
    )
    version.status = "published"
    db.add(release)
    _apply_release_to_agents(db, target_type, target_ref, version.version_id)
    return release


def _apply_release_to_agents(db: Session, target_type: str, target_ref: str, version_id: str) -> None:
    if target_type == "agent" and target_ref:
        agents = db.query(CollectorAgent).filter(CollectorAgent.agent_id == target_ref).all()
    elif target_type == "group" and target_ref:
        agents = _agents_matching_group(db, target_ref)
    else:
        agents = db.query(CollectorAgent).all()
    for agent in agents:
        agent.current_version_id = version_id
        agent.updated_at = now_ts()


def _resolve_remote_write_url(cfg: dict[str, Any]) -> str:
    """从监控连接配置推导 Alloy remote_write 入口。"""
    explicit_url = str(cfg.get("remote_write_url") or "").strip()
    if explicit_url:
        return explicit_url
    vm_url = str(cfg.get("vm_url") or "").strip().rstrip("/")
    if vm_url:
        if vm_url.endswith("/api/v1/write"):
            return vm_url
        return f"{vm_url}/api/v1/write"
    return "http://victoriametrics:8428/api/v1/write"


def _agents_matching_group(db: Session, group_id: str) -> list[CollectorAgent]:
    """按分组标签选择 Agent，避免发布误扩散到其他主机。"""
    group = db.query(CollectorGroup).filter(CollectorGroup.group_id == group_id).first()
    if not group:
        return []
    selector = _load_json_dict(group.selector_json)
    agents = db.query(CollectorAgent).all()
    return [agent for agent in agents if _agent_matches_selector(agent, selector)]


def _agent_matches_selector(agent: CollectorAgent, selector: dict[str, Any]) -> bool:
    """判断 Agent 标签是否满足分组选择器。空选择器不匹配任何 Agent。"""
    if not selector:
        return False
    labels = _load_json_dict(agent.labels_json)
    for key, expected in selector.items():
        if str(labels.get(str(key)) or "") != str(expected):
            return False
    return True


def _matching_group_summaries(agent: CollectorAgent, groups: list[CollectorGroup]) -> list[dict[str, str]]:
    """返回 Agent 命中的分组摘要，供主机列表直接展示。"""
    summaries: list[dict[str, str]] = []
    for group in groups:
        if _agent_matches_selector(agent, _load_json_dict(group.selector_json)):
            summaries.append({"group_id": group.group_id, "name": group.name})
    return summaries


def _policy_summaries_by_version(db: Session, version_ids: list[str]) -> dict[str, dict[str, str]]:
    """按版本 ID 返回策略摘要，避免前端根据策略范围自行推断生效策略。"""
    cleaned_ids = sorted({version_id for version_id in version_ids if version_id})
    if not cleaned_ids:
        return {}
    versions = db.query(CollectorConfigVersion).filter(CollectorConfigVersion.version_id.in_(cleaned_ids)).all()
    policy_ids = sorted({version.policy_id for version in versions if version.policy_id})
    policies = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id.in_(policy_ids)).all() if policy_ids else []
    policy_map = {policy.policy_id: policy for policy in policies}
    result: dict[str, dict[str, str]] = {}
    for version in versions:
        policy = policy_map.get(version.policy_id)
        result[version.version_id] = {
            "policy_id": version.policy_id,
            "name": policy.name if policy else version.policy_id,
            "version_id": version.version_id,
        }
    return result
