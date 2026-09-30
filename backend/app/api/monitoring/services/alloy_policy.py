"""Alloy Collector Fleet 策略管理与 Agent 生命周期。

【架构背景】
 OpsPilot 采用"后端策略管控 + 边缘 Agent 拉取"的两级架构：
1. 控制台生成结构化采集策略（metrics、logs、labels）；
2. collector_config.py 把策略渲染为受控 Alloy DSL；
3. 主机侧 Alloy Agent 通过原生 `import.http` 轮询 OpsPilot 远程 module。

【本模块职责】
1. Agent 注册令牌管理：生成带哈希校验的 register_token，写入数据库并返回给安装脚本；
2. 应用作用域解析（_resolve_app_scope_for_asset）：根据资产所属应用决定 Alloy policy 的 label，
   实现按应用分组的精细化权限与告警路由；
3. 策略版本化：每次变更生成新的 CollectorConfigVersion，Agent 通过 version_id 比对决定是否重载，
   避免无意义重启；
4. 批量重同步（resync_alloy_policy_for_asset_ids）：当应用结构变更或资产批量迁移时，
   可一次性为多个资产重新下发策略。
"""
from __future__ import annotations

import json
import re
from typing import Any

import secrets
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.collector import (
    CollectorAgent,
    CollectorConfigVersion,
    CollectorPolicy,
    CollectorRegistrationToken,
)
from app.models.user import User
from app.core.crypto import decrypt_secret
from app.services.collector_config import CollectorConfigError, config_hash, render_alloy_config
from app.api.monitoring.constants import (
    ALLOY_DEFAULT_POLICY_PREFIX,
)
from app.api.assets import now_ts
from app.api.monitoring.services.config_store import read_monitoring_config

def _collector_now_ts() -> str:
    """返回 Collector Fleet 使用的 UTC 时间格式。"""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# 注册令牌只保存哈希，避免数据库泄露后可直接注册 Agent。
# 为什么用 SHA-256 而非 bcrypt？因为这不是用户密码，而是短期一次性令牌，
# SHA-256 足够抵抗彩虹表，且计算速度快，不影响批量注册场景。
def _hash_agent_token(raw_token: str) -> str:
    """注册令牌只保存哈希，避免数据库泄露后可直接注册 Agent。"""
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


def _latest_agent_for_asset(db: Session, asset_id: str) -> CollectorAgent | None:
    """按资产 ID 找到最新注册的 Alloy Agent。"""
    return (
        db.query(CollectorAgent)
        .filter(CollectorAgent.asset_id == (asset_id or "").strip())
        .order_by(CollectorAgent.id.desc())
        .first()
    )


# 判断 Agent 最近是否拉取过远程 module。
# Alloy import.http 默认 60 秒轮询一次，这里给 3 分钟容忍窗口，避免短时网络抖动立刻显示离线。
def _is_agent_recent(row: CollectorAgent | None) -> bool:
    """判断 Agent 是否在近期有心跳。"""
    if not row or not row.last_seen_at:
        return False
    try:
        last_seen = datetime.fromisoformat(row.last_seen_at.rstrip("Z"))
    except ValueError:
        return False
    return datetime.utcnow() - last_seen <= timedelta(minutes=3)


def _agent_status_record(asset: Asset, agent: CollectorAgent | None, enabled: bool | None = None) -> dict[str, Any]:
    """把 Collector Agent 状态映射成资产页原有 target 结构。"""
    is_recent = _is_agent_recent(agent)
    status = "up" if agent and is_recent and agent.status == "online" and not agent.last_error else "unreachable"
    if enabled is False:
        status = "disabled"
    if agent and agent.status == "disabled":
        status = "disabled"
    if not agent:
        status = "disabled"
    return {
        "asset_id": asset.instance_id,
        "name": asset.name,
        "ip": asset.ip,
        "provider": str(asset.provider or ""),
        "region": str(asset.region or ""),
        "port": 0,
        "job": "opspilot-alloy",
        "enabled": bool(agent and agent.status != "disabled") if enabled is None else bool(enabled),
        "source": "collector-fleet",
        "target": agent.agent_id if agent else "",
        "agent": "alloy",
        "agent_id": agent.agent_id if agent else "",
        "status": status,
        "last_checked_at": now_ts(),
        "last_error": (agent.last_error if agent else "")[:255],
    }


def _create_collector_register_token(db: Session, current_user: User, asset: Asset) -> str:
    """创建一次性注册令牌，专供本次 SSH 安装命令使用。"""
    raw_token = f"opspilot_reg_{secrets.token_urlsafe(32)}"
    db.add(
        CollectorRegistrationToken(
            token_id=f"rtok-{secrets.token_hex(8)}",
            token_hash=_hash_agent_token(raw_token),
            description=f"资产 {asset.name} Alloy Agent 自动注册",
            expires_at=(datetime.utcnow() + timedelta(minutes=30)).isoformat(timespec="seconds") + "Z",
            used_at="",
            created_by=getattr(current_user, "username", None) or "system",
            created_at=_collector_now_ts(),
        )
    )
    db.commit()
    return raw_token


def _asset_policy_labels(asset: Asset) -> dict[str, str]:
    """生成低基数、安全的采集标签。

    标签设计：
    - instance: 主机 IP（唯一标识，用于查询匹配）
    - host: 真实 hostname（优先），回退到资产名称
    """
    labels = {
        "asset_id": asset.instance_id,
        "host": (asset.hostname or "").strip() or asset.name,
        "instance": asset.ip,
    }
    if asset.provider:
        labels["provider"] = str(asset.provider)
    if asset.region:
        labels["region"] = str(asset.region)
    return labels


# 根据资产关联的应用确定 Alloy 策略的 app_scope 标签。
# 业务逻辑：一台主机可能属于多个应用（如 k8s-node 同时承载 A、B 两个 namespace），
# 但 Alloy 配置需要单一明确的 app_scope 用于日志路由与告警分组。
# 策略：优先取应用显式配置的 alloy_app_scope，若无则按应用名回退，
# 确保日志和指标都有清晰的业务归属标签。
def _resolve_app_scope_for_asset(db: Session, asset: Asset) -> str:
    """读取当前资产在应用中心的归属标签。

    规则（为了控制基数和避免歧义）：
    - 资产未被任何应用绑定：返回空串，不注入 app_id 标签；
    - 资产仅被一个应用绑定：返回该应用 app_id，渲染为 `app_id` 标签；
    - 资产被多个应用绑定：返回空串。多应用共享主机属于例外，继续强行打单一 label 会让指标归属不准。

    安全边界：
    - 这里只读 `application_resources` 一张业务表，不触发 SSH 或 HTTP 请求；
    - `app_id` 值由 OpsPilot 生成（固定格式 `APP-xxxxxxxx`），不受用户输入影响，避免 PromQL 注入；
    - 即便未来加上多应用归属，也优先保障不破坏现有 label 集。
    """
    # 延迟导入避免与 applications 模块产生循环依赖风险。
    from app.models.application import ApplicationResource  # noqa: WPS433

    resource_id = str(getattr(asset, "instance_id", "") or "").strip()
    if not resource_id:
        return ""
    rows = (
        db.query(ApplicationResource)
        .filter(
            ApplicationResource.resource_type == "host",
            ApplicationResource.resource_id == resource_id,
        )
        .all()
    )
    app_ids = list(dict.fromkeys([str(row.app_id or "").strip() for row in rows if str(row.app_id or "").strip()]))
    if len(app_ids) == 1:
        return app_ids[0]
    return ""


# 批量为资产列表重同步 Alloy 采集策略。
# 触发场景：
# - 应用删除或归档后，需要把关联资产的策略清理或迁移；
# - 资产从应用 A 迁移到应用 B，app_scope 标签需要更新；
# - 管理员在全局设置中修改了默认采集参数，需要全量下发。
# 为什么用批量而非单条？因为机房级重同步可能涉及数百台主机，逐条处理会导致 N+1 查询和事务膨胀。
def resync_alloy_policy_for_asset_ids(
    db: Session,
    asset_ids: list[str],
    current_user: User,
) -> None:
    """尝试把 Alloy 策略重新下发到被应用中心绑定 / 解绑的资产。

    该函数用于“应用中心资源绑定变更后 -> `app_id` 标签随之更新”的场景。
    安全边界：
    - 采用 best-effort：未安装 Agent、监控配置缺失、Alloy 渲染失败等任何异常都被静默吞下，
      让应用中心的资源绑定操作本身不因为监控链路问题而失败；
    - 每台资产仍复用 `_ensure_asset_alloy_policy` 的既有执行链，包括 label 白名单、
      remote_write / VictoriaLogs 地址校验、策略版本化；
    - 不接收任何前端传入的 label 或 Alloy DSL；所有 label 依旧在后端结构化生成。
    """
    unique_ids = [str(item or "").strip() for item in asset_ids or []]
    unique_ids = list(dict.fromkeys(item for item in unique_ids if item))
    if not unique_ids:
        return
    from app.models.asset import Asset as AssetModel  # noqa: WPS433

    assets = (
        db.query(AssetModel)
        .filter(AssetModel.instance_id.in_(unique_ids))
        .all()
    )
    for asset in assets:
        try:
            agent = _latest_agent_for_asset(db, asset.instance_id)
            if agent is None:
                continue
            _ensure_asset_alloy_policy(db=db, asset=asset, agent=agent, current_user=current_user)
        except Exception:
            # 监控链路问题不应阻塞应用中心的资源绑定变更。
            continue


def _load_policy_json(raw: str | None) -> dict[str, Any]:
    """安全解析策略里存的 JSON 字段，异常或非字典时返回空 dict。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_asset_alloy_policy(
    db: Session,
    asset: Asset,
    agent: CollectorAgent,
    current_user: User | None,
    logs_override: dict[str, Any] | None = None,
) -> CollectorConfigVersion:
    """
    为单台资产创建或更新默认 Alloy 策略，并发布到该 Agent。

    基线采集项（node 主机指标、docker 容器日志）仅在策略首次创建时写入；
    策略已存在时保留用户在采集管理页编辑过的 metrics/logs，仅刷新随资产/应用
    归属变化的标签与写入地址，避免装机/应用归属变更/批量重同步把人工编辑冲掉。

    logs_override：日志目标启停时强制覆盖 logs（只采 Docker，不采 syslog），
    用于「启用/停用日志采集」必须立即生效的场景。
    """
    cfg = read_monitoring_config(db)
    remote_write_url = str(cfg.get("remote_write_url") or "").strip()
    if not remote_write_url:
        vm_url = str(cfg.get("vm_url") or "").strip().rstrip("/")
        if not vm_url:
            raise HTTPException(status_code=400, detail="请先配置 VictoriaMetrics 地址")
        remote_write_url = _agent_metric_write_url(vm_url)
    else:
        remote_write_url = _agent_metric_write_url(remote_write_url)
    vlogs_url = _agent_logs_write_url(str(cfg.get("vlogs_ingest_url") or cfg.get("vlogs_url") or ""))
    if not vlogs_url:
        raise HTTPException(status_code=400, detail="请先配置 VictoriaLogs 写入地址")
    policy_id = f"{ALLOY_DEFAULT_POLICY_PREFIX}{asset.instance_id}"[:50]
    now = _collector_now_ts()

    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    is_new = policy is None

    # 基线默认仅在首次创建时写入；策略已存在则保留用户在采集管理页编辑过的
    # metrics/logs，避免装机 / 应用归属变更 / 批量重同步把人工编辑冲掉。
    if is_new:
        metrics: dict[str, Any] = {"node": True}
        logs: dict[str, Any] = {"system": False, "docker_containers": True}
    else:
        metrics = _load_policy_json(policy.metrics_json) or {"node": True}
        logs = _load_policy_json(policy.logs_json) or {"system": False, "docker_containers": True}
    if logs_override is not None:
        logs = dict(logs_override)

    # 标签按资产 / 应用归属每次刷新（这部分不属于用户可编辑的采集内容）。
    labels = _asset_policy_labels(asset)
    app_scope = _resolve_app_scope_for_asset(db, asset)
    if app_scope:
        # 只有当前资产归属单一应用时才打 app_id 标签，避免多应用共享主机时指标错位。
        labels["app_id"] = app_scope
    try:
        alloy_config = render_alloy_config(
            agent_id=agent.agent_id,
            metrics=metrics,
            logs=logs,
            labels=labels,
            remote_write_url=remote_write_url,
            vlogs_url=vlogs_url,
            remote_write_auth=_collector_write_auth(cfg),
            vlogs_auth=_collector_write_auth(cfg),
        )
    except CollectorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_new:
        policy = CollectorPolicy(
            policy_id=policy_id,
            name=f"{asset.name} 默认 Alloy 采集",
            scope_type="agent",
            scope_ref=agent.agent_id,
            created_at=now,
            updated_at=now,
        )
        db.add(policy)
    policy.name = f"{asset.name} 默认 Alloy 采集"
    policy.scope_type = "agent"
    policy.scope_ref = agent.agent_id
    policy.metrics_json = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
    policy.logs_json = json.dumps(logs, ensure_ascii=False, sort_keys=True)
    policy.labels_json = json.dumps(labels, ensure_ascii=False, sort_keys=True)
    policy.redaction_json = json.dumps({}, ensure_ascii=False)
    policy.enabled = "true"
    policy.updated_at = now
    db.flush()

    latest = (
        db.query(CollectorConfigVersion)
        .filter(CollectorConfigVersion.policy_id == policy_id)
        .order_by(CollectorConfigVersion.version.desc())
        .first()
    )
    new_hash = config_hash(alloy_config)
    # 配置未变化（例如仅触发重同步但标签/内容不变）时复用现有已发布版本，
    # 避免每次应用归属变更都刷出一堆重复配置版本。
    if latest is not None and latest.config_hash == new_hash:
        if agent.current_version_id != latest.version_id:
            agent.current_version_id = latest.version_id
            agent.updated_at = now
            db.commit()
        else:
            db.commit()
        return latest

    version_no = int(latest.version if latest else 0) + 1
    version = CollectorConfigVersion(
        version_id=f"cfg-{secrets.token_hex(8)}",
        policy_id=policy_id,
        version=version_no,
        config_hash=new_hash,
        alloy_config=alloy_config,
        status="published",
        created_by=current_user.username,
        created_at=now,
    )
    db.add(version)
    agent.current_version_id = version.version_id
    agent.updated_at = now
    db.commit()
    return version


def _policy_logs_json(db: Session, asset_id: str) -> dict[str, Any]:
    """读取某资产默认策略的 logs 配置（无策略时返回空 dict）。"""
    policy_id = f"{ALLOY_DEFAULT_POLICY_PREFIX}{str(asset_id or '').strip()}"[:50]
    policy = db.query(CollectorPolicy).filter(CollectorPolicy.policy_id == policy_id).first()
    return _load_policy_json(policy.logs_json) if policy else {}


def _log_collect_state(
    db: Session,
    asset: Asset,
    agent: CollectorAgent | None,
    target_enabled: bool | None = None,
) -> dict[str, Any]:
    """聚合「日志是否在采」状态：以策略是否成功下发 docker 采集为准。

    - logs_enabled：策略里 docker_containers 已开，且 Agent 已挂上配置版本；
      若调用方给了 target_enabled=False，一律视为未在采。
    - last_log_at：在采时用 Agent 心跳近似（无独立日志入库时间戳）。
    """
    logs = _policy_logs_json(db, asset.instance_id)
    docker_on = bool(logs.get("docker_containers"))
    has_version = bool(agent and getattr(agent, "current_version_id", ""))
    deployed = docker_on and has_version
    if target_enabled is False:
        deployed = False
    last_log_at = str(getattr(agent, "last_seen_at", "") or "") if deployed and agent else ""
    return {
        "logs_enabled": deployed,
        "last_log_at": last_log_at,
    }


def _metrics_collect_state(
    agent: CollectorAgent | None,
    target_enabled: bool | None = None,
) -> dict[str, Any]:
    """聚合「指标是否在采」状态（Agent 心跳近似）。"""
    enabled = bool(agent and getattr(agent, "current_version_id", "") and agent.status != "disabled")
    if target_enabled is False:
        enabled = False
    last_metric_at = str(getattr(agent, "last_seen_at", "") or "") if enabled and agent else ""
    return {
        "metrics_enabled": enabled,
        "last_metric_at": last_metric_at,
    }


def _collector_write_auth(cfg: dict[str, Any]) -> dict[str, str]:
    """把监控连接认证转换为 Alloy endpoint 可渲染的安全结构。"""
    auth_type = str(cfg.get("auth_type") or "none").strip().lower()
    secret = decrypt_secret(str(cfg.get("secret") or ""))
    if auth_type == "basic":
        username = str(cfg.get("username") or "").strip()
        return {"type": "basic", "username": username, "password": secret} if username and secret else {"type": "none"}
    if auth_type == "bearer" and secret:
        return {"type": "bearer", "token": secret}
    return {"type": "none"}


def _agent_metric_write_url(vm_url: str) -> str:
    """返回主机侧 Alloy 可访问的 VictoriaMetrics remote_write 地址。

    远端 Agent 通过 Nginx 代理写入（:18080/vm/），不直连 :8428。
    """
    base = _agent_reachable_url(vm_url, {"opspilot-victoriametrics", "victoriametrics"}, "8428", "/vm")
    return base if base.endswith("/api/v1/write") else f"{base}/api/v1/write"


def _agent_logs_write_url(vlogs_url: str) -> str:
    """返回主机侧 Alloy 可访问的 VictoriaLogs Loki 写入地址。

    远端 Agent 通过 Nginx 代理写入（:18080/vlogs/），不直连 :9428。
    """
    base = _agent_reachable_url(vlogs_url, {"opspilot-victorialogs", "victorialogs"}, "9428", "/vlogs")
    if not base:
        return ""
    if base.endswith("/insert/loki/api/v1/push"):
        return base
    if base.endswith("/insert/elasticsearch"):
        base = base[: -len("/insert/elasticsearch")]
    return f"{base.rstrip('/')}/insert/loki/api/v1/push"


# 监控 Nginx 代理端口，所有远端 Agent 通过该入口写入。
MONITORING_PROXY_PORT = "18080"


def _agent_reachable_url(raw_url: str, internal_hosts: set[str], local_port: str, proxy_prefix: str) -> str:
    """
    将内部地址转换为远端 Agent 可通过 Nginx 代理访问的地址。

    策略：
    1. Docker 服务名或回环地址 → 走 Nginx 代理（真实IP:18080/prefix）
    2. 管理员显式配置的外部地址 → 保持原样（认为已是可达入口）
    """
    value = str(raw_url or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        real_ip = s.getsockname()[0]
        s.close()
    except Exception:
        real_ip = "127.0.0.1"

    if hostname in internal_hosts or hostname in {"127.0.0.1", "localhost", "::1"}:
        return f"http://{real_ip}:{MONITORING_PROXY_PORT}{proxy_prefix}"

    return value


