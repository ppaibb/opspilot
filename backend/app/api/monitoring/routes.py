"""监控告警模块 FastAPI 路由层（薄壳）。

【架构设计】
 routes.py 只负责三件事：
1. 权限校验（RBAC）：所有敏感操作通过 Depends(require_permissions) 拦截；
2. 参数校验与反序列化：利用 Pydantic 模型自动完成；
3. 调用 services/ 中的业务函数，自身不包含任何业务逻辑。

为什么坚持"薄壳"？
- 路由层如果混入业务逻辑，会导致单元测试必须启动 HTTP 客户端，测试成本指数级上升；
- services/ 中的纯函数可在无 FastAPI 上下文的环境下被直接测试；
- 未来如需替换为 gRPC 或 CLI，业务逻辑无需改动。
"""
from __future__ import annotations

import json
import re
from typing import Any

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import requests
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_permissions
from app.core.crypto import encrypt_secret
from app.db.database import get_db
from app.models.asset import Asset
from app.models.collector import CollectorAgent
from app.models.system_config import SystemConfig
from app.models.user import User
from app.api.assets import ensure_asset_credential_access
from app.api.monitoring.constants import (
    ALERT_RULE_TEMPLATES,
    AUTH_TYPES,
    CONFIG_KEY,
    DASHBOARD_RANGE_QUERIES,
    DEFAULT_ALERT_RULES,
    DEFAULT_NODE_EXPORTER_PORT,
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEPLOYMENT_ACTIONS,
    LOG_TARGETS_CONFIG_KEY,
    MAX_DASHBOARD_RANGE_MINUTES,
    MAX_DASHBOARD_STEP_SECONDS,
    MAX_LOG_LIMIT,
    MAX_LOG_QUERY_LENGTH,
    MIN_DASHBOARD_RANGE_MINUTES,
    MIN_DASHBOARD_STEP_SECONDS,
    MONITORING_COMPOSE_FILE,
    NOTIFICATIONS_CONFIG_KEY,
    REPO_ROOT,
    RULES_CONFIG_KEY,
    TARGETS_CONFIG_KEY,
    VMAGENT_CONFIG_FILE,
)
from app.api.monitoring.schemas import (
    AlertRuleItem,
    ApplyTemplatesRequest,
    LogContextRequest,
    LogHistogramRequest,
    LogQueryRequest,
    LogFieldsRequest,
    LogSuggestRequest,
    LogTargetEnableRequest,
    MonitoringConfigResponse,
    MonitoringConfigUpdate,
    MonitoringDeploymentAction,
    MonitoringRulesResponse,
    MonitoringRulesUpdate,
    MonitoringSelfCheckResponse,
    NotificationChannelItem,
    NotificationChannelsResponse,
    NotificationChannelsUpdate,
    NotificationTemplatesResponse,
    NotificationTemplatesUpdate,
    NotificationTemplatePreviewRequest,
    TargetEnableRequest,
)
from app.api.monitoring.services.config_store import (
    read_monitoring_config,
    read_log_targets,
    read_monitoring_targets,
    save_log_targets,
    save_monitoring_targets,
    _safe_load_json_dict,
)
from app.api.monitoring.services.rules import (
    apply_rule_templates,
    read_monitoring_rules,
    _normalize_monitoring_rules,
    _persist_rules_and_sync_alerting,
    stamp_rule_updated_at,
)
from app.api.monitoring.services.notifications import (
    _build_test_alertmanager_payload,
    _normalize_notification_channels,
    _persist_notifications_and_sync_alerting,
    read_monitoring_notifications,
)
from app.api.monitoring.services.vmagent import (
    _persist_targets_and_sync_vmagent,
    _normalize_targets_for_vmagent,
    _render_opspilot_vmagent_block,
    _write_vmagent_config_file,
)
from app.api.monitoring.services.deployment import (
    _apply_local_monitoring_config,
    _compose_args,
    _deployment_status,
    _run_local_command,
)
from app.api.monitoring.services.proxy import (
    _normalize_api_base,
    _normalize_vlogs_base,
    _normalize_vlogs_ingest_base,
    _request_kwargs,
    _response_config,
    _vm_get,
    _vlogs_get,
)
from app.api.monitoring.services.dashboard import (
    _clamp_dashboard_range,
    _fetch_alerts,
    _fetch_summary_load1,
    _fetch_top_filesystems,
    _fetch_top_hosts,
    _filter_dashboard_alerts,
    _filter_dashboard_targets,
    _filter_logs_by_scope,
    _filter_metric_instances,
    _parse_matrix_first_series,
    _parse_up_targets,
    _parse_vector_by_instance,
    _safe_metric_float,
    _split_scope_param,
    build_logs_scope_clause,
    prom_instance_regex,
)
from app.api.monitoring.services.targets import (
    _asset_target_record,
    _build_alloy_disable_command,
    _build_alloy_install_command,
    _build_alloy_uninstall_command,
    _check_exporter_endpoint,
    _external_request_base_url,
    _get_asset_or_404,
    _upsert_target,
)
from app.api.monitoring.services.log_targets import (
    _asset_log_target_record,
    _build_remote_vector_check_command,
    _build_remote_vector_disable_command,
    _format_remote_error,
    _normalize_log_targets_for_assets,
    _persist_log_targets,
    _run_asset_ssh,
)
from app.api.monitoring.services.alloy_policy import (
    _agent_status_record,
    _collector_now_ts,
    _create_collector_register_token,
    _ensure_asset_alloy_policy,
    _is_agent_recent,
    _latest_agent_for_asset,
    resync_alloy_policy_for_asset_ids,
)

# router 定义在 routes.py 而非 __init__.py，是为了消除循环导入：
# __init__.py 从 routes import router，而 routes.py 从 services import 函数，
# services 不反向引用 routes，形成干净的无环依赖图。
router = APIRouter()


def _builtin_monitoring_probe_online(status: dict[str, Any]) -> bool:
    """判断内置监控服务探测是否已经在线，用于 Docker CLI 不可用时避免误报未部署。"""
    output = str(status.get("ps_output") or "")
    return "VictoriaMetrics: online" in output or "VictoriaLogs: online" in output or "Grafana: online" in output


def _get_base_disk(device: str) -> str:
    """匹配并提取物理磁盘设备名（如 /dev/sda1 -> /dev/sda，sda1 -> sda，/dev/nvme0n1p1 -> /dev/nvme0n1）"""
    if not device:
        return "disk"
    nvme_match = re.match(r"^(/dev/)?(nvme\d+n\d+)p\d+$", device)
    if nvme_match:
        prefix = nvme_match.group(1) or ""
        return prefix + nvme_match.group(2)
    sd_vd_match = re.match(r"^(/dev/)?([a-zA-Z]+)\d+$", device)
    if sd_vd_match:
        prefix = sd_vd_match.group(1) or ""
        return prefix + sd_vd_match.group(2)
    return device


@router.get("/deployment/status")
def get_monitoring_deployment_status(
    _=Depends(require_permissions("module.settings.system")),
):
    """查看内置 VictoriaMetrics 套件部署状态。"""
    return _deployment_status()


@router.post("/deployment/action")
def run_monitoring_deployment_action(
    request: Request,
    payload: MonitoringDeploymentAction,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """执行内置监控套件部署动作。"""
    action = (payload.action or "").strip().lower()
    if action not in DEPLOYMENT_ACTIONS:
        raise HTTPException(status_code=400, detail="部署动作非法")
    if not MONITORING_COMPOSE_FILE.exists() and action != "apply-local-config":
        raise HTTPException(status_code=400, detail="监控套件 docker-compose.yml 不存在")

    result = {"ok": True, "code": 0, "stdout": "", "stderr": ""}
    message = "操作完成"
    if action == "status":
        status = _deployment_status()
        result = {
            "ok": not bool(status.get("last_error")),
            "code": 0 if not status.get("last_error") else 1,
            "stdout": str(status.get("ps_output") or ""),
            "stderr": str(status.get("last_error") or ""),
        }
        message = "监控套件状态已刷新"
    elif action == "start":
        try:
            _write_vmagent_config_file(db, read_monitoring_targets(db)["targets"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"启动前同步监控配置失败: {str(exc)}")
        current_status = _deployment_status()
        # Docker 化部署时后端容器默认不挂载宿主机 Docker Socket。
        # 这种情况下不能执行 up/down/restart，但如果探测到监控服务已经在线，
        # “启动套件”应返回当前可用状态，而不是用 docker 命令缺失误导用户。
        if not current_status.get("docker_ok") and _builtin_monitoring_probe_online(current_status):
            result = {"ok": True, "code": 0, "stdout": str(current_status.get("ps_output") or ""), "stderr": ""}
            message = "内置监控服务已在线"
            if payload.apply_config:
                _apply_local_monitoring_config(db, _external_request_base_url(request))
                db.commit()
                message = "内置监控服务已在线，并已写入本地监控连接"
        else:
            result = _run_local_command(_compose_args("up", "-d"), 180)
            message = "监控套件启动命令已执行"
            if result["ok"] and payload.apply_config:
                _apply_local_monitoring_config(db, _external_request_base_url(request))
                db.commit()
                message = "监控套件已启动，并已写入本地监控连接"
    elif action == "stop":
        result = _run_local_command(_compose_args("down"), 120)
        message = "监控套件停止命令已执行"
    elif action == "restart":
        try:
            _write_vmagent_config_file(db, read_monitoring_targets(db)["targets"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"重启前同步监控配置失败: {str(exc)}")
        result = _run_local_command(_compose_args("restart"), 120)
        message = "监控套件重启命令已执行"
    elif action == "apply-local-config":
        _apply_local_monitoring_config(db, _external_request_base_url(request))
        db.commit()
        message = "已写入内置监控套件默认连接"

    return {
        "success": bool(result["ok"]),
        "action": action,
        "message": message if result["ok"] else str(result["stderr"] or result["stdout"] or "命令执行失败").strip(),
        "stdout": str(result["stdout"] or ""),
        "stderr": str(result["stderr"] or ""),
        "status": _deployment_status(),
    }


@router.get("/config", response_model=MonitoringConfigResponse)
def get_monitoring_config(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    """读取 VictoriaMetrics 连接配置。"""
    return _response_config(read_monitoring_config(db))


@router.put("/config", response_model=MonitoringConfigResponse)
def save_monitoring_config(
    payload: MonitoringConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """保存监控连接配置（**增量合并**）：只更新提交里出现的字段。

    只想改 Grafana 时不必重填 VictoriaMetrics；未传字段保持原值。
    """
    row = db.query(SystemConfig).filter(SystemConfig.config_key == CONFIG_KEY).first()
    current = _safe_load_json_dict(row.config_value if row else "")
    content = dict(current)

    # 仅覆盖请求里显式出现的字段（exclude_unset）
    patch = payload.model_dump(exclude_unset=True)

    if "vm_url" in patch:
        content["vm_url"] = str(patch["vm_url"] or "").strip().rstrip("/")
    if "auth_type" in patch:
        content["auth_type"] = str(patch["auth_type"] or "none").strip().lower() or "none"
    for key in (
        "vmui_url",
        "grafana_url",
        "vlogs_url",
        "vlogs_ingest_url",
        "remote_write_url",
        "vlogsui_url",
        "username",
    ):
        if key in patch:
            content[key] = str(patch[key] or "").strip().rstrip("/")
    if "verify_tls" in patch:
        content["verify_tls"] = bool(patch["verify_tls"])
    if "timeout_seconds" in patch:
        content["timeout_seconds"] = max(3, min(int(patch["timeout_seconds"] or DEFAULT_TIMEOUT_SECONDS), 60))
    if "default_step_seconds" in patch:
        content["default_step_seconds"] = max(10, min(int(patch["default_step_seconds"] or DEFAULT_STEP_SECONDS), 600))
    if "secret" in patch and str(patch.get("secret") or "").strip():
        content["secret"] = encrypt_secret(str(patch["secret"]))

    auth_type = str(content.get("auth_type") or "none")
    if not str(content.get("vm_url") or "").strip():
        raise HTTPException(status_code=400, detail="VictoriaMetrics 查询地址不能为空")
    # 写入地址格式校验（空值=自动推导）；可达性在 self-check/link-health 展示
    from app.api.monitoring.services.deployment import (
        _validate_write_url_format,
        resolve_agent_write_urls,
    )
    _validate_write_url_format(str(content.get("remote_write_url") or ""), "指标写入（Agent）")
    _validate_write_url_format(str(content.get("vlogs_ingest_url") or ""), "日志写入（Agent）")
    if auth_type not in AUTH_TYPES:
        raise HTTPException(status_code=400, detail="鉴权方式非法")
    if auth_type != "none" and not str(content.get("secret") or "").strip():
        raise HTTPException(status_code=400, detail="当前鉴权方式需要填写密钥或密码")
    if auth_type == "none":
        content["secret"] = ""

    from datetime import datetime as _datetime

    now_str = _datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not row:
        row = SystemConfig(config_key=CONFIG_KEY, config_value=json.dumps(content, ensure_ascii=False), updated_at=now_str)
        db.add(row)
    else:
        row.config_value = json.dumps(content, ensure_ascii=False)
        row.updated_at = now_str
    db.commit()
    return _response_config(content)


@router.get("/rules", response_model=MonitoringRulesResponse)
def get_monitoring_rules(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """读取告警规则配置。"""
    rules, updated_at = read_monitoring_rules(db)
    return {"rules": rules, "updated_at": updated_at, "sync_info": None}


@router.put("/rules", response_model=MonitoringRulesResponse)
def save_monitoring_rules(
    payload: MonitoringRulesUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """保存告警规则。规则写入数据库后由内置告警引擎下一周期自动生效。"""
    channels, _ = read_monitoring_notifications(db)
    normalized = _normalize_monitoring_rules([item.model_dump() for item in payload.rules], {str(item.get("id") or "") for item in channels})
    if not normalized:
        raise HTTPException(status_code=400, detail="至少需要保留一条有效规则")
    normalized = stamp_rule_updated_at(db, normalized)
    sync_info = _persist_rules_and_sync_alerting(db, normalized)
    return {"rules": normalized, "updated_at": sync_info["updated_at"], "sync_info": sync_info}


@router.get("/rules/templates")
def get_alert_rule_templates(
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """返回内置告警规则模板库，供前端"从模板创建"功能使用。"""
    return {"templates": ALERT_RULE_TEMPLATES}


@router.post("/rules/apply-templates")
def apply_alert_rule_templates(
    payload: ApplyTemplatesRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """从模板库批量创建告警规则。"""
    result = apply_rule_templates(db, payload.template_ids)
    return result


class RuleTestRequest(BaseModel):
    """规则试跑请求:对当前 PromQL 即时查询一次,看命中哪些序列。"""
    expr: str = ""
    host_ids: list[str] = Field(default_factory=list)


@router.post("/rules/test")
def test_alert_rule(
    payload: RuleTestRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """对一条规则的 PromQL 即时查询一次(瞬时值),返回命中的序列与样本值。

    不写状态、不发通知,纯只读试跑;复用告警引擎的 host 过滤与 VM 查询逻辑,
    与真实评估走同一条数据路径。
    """
    from app.services.alert_engine.engine import apply_host_filter, vm_query

    expr = (payload.expr or "").strip()
    if not expr:
        raise HTTPException(status_code=400, detail="PromQL 表达式不能为空")
    if len(expr) > 4096:
        raise HTTPException(status_code=400, detail="PromQL 表达式过长(上限 4096)")

    cfg = read_monitoring_config(db)
    filtered_expr = apply_host_filter(db, payload.host_ids or [], expr)

    try:
        results = vm_query(cfg, filtered_expr)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "expr": filtered_expr,
            "match_count": 0,
            "series": [],
        }

    series = []
    for r in results[:50]:  # 最多回 50 条,避免前端爆量
        labels = r.get("metric", {}) or {}
        value_tuple = r.get("value", []) or []
        value = str(value_tuple[1]) if len(value_tuple) > 1 else ""
        series.append({
            "instance": labels.get("instance", ""),
            "labels": labels,
            "value": value,
        })

    return {
        "ok": True,
        "error": "",
        "expr": filtered_expr,
        "match_count": len(results),
        "series": series,
    }


TEMPLATES_CONFIG_KEY = "monitoring_notification_templates"


@router.get("/notifications", response_model=NotificationChannelsResponse)
def get_monitoring_notifications(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.channels")),
):
    """读取通知渠道配置。"""
    channels, updated_at = read_monitoring_notifications(db)
    return {"channels": channels, "updated_at": updated_at, "sync_info": None}


@router.put("/notifications", response_model=NotificationChannelsResponse)
def save_monitoring_notifications(
    payload: NotificationChannelsUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.channels", "action.monitoring.channels.manage")),
):
    """保存通知渠道，并同步 Alertmanager 配置。"""
    normalized = _normalize_notification_channels([item.model_dump() for item in payload.channels])
    sync_info = _persist_notifications_and_sync_alerting(db, normalized)
    return {"channels": normalized, "updated_at": sync_info["updated_at"], "sync_info": sync_info}


@router.get("/notifications/templates", response_model=NotificationTemplatesResponse)
def get_monitoring_templates(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.channels")),
):
    """读取所有通知模板。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == TEMPLATES_CONFIG_KEY).first()
    if not row:
        return {"templates": [], "updated_at": ""}
    payload = _safe_load_json_dict(row.config_value)
    templates = payload.get("templates") or []
    return {"templates": templates, "updated_at": str(row.updated_at or "")}


@router.put("/notifications/templates", response_model=NotificationTemplatesResponse)
def save_monitoring_templates(
    payload: NotificationTemplatesUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.channels", "action.monitoring.channels.manage")),
):
    """保存通知模板列表。"""
    from datetime import datetime as _datetime
    row = db.query(SystemConfig).filter(SystemConfig.config_key == TEMPLATES_CONFIG_KEY).first()
    now_str = _datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 规整模板数据
    normalized = []
    for index, item in enumerate(payload.templates):
        tmpl_dict = item.model_dump()
        if not tmpl_dict.get("id"):
            tmpl_dict["id"] = f"template-{int(_datetime.now().timestamp() * 1000) + index}"
        normalized.append(tmpl_dict)
        
    content = json.dumps({"templates": normalized}, ensure_ascii=False)
    if not row:
        row = SystemConfig(config_key=TEMPLATES_CONFIG_KEY, config_value=content, updated_at=now_str)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存通知模板失败: {str(exc)}")
        
    return {"templates": normalized, "updated_at": now_str}


@router.post("/notifications/templates/preview")
def preview_monitoring_template(
    payload: NotificationTemplatePreviewRequest,
    _=Depends(require_permissions("module.monitoring.channels")),
):
    """根据样例数据预览渲染通知模板。"""
    from app.api.monitoring.services.alert_dispatcher import render_template_string
    
    # 模拟一份样例告警数据
    starts_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mock_alert = {
        "name": "CPU使用率过高",
        "severity": "critical",
        "instance": "192.168.1.100:9100",
        "summary": "主机 192.168.1.100 CPU 使用率已达 92.5%",
        "description": "实例 192.168.1.100 的 CPU 持续 5 分钟超过阈值 90%，当前值为 92.5%，可能会引起服务响应变慢。",
        "startsAt": starts_iso,
        "status": "firing" if payload.trigger_type != "resolved" else "resolved"
    }
    
    # 如果是恢复通知，把 summary 换成恢复样式
    if payload.trigger_type == "resolved":
        mock_alert["summary"] = f"[已恢复] {mock_alert['summary']}"
        mock_alert["description"] = f"主机 192.168.1.100 的 CPU 占用已回落正常。首发时间: {starts_iso}"
        
    title_rendered = render_template_string(payload.title_template, mock_alert)
    body_rendered = render_template_string(payload.body_template, mock_alert)
    
    return {
        "title": title_rendered,
        "body": body_rendered,
    }


@router.post("/notifications/{channel_id}/test")
def test_monitoring_notification(
    channel_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.channels", "action.monitoring.channels.manage")),
):
    """向指定通知渠道发送测试 Webhook。"""
    channels, _ = read_monitoring_notifications(db)
    channel = next((item for item in channels if str(item.get("id") or "") == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="通知渠道不存在")
    if not channel.get("enabled"):
        raise HTTPException(status_code=400, detail="通知渠道已停用，不能执行测试发送")
    webhook_url = str(channel.get("webhook_url") or "").strip()
    if not webhook_url:
        raise HTTPException(status_code=400, detail="通知渠道未配置 Webhook 地址")

    try:
        resp = requests.post(
            webhook_url,
            json=_build_test_alertmanager_payload(channel),
            timeout=8,
        )
        ok = resp.status_code < 400
        return {
            "success": ok,
            "status_code": resp.status_code,
            "message": "测试通知已发送" if ok else f"Webhook 返回 HTTP {resp.status_code}",
            "response": resp.text[:1000],
        }
    except requests.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"测试通知失败: {str(exc)}")


@router.post("/self-check", response_model=MonitoringSelfCheckResponse)
def self_check_monitoring(
    payload: MonitoringConfigUpdate | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """检测连接状态。可带未保存的表单值覆盖已存配置（测试连接不落库）。"""
    cfg = read_monitoring_config(db)
    if payload is not None:
        override = payload.model_dump(exclude_unset=True)
        # 密钥留空时沿用已存密文，避免测试把 secret 弄丢
        if not str(override.get("secret") or "").strip():
            override.pop("secret", None)
        cfg = {**cfg, **{k: v for k, v in override.items() if v is not None}}
    checks: list[dict[str, Any]] = []
    hints: list[str] = []

    try:
        _vm_get(cfg, "/query", {"query": "1"})
        checks.append({"name": "VictoriaMetrics 查询接口", "ok": True, "message": "连接正常"})
    except HTTPException as exc:
        checks.append({"name": "VictoriaMetrics 查询接口", "ok": False, "message": str(exc.detail)})
        hints.append("检查 VictoriaMetrics 地址是否填写到单机版 8428 或集群版 vmselect 查询入口")

    grafana_url = str(cfg.get("grafana_url") or "").strip().rstrip("/")
    if grafana_url:
        try:
            resp = requests.get(f"{grafana_url}/api/health", **_request_kwargs(cfg))
            checks.append({"name": "Grafana", "ok": resp.status_code < 400, "message": f"HTTP {resp.status_code}"})
        except Exception as exc:
            # 外部回环可能被防火墙拦截，尝试通过 Docker 内置网络直连进行检测
            internal_url = "http://opspilot-grafana:3000/grafana"
            try:
                resp = requests.get(f"{internal_url}/api/health", **_request_kwargs(cfg))
                checks.append({"name": "Grafana", "ok": resp.status_code < 400, "message": f"HTTP {resp.status_code} (内部网络)"})
            except Exception:
                checks.append({"name": "Grafana", "ok": False, "message": str(exc)})
                hints.append("Grafana 需要配置 root_url 为外部访问地址，并启用 auth.proxy 后才能稳定免密跳转")

    vlogs_url = str(cfg.get("vlogs_url") or "").strip().rstrip("/")
    if vlogs_url:
        try:
            _vlogs_get(cfg, {"query": "*", "limit": 1})
            checks.append({"name": "VictoriaLogs", "ok": True, "message": "连接正常"})
        except HTTPException as exc:
            checks.append({"name": "VictoriaLogs", "ok": False, "message": str(exc.detail)})
            hints.append("检查 VictoriaLogs 地址是否填写到单机版 HTTP 入口，例如 http://host:9428")
    # 采集 Agent 写入地址：resolve_agent_write_urls + 可达性探测
    from app.api.monitoring.services.deployment import probe_agent_write_urls, resolve_agent_write_urls

    agent_urls = probe_agent_write_urls(cfg)
    remote_write_resolved, vlogs_ingest_resolved = resolve_agent_write_urls(cfg)
    checks.append({
        "name": "指标写入（Agent）",
        "ok": bool(agent_urls.get("remote_write", {}).get("ok")),
        "message": f"{agent_urls.get('remote_write', {}).get('url') or '(空)'} — {agent_urls.get('remote_write', {}).get('message')}",
    })
    checks.append({
        "name": "日志写入（Agent）",
        "ok": bool(agent_urls.get("vlogs_ingest", {}).get("ok")),
        "message": f"{agent_urls.get('vlogs_ingest', {}).get('url') or '(空)'} — {agent_urls.get('vlogs_ingest', {}).get('message')}",
    })
    for key in ("remote_write", "vlogs_ingest"):
        item = agent_urls.get(key) or {}
        if not item.get("ok") and item.get("hint"):
            hints.append(f"{item.get('name')}：{item.get('hint')}")
    if not vlogs_ingest_resolved:
        hints.append("如需主机侧日志采集，请额外填写“日志写入（Agent）”，该地址必须能被远端主机访问")

    overall_ok = bool(checks) and all(item["ok"] for item in checks)
    return {
        "overall_ok": overall_ok,
        "checks": checks,
        "hints": hints,
        "agent_write_urls": agent_urls,
    }


@router.get("/link-health")
def monitoring_link_health(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """链路自检：采集心跳 / 规则评估最近时间 / 渠道连通（绿黄红三灯）。

    无独立健康接口时聚合现有 config / scheduler / 渠道配置与通知日志，
    供监控首页小面板直接渲染。
    """
    from app.core.scheduler_registry import _TASKS
    from app.models.monitoring_notification_log import MonitoringNotificationLog

    cfg = read_monitoring_config(db)

    def _lamp(ok: bool | None) -> str:
        if ok is True:
            return "green"
        if ok is None:
            return "yellow"
        return "red"

    # 1) 采集/评估心跳：alert_engine 定时任务最近一次执行
    engine_state = dict(_TASKS.get("alert_engine") or {})
    last_run_at = str(engine_state.get("last_run_at") or "")
    last_status = str(engine_state.get("last_status") or "pending")
    collector_ok: bool | None
    collector_msg: str
    if not last_run_at:
        collector_ok = None
        collector_msg = "告警引擎尚未执行（启动后约 20 秒首跑）"
    elif last_status == "ok":
        collector_ok = True
        collector_msg = f"最近评估 {last_run_at}"
    else:
        collector_ok = False
        collector_msg = f"最近评估失败：{str(engine_state.get('last_error') or '未知错误')[:120]}"

    # 2) 规则评估：规则是否配置 + VM 是否可达
    rules, _rules_updated = read_monitoring_rules(db)
    enabled_rules = [r for r in rules if r.get("enabled")]
    vm_configured = bool(str(cfg.get("vm_url") or "").strip())
    if not enabled_rules:
        rule_ok: bool | None = None
        rule_msg = "未启用任何告警规则，去「告警规则」创建或启用一条"
    elif not vm_configured:
        rule_ok = False
        rule_msg = "VictoriaMetrics 未配置，规则无法评估"
    elif collector_ok is False:
        rule_ok = False
        rule_msg = "评估任务最近一次失败，见采集心跳"
    else:
        rule_ok = True
        rule_msg = f"{len(enabled_rules)} 条规则启用中 · 最近评估 {last_run_at or '-'}"

    # 3) 渠道连通：是否配置渠道 + 最近一次发送结果
    channels, _ch_updated = read_monitoring_notifications(db)
    enabled_channels = [c for c in channels if c.get("enabled", True)]
    if not enabled_channels:
        channel_ok: bool | None = False
        channel_msg = "未配置通知渠道，请到「系统设置 → 通知渠道」添加 Webhook"
    else:
        last_log = (
            db.query(MonitoringNotificationLog)
            .order_by(MonitoringNotificationLog.created_at.desc())
            .first()
        )
        if last_log is None:
            channel_ok = None
            channel_msg = f"{len(enabled_channels)} 个渠道已配置，尚无发送记录（可用测试通知验证）"
        elif last_log.success:
            channel_ok = True
            channel_msg = f"{len(enabled_channels)} 个渠道 · 最近发送成功（{last_log.channel_name}）"
        else:
            channel_ok = False
            channel_msg = f"最近发送失败：{(last_log.message or '')[:120]}"

    checks = [
        {
            "key": "collector",
            "name": "采集 / 规则评估心跳",
            "lamp": _lamp(collector_ok),
            "message": collector_msg,
            "last_at": last_run_at,
        },
        {
            "key": "rules",
            "name": "规则评估",
            "lamp": _lamp(rule_ok),
            "message": rule_msg,
            "last_at": last_run_at,
        },
        {
            "key": "channels",
            "name": "通知渠道",
            "lamp": _lamp(channel_ok),
            "message": channel_msg,
            "last_at": str(getattr(last_log, "created_at", "") or "") if enabled_channels else "",
        },
    ]

    # 4) 采集 Agent 写入地址（resolve_agent_write_urls + 可达性）
    from app.api.monitoring.services.deployment import probe_agent_write_urls

    agent_urls = probe_agent_write_urls(cfg)
    rw = agent_urls.get("remote_write") or {}
    vi = agent_urls.get("vlogs_ingest") or {}
    write_ok = bool(rw.get("ok")) and bool(vi.get("ok"))
    write_msg_parts = [
        f"指标 {rw.get('url') or '(空)'} {'可达' if rw.get('ok') else '不可达'}",
        f"日志 {vi.get('url') or '(空)'} {'可达' if vi.get('ok') else '不可达'}",
    ]
    write_hint = str(rw.get("hint") or "") or str(vi.get("hint") or "")
    if write_hint:
        write_msg_parts.append(write_hint)
    checks.append({
        "key": "agent_write",
        "name": "Agent 写入地址",
        "lamp": _lamp(write_ok if (rw.get("url") or vi.get("url")) else None),
        "message": " · ".join(write_msg_parts),
        "last_at": "",
    })
    overall = "green" if all(c["lamp"] == "green" for c in checks) else (
        "red" if any(c["lamp"] == "red" for c in checks) else "yellow"
    )
    return {"overall": overall, "checks": checks, "engine": engine_state, "agent_write_urls": agent_urls}


@router.get("/dashboard")
def get_monitoring_dashboard(
    app_id: str = "",
    app_name: str = "",
    host_ips: str = Query(default=""),
    host_names: str = Query(default=""),
    range_minutes: int = Query(default=60, ge=MIN_DASHBOARD_RANGE_MINUTES, le=MAX_DASHBOARD_RANGE_MINUTES),
    step_seconds: int = Query(default=DEFAULT_STEP_SECONDS, ge=MIN_DASHBOARD_STEP_SECONDS, le=MAX_DASHBOARD_STEP_SECONDS),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """返回实时大盘需要的 VictoriaMetrics 数据。"""
    scope_host_ips = _split_scope_param(host_ips)
    scope_host_names = _split_scope_param(host_names)
    
    # 从数据库获取资产 IP 到资产元信息的映射关系
    assets = db.query(Asset).all()
    ip_to_asset = {
        asset.ip: {
            "name": asset.name,
            "provider": asset.provider or "unknown",
            "region": asset.region or "default",
        }
        for asset in assets
        if asset.ip
    }
    
    # 如果过滤条件中传入了主机名，通过资产数据库查出 IP 并动态补充到 scope_host_ips 中，以供大盘节点指标和排行过滤
    if scope_host_names:
        for asset in assets:
            if asset.name in scope_host_names and asset.ip and asset.ip not in scope_host_ips:
                scope_host_ips.append(asset.ip)
                
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {
            "configured": False,
            "message": "VictoriaMetrics 未配置",
            "metrics": [],
            "services": [],
            "alerts": [],
            "top_hosts": [],
            "top_filesystems": [],
            "recent_alerts": [],
            "summary": {"targets": 0, "up": 0, "down": 0, "alerts": 0, "load1": None},
            "range": {"minutes": range_minutes, "step_seconds": step_seconds},
            "scope": {
                "app_id": app_id,
                "app_name": app_name,
                "host_ips": scope_host_ips,
                "host_names": scope_host_names,
            },
        }

    safe_range_minutes, safe_step_seconds = _clamp_dashboard_range(
        range_minutes,
        step_seconds or int(cfg.get("default_step_seconds") or DEFAULT_STEP_SECONDS),
    )
    end = datetime.now()
    start = end - timedelta(minutes=safe_range_minutes)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    
    # 【对齐时序边界】将开始和结束时间戳向下对齐到步长（safe_step_seconds）的整数倍。
    # 为什么这么做？
    # VictoriaMetrics 在执行 query_range 时，其返回的序列时间戳默认会对齐到 step 边界。
    # 如果后端生成的 grid_timestamps 使用未对齐的时间戳（带任意秒数），两边的时间戳将完全无法匹配，
    # 导致映射出来的数据全部变成 None (null)，前端图表变空白。
    start_ts = (start_ts // safe_step_seconds) * safe_step_seconds
    end_ts = (end_ts // safe_step_seconds) * safe_step_seconds
    
    series_map: dict[str, dict[int, float]] = {}
    for key, query in DASHBOARD_RANGE_QUERIES.items():
        # PromQL 失败优雅降级：单条序列为空，不拖垮整个大盘
        try:
            payload = _vm_get(
                cfg,
                "/query_range",
                {
                    "query": query,
                    "start": start_ts,
                    "end": end_ts,
                    "step": safe_step_seconds,
                    "round_digits": 2,
                },
            )
            series_map[key] = {ts: value for ts, value in _parse_matrix_first_series(payload)}
        except Exception:
            series_map[key] = {}

    # Generate a complete grid of timestamps covering the full query range
    grid_timestamps = []
    current_ts = start_ts
    while current_ts <= end_ts:
        grid_timestamps.append(current_ts)
        current_ts += safe_step_seconds
    timestamps = grid_timestamps

    metrics = []
    # 横坐标格式：<1小时显示秒，<24小时显示时:分，>=24小时显示月-日 时:分
    if safe_range_minutes >= 1440:
        time_fmt = "%m-%d %H:%M"
    elif safe_step_seconds < 60:
        time_fmt = "%H:%M:%S"
    else:
        time_fmt = "%H:%M"
    for ts in timestamps:
        row = {"time": datetime.fromtimestamp(ts).strftime(time_fmt)}
        for key in DASHBOARD_RANGE_QUERIES.keys():
            row[key] = series_map.get(key, {}).get(ts, None)
        metrics.append(row)

    try:
        targets = _filter_dashboard_targets(
            _parse_up_targets(_vm_get(cfg, "/query", {"query": "up"}), ip_to_asset=ip_to_asset),
            scope_host_ips,
            scope_host_names,
        )
    except Exception:
        targets = []
    top_hosts = _fetch_top_hosts(cfg, scope_host_ips, scope_host_names)
    alerts = _filter_dashboard_alerts(_fetch_alerts(cfg), scope_host_ips, scope_host_names)
    # 只增不减：Load1 KPI + 文件系统 Top5 + 最近告警切片
    load1 = _fetch_summary_load1(cfg)
    top_filesystems = _fetch_top_filesystems(cfg)
    recent_alerts = [
        {
            "name": item.get("name") or item.get("rule") or "",
            "severity": item.get("severity") or "",
            "instance": item.get("instance") or item.get("host") or "",
            "time": item.get("time") or "",
            "status": item.get("status") or "",
            "id": item.get("id") or "",
        }
        for item in alerts[:8]
    ]
    up_count = sum(1 for item in targets if item.get("status") == "healthy")
    down_count = max(0, len(targets) - up_count)
    return {
        "configured": True,
        "message": "",
        "metrics": metrics,
        "services": targets,
        "alerts": alerts,
        "top_hosts": top_hosts,
        "top_filesystems": top_filesystems,
        "recent_alerts": recent_alerts,
        "summary": {
            "targets": len(targets),
            "up": up_count,
            "down": down_count,
            "alerts": len(alerts),
            "load1": load1,
        },
        "range": {"minutes": safe_range_minutes, "step_seconds": safe_step_seconds},
        "scope": {
            "app_id": app_id,
            "app_name": app_name,
            "host_ips": scope_host_ips,
            "host_names": scope_host_names,
        },
    }


# ---------- 单主机指标查询（供主机管理页弹窗使用） ----------

HOST_METRIC_QUERIES = {
    "cpu": '100 - (avg(rate(node_cpu_seconds_total{{mode="idle",instance=~"{host}"}}[5m])) * 100)',
    "memory": '(1 - (avg(node_memory_MemAvailable_bytes{{instance=~"{host}"}}) / avg(node_memory_MemTotal_bytes{{instance=~"{host}"}}))) * 100',
    "memTotal": 'avg(node_memory_MemTotal_bytes{{instance=~"{host}"}})',
    "memAvailable": 'avg(node_memory_MemAvailable_bytes{{instance=~"{host}"}})',
    "networkIn": 'sum(rate(node_network_receive_bytes_total{{instance=~"{host}",device!="lo"}}[5m])) / 1024 / 1024',
    "networkOut": 'sum(rate(node_network_transmit_bytes_total{{instance=~"{host}",device!="lo"}}[5m])) / 1024 / 1024',
    # 只增不减：对比模式补 Load / 磁盘读写吞吐序列（跳过 loop/ram 虚拟盘）
    "load1": 'avg(node_load1{{instance=~"{host}"}})',
    "diskRead": 'sum(rate(node_disk_read_bytes_total{{instance=~"{host}",device!~"loop.*|ram.*"}}[5m])) / 1024 / 1024',
    "diskWrite": 'sum(rate(node_disk_written_bytes_total{{instance=~"{host}",device!~"loop.*|ram.*"}}[5m])) / 1024 / 1024',
}


@router.get("/host-metrics")
def get_host_metrics(
    host_name: str = Query(default=""),
    host_ip: str = Query(default=""),
    range_minutes: int = Query(default=60, ge=1, le=43200),
    step_seconds: int = Query(default=60, ge=10, le=86400),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """返回单台主机的 CPU/内存/网络时序指标，供主机管理页弹窗使用。

    安全边界：
    - 只使用后端内置查询模板，前端不能传 PromQL；
    - host_name / host_ip 仅作为 instance 标签匹配，不拼接到其他位置。
    """
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置", "metrics": []}

    resolved_ip = host_ip.strip() if host_ip else ""
    resolved_name = host_name.strip() if host_name else ""

    if not resolved_ip and resolved_name:
        asset = db.query(Asset).filter(Asset.name == resolved_name).first()
        if asset:
            resolved_ip = asset.ip
    elif resolved_ip and not resolved_name:
        asset = db.query(Asset).filter(Asset.ip == resolved_ip).first()
        if asset:
            resolved_name = asset.name

    if resolved_ip and resolved_name:
        host_filter = prom_instance_regex(resolved_ip, resolved_name)
    else:
        host_filter = prom_instance_regex(resolved_ip or resolved_name)

    if not host_filter:
        return {"configured": True, "message": "未指定主机", "metrics": []}

    safe_range, safe_step = _clamp_dashboard_range(range_minutes, step_seconds)
    end = datetime.now()
    start = end - timedelta(minutes=safe_range)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    # 【对齐时序边界】将开始和结束时间戳向下对齐到步长（safe_step）的整数倍，
    # 保证后端格栅时间戳与 VictoriaMetrics 查询返回的时间戳完全匹配。
    start_ts = (start_ts // safe_step) * safe_step
    end_ts = (end_ts // safe_step) * safe_step

    series_map: dict[str, dict[int, float]] = {}
    for key, query_tpl in HOST_METRIC_QUERIES.items():
        query = query_tpl.format(host=host_filter)
        try:
            payload = _vm_get(
                cfg,
                "/query_range",
                {
                    "query": query,
                    "start": start_ts,
                    "end": end_ts,
                    "step": safe_step,
                    "round_digits": 2,
                },
            )
            series_map[key] = {ts: value for ts, value in _parse_matrix_first_series(payload)}
        except Exception:
            # PromQL / 网络失败一律降级为空序列，不 500
            series_map[key] = {}

    # 以物理磁盘为维度，对趋势数据进行合并处理（不再过滤引导分区，从而获得物理盘整体使用率）
    size_query = 'node_filesystem_size_bytes{{instance=~"{host}",fstype!~"tmpfs|squashfs|iso9660"}}'.format(host=host_filter)
    free_query = 'node_filesystem_free_bytes{{instance=~"{host}",fstype!~"tmpfs|squashfs|iso9660"}}'.format(host=host_filter)
    disk_series: dict[str, dict[int, float]] = {} # base_disk -> {ts: value}
    try:
        size_payload = _vm_get(
            cfg,
            "/query_range",
            {
                "query": size_query,
                "start": start_ts,
                "end": end_ts,
                "step": safe_step,
                "round_digits": 2,
            },
        )
        free_payload = _vm_get(
            cfg,
            "/query_range",
            {
                "query": free_query,
                "start": start_ts,
                "end": end_ts,
                "step": safe_step,
                "round_digits": 2,
            },
        )
        
        # 1. 汇总各个时间戳上每个物理磁盘的分区总空间
        size_data: dict[str, dict[int, float]] = {}
        for item in size_payload.get("data", {}).get("result", []):
            metric = item.get("metric") or {}
            device = str(metric.get("device") or "")
            if not device:
                continue
                
            base_device = _get_base_disk(device)
            if base_device not in size_data:
                size_data[base_device] = {}
                
            for point in item.get("values") or []:
                if isinstance(point, list) and len(point) >= 2:
                    try:
                        ts = int(float(point[0]))
                        val = float(point[1])
                        size_data[base_device][ts] = size_data[base_device].get(ts, 0.0) + val
                    except Exception:
                        continue

        # 2. 汇总各个时间戳上每个物理磁盘的分区剩余空间
        free_data: dict[str, dict[int, float]] = {}
        for item in free_payload.get("data", {}).get("result", []):
            metric = item.get("metric") or {}
            device = str(metric.get("device") or "")
            if not device:
                continue
                
            base_device = _get_base_disk(device)
            if base_device not in free_data:
                free_data[base_device] = {}
                
            for point in item.get("values") or []:
                if isinstance(point, list) and len(point) >= 2:
                    try:
                        ts = int(float(point[0]))
                        val = float(point[1])
                        free_data[base_device][ts] = free_data[base_device].get(ts, 0.0) + val
                    except Exception:
                        continue

        # 3. 计算物理磁盘的使用率，生成时序序列
        for base_device, ts_sizes in size_data.items():
            ts_frees = free_data.get(base_device, {})
            disk_series[base_device] = {}
            for ts, size_val in ts_sizes.items():
                if size_val > 0:
                    free_val = ts_frees.get(ts, 0.0)
                    used_val = size_val - free_val
                    disk_series[base_device][ts] = round((used_val / size_val) * 100, 2)
    except Exception:
        pass

    # Generate a complete grid of timestamps covering the full query range
    grid_timestamps = []
    current_ts = start_ts
    while current_ts <= end_ts:
        grid_timestamps.append(current_ts)
        current_ts += safe_step
    timestamps = grid_timestamps

    metrics = []
    if safe_range >= 1440:
        time_fmt = "%m-%d %H:%M"
    elif safe_step < 60:
        time_fmt = "%H:%M:%S"
    else:
        time_fmt = "%H:%M"

    for ts in timestamps:
        row = {"time": datetime.fromtimestamp(ts).strftime(time_fmt)}
        for key in HOST_METRIC_QUERIES:
            row[key] = series_map.get(key, {}).get(ts, None)
        for mountpoint, values in disk_series.items():
            row[f"disk_{mountpoint}"] = values.get(ts, None)
        metrics.append(row)

    return {
        "configured": True,
        "message": "",
        "host_name": host_filter,
        "metrics": metrics,
        "disk_partitions": list(disk_series.keys()),
    }


@router.get("/host-summary")
def get_host_summary(
    host_name: str = Query(default=""),
    host_ip: str = Query(default=""),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """返回单台主机的基本硬件资源及磁盘分区摘要。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置"}

    resolved_ip = host_ip.strip() if host_ip else ""
    resolved_name = host_name.strip() if host_name else ""

    if not resolved_ip and resolved_name:
        asset = db.query(Asset).filter(Asset.name == resolved_name).first()
        if asset:
            resolved_ip = asset.ip
    elif resolved_ip and not resolved_name:
        asset = db.query(Asset).filter(Asset.ip == resolved_ip).first()
        if asset:
            resolved_name = asset.name

    if resolved_ip and resolved_name:
        host_filter = prom_instance_regex(resolved_ip, resolved_name)
    else:
        host_filter = prom_instance_regex(resolved_ip or resolved_name)

    if not host_filter:
        return {"configured": True, "message": "未指定主机", "summary": None}

    summary = {
        "cpu_cores": 0,
        "cpu_usage": 0.0,
        "cpu_iowait": 0.0,
        "mem_total": 0,
        "mem_used": 0,
        "mem_usage": 0.0,
        "uptime_seconds": 0,
        "file_descriptors": 0,
        "filesystems": []
    }

    try:
        # 1. 运行时间
        uptime_payload = _vm_get(cfg, "/query", {"query": f'time() - node_boot_time_seconds{{instance=~"{host_filter}"}}'})
        res = uptime_payload.get("data", {}).get("result", [])
        if res:
            summary["uptime_seconds"] = int(float(res[0]["value"][1]))

        # 2. CPU 核数
        cores_payload = _vm_get(cfg, "/query", {"query": f'count(node_cpu_seconds_total{{mode="idle",instance=~"{host_filter}"}})'})
        res = cores_payload.get("data", {}).get("result", [])
        if res:
            summary["cpu_cores"] = int(float(res[0]["value"][1]))

        # 3. 内存总量与已用
        mem_total_p = _vm_get(cfg, "/query", {"query": f'node_memory_MemTotal_bytes{{instance=~"{host_filter}"}}'})
        res_t = mem_total_p.get("data", {}).get("result", [])
        if res_t:
            summary["mem_total"] = int(float(res_t[0]["value"][1]))

        mem_avail_p = _vm_get(cfg, "/query", {"query": f'node_memory_MemAvailable_bytes{{instance=~"{host_filter}"}}'})
        res_a = mem_avail_p.get("data", {}).get("result", [])
        if res_a and summary["mem_total"] > 0:
            avail = int(float(res_a[0]["value"][1]))
            summary["mem_used"] = summary["mem_total"] - avail
            summary["mem_usage"] = round((1 - avail / summary["mem_total"]) * 100, 2)

        # 4. CPU 使用率
        cpu_payload = _vm_get(cfg, "/query", {"query": f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",instance=~"{host_filter}"}}[5m])) * 100)'})
        res = cpu_payload.get("data", {}).get("result", [])
        if res:
            summary["cpu_usage"] = round(float(res[0]["value"][1]), 2)

        # 5. CPU iowait
        iowait_payload = _vm_get(cfg, "/query", {"query": f'avg(rate(node_cpu_seconds_total{{mode="iowait",instance=~"{host_filter}"}}[5m])) * 100'})
        res = iowait_payload.get("data", {}).get("result", [])
        if res:
            summary["cpu_iowait"] = round(float(res[0]["value"][1]), 2)

        # 6. 文件描述符数
        fd_payload = _vm_get(cfg, "/query", {"query": f'node_filefd_allocated{{instance=~"{host_filter}"}}'})
        res = fd_payload.get("data", {}).get("result", [])
        if res:
            summary["file_descriptors"] = int(float(res[0]["value"][1]))
            
        # 7. 分区空间列表以物理磁盘为维度汇总展示（过滤特殊/临时文件系统，合并分区到主磁盘设备）
        fs_size_p = _vm_get(cfg, "/query", {"query": f'node_filesystem_size_bytes{{instance=~"{host_filter}",fstype!~"tmpfs|squashfs|iso9660"}}'})
        fs_free_p = _vm_get(cfg, "/query", {"query": f'node_filesystem_free_bytes{{instance=~"{host_filter}",fstype!~"tmpfs|squashfs|iso9660"}}'})
        
        sizes_list = fs_size_p.get("data", {}).get("result", [])
        frees_map = {
            (str(item["metric"].get("device")), str(item["metric"].get("mountpoint"))): item 
            for item in fs_free_p.get("data", {}).get("result", [])
        }
        
        disk_groups = {}
        for size_item in sizes_list:
            metric = size_item.get("metric") or {}
            device = str(metric.get("device") or "")
            mountpoint = str(metric.get("mountpoint") or "")
            fstype = str(metric.get("fstype") or "")
            
            free_item = frees_map.get((device, mountpoint))
            if not free_item:
                continue
                
            try:
                size_val = float(size_item["value"][1])
                free_val = float(free_item["value"][1])
            except (ValueError, TypeError, IndexError):
                continue
                
            base_device = _get_base_disk(device)
                    
            if base_device not in disk_groups:
                disk_groups[base_device] = {
                    "device": base_device,
                    "fstypes": set(),
                    "mountpoints": set(),
                    "total_bytes": 0.0,
                    "free_bytes": 0.0,
                }
                
            disk_groups[base_device]["total_bytes"] += size_val
            disk_groups[base_device]["free_bytes"] += free_val
            if fstype:
                disk_groups[base_device]["fstypes"].add(fstype)
            if mountpoint:
                disk_groups[base_device]["mountpoints"].add(mountpoint)
                
        filesystems = []
        for base_device, info in disk_groups.items():
            total_val = info["total_bytes"]
            free_val = info["free_bytes"]
            used_val = total_val - free_val
            usage_pct = round((1 - free_val / total_val) * 100, 2) if total_val > 0 else 0.0
            
            fstype_str = ", ".join(sorted(info["fstypes"]))
            
            # 将 "/" 根挂载点排序置顶，其余字母排序后逗号拼接展示
            mounts = list(info["mountpoints"])
            if "/" in mounts:
                mounts.remove("/")
                mounts = ["/"] + sorted(mounts)
            else:
                mounts = sorted(mounts)
            mountpoint_str = ", ".join(mounts)
            
            filesystems.append({
                "device": base_device,
                "fstype": fstype_str,
                "mountpoint": mountpoint_str,
                "total_bytes": int(total_val),
                "free_bytes": int(free_val),
                "used_bytes": int(used_val),
                "usage_percentage": usage_pct
            })
        summary["filesystems"] = filesystems

    except Exception:
        pass

    return {
        "configured": True,
        "message": "",
        "host_name": resolved_name or host_filter,
        "summary": summary
    }


def _builtin_ready(db: Session) -> bool:
    """内置采集是否可用：监控连接已写入即认为本机栈就绪。"""
    from app.api.monitoring.services.config_store import read_monitoring_config

    cfg = read_monitoring_config(db)
    return bool(str(cfg.get("vm_url") or "").strip() and str(cfg.get("vlogs_url") or "").strip())


def _builtin_target_record(asset: Asset, ready: bool) -> dict[str, Any]:
    """本机指标 target：内置 Alloy 自动采集，不走 SSH 安装。"""
    from app.api.assets import now_ts

    now = now_ts() if ready else ""
    return {
        "asset_id": asset.instance_id,
        "name": asset.name,
        "ip": asset.ip,
        "provider": str(asset.provider or "localhost"),
        "region": str(asset.region or ""),
        "port": 0,
        "job": "opspilot-node",
        "enabled": ready,
        "source": "builtin",
        "agent": "builtin-alloy",
        "target": "builtin",
        "status": "up" if ready else "disabled",
        "last_checked_at": now,
        "last_error": "",
        "metrics_enabled": ready,
        "logs_enabled": ready,
        "last_metric_at": now,
        "last_log_at": now,
    }


@router.get("/targets")
def get_monitoring_targets(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.assets.hosts")),
):
    """返回 OpsPilot 维护的 vmagent target 清单。"""
    saved = read_monitoring_targets(db)["targets"]
    saved_map = {str(item.get("asset_id") or ""): item for item in saved if isinstance(item, dict)}
    agents = db.query(CollectorAgent).order_by(CollectorAgent.id.asc()).all()
    agent_map = {agent.asset_id: agent for agent in agents if agent.asset_id}
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    from app.api.monitoring.services.alloy_policy import (
        _log_collect_state,
        _metrics_collect_state,
    )
    log_saved_map = {
        str(item.get("asset_id") or ""): item
        for item in read_log_targets(db)["targets"]
        if isinstance(item, dict)
    }
    builtin_ready = _builtin_ready(db)
    result: list[dict[str, Any]] = []
    for asset in assets:
        agent = agent_map.get(asset.instance_id)
        if agent:
            record = _agent_status_record(asset, agent)
            log_item = log_saved_map.get(asset.instance_id) or {}
            record.update(_metrics_collect_state(agent, target_enabled=bool(record.get("enabled"))))
            record.update(
                _log_collect_state(
                    db,
                    asset,
                    agent,
                    target_enabled=bool(log_item.get("enabled")) if log_item else False,
                )
            )
            result.append(record)
            continue
        # 本机：内置监控栈自动采集（无 Agent 也显示在采）
        if str(asset.provider or "").strip().lower() == "localhost":
            result.append(_builtin_target_record(asset, builtin_ready))
            continue
        item = saved_map.get(asset.instance_id)
        if item:
            result.append({**item, "name": asset.name, "ip": asset.ip, "target": f"{asset.ip}:{int(item.get('port') or DEFAULT_NODE_EXPORTER_PORT)}"})
        else:
            result.append({
                "asset_id": asset.instance_id,
                "name": asset.name,
                "ip": asset.ip,
                "port": DEFAULT_NODE_EXPORTER_PORT,
                "job": "opspilot-node",
                "enabled": False,
                "source": "asset",
                "agent": "alloy",
                "target": f"{asset.ip}:{DEFAULT_NODE_EXPORTER_PORT}",
                "status": "disabled",
                "last_checked_at": "",
                "last_error": "",
            })
    enabled = [item for item in result if item.get("enabled")]
    return {"targets": result, "enabled_count": len(enabled), "total": len(result)}


@router.post("/targets/{asset_id}/enable")
def enable_monitoring_target(
    asset_id: str,
    payload: TargetEnableRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """把主机加入 vmagent target 清单。"""
    asset = _get_asset_or_404(db, asset_id)
    ok, msg = _check_exporter_endpoint(asset.ip, payload.port)
    saved = read_monitoring_targets(db)["targets"]
    record = _asset_target_record(asset, payload.port, payload.job, "up" if ok else "unreachable", "" if ok else msg)
    sync_info = _persist_targets_and_sync_vmagent(db, _upsert_target(saved, record))
    return {**record, **sync_info}


@router.delete("/targets/{asset_id}")
def disable_monitoring_target(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """停用主机默认可观测性接入；无 Agent 时兼容旧 vmagent target 清单。"""
    asset = _get_asset_or_404(db, asset_id)
    agent = _latest_agent_for_asset(db, asset_id)
    if agent:
        ok, msg, out, err, code = _run_asset_ssh(db, asset, _build_alloy_disable_command(), 60, current_user)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        if code != 0:
            raise HTTPException(status_code=400, detail=(err or msg or f"停用 Alloy Agent 失败，退出码 {code}")[:500])
        agent.status = "disabled"
        agent.last_error = ""
        agent.updated_at = _collector_now_ts()
        saved_targets = read_monitoring_targets(db)["targets"]
        save_monitoring_targets(db, [item for item in saved_targets if isinstance(item, dict) and item.get("asset_id") != asset_id])
        saved_logs = read_log_targets(db)["targets"]
        save_log_targets(db, [item for item in saved_logs if isinstance(item, dict) and item.get("asset_id") != asset_id])
        db.commit()
        return {"success": True, "message": "Alloy Agent 已停用", "stdout": out[-2000:], "stderr": err[-2000:]}

    saved = read_monitoring_targets(db)["targets"]
    next_targets = [item for item in saved if isinstance(item, dict) and item.get("asset_id") != asset_id]
    sync_info = _persist_targets_and_sync_vmagent(db, next_targets)
    return {"success": True, **sync_info}


@router.post("/targets/{asset_id}/check")
def check_monitoring_target(
    asset_id: str,
    payload: TargetEnableRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """检查主机 Alloy Agent 心跳；无 Agent 时兼容旧 node_exporter target。"""
    asset = _get_asset_or_404(db, asset_id)
    agent = _latest_agent_for_asset(db, asset_id)
    if agent:
        record = _agent_status_record(asset, agent)
        return {**record, "message": record["last_error"] or ("Alloy Agent 心跳正常" if record["status"] == "up" else "Alloy Agent 心跳异常")}
    ok, msg = _check_exporter_endpoint(asset.ip, payload.port)
    saved = read_monitoring_targets(db)["targets"]
    record = _asset_target_record(asset, payload.port, payload.job, "up" if ok else "unreachable", "" if ok else msg)
    record["enabled"] = any(item.get("asset_id") == asset_id and item.get("enabled") for item in saved if isinstance(item, dict))
    sync_info = _persist_targets_and_sync_vmagent(db, _upsert_target(saved, record))
    return {**record, **sync_info}


@router.post("/targets/{asset_id}/install-node-exporter")
def install_node_exporter(
    asset_id: str,
    payload: TargetEnableRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """兼容旧接口：实际安装 OpsPilot Alloy Agent。"""
    return install_alloy_agent(asset_id, request, db, current_user)


@router.post("/targets/{asset_id}/uninstall-alloy")
def uninstall_alloy_agent(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """彻底卸载主机上的 Alloy Agent —— 「装了什么就删什么」。

    与停用(`DELETE /targets/{id}`,只 systemctl disable)不同,本接口 SSH 到目标机
    执行完整卸载命令(停服务/删二进制/配置/数据/用户/源),并清理 OpsPilot 侧
    全部状态(collector_agents 行 + monitoring_targets + log_targets)。

    容错降级：若 SSH 不可达(目标机关机/网络不通),不阻断本地清理 —— 仍删 DB 记录,
    并提示远程 alloy 可能残留、需手动卸载(degraded=True),避免本地灯长期卡在旧绿状态。
    """
    asset = _get_asset_or_404(db, asset_id)
    ensure_asset_credential_access(db, asset, current_user)

    remote_ok = True
    remote_degraded = False
    out = ""
    err = ""
    code = 0
    msg = ""
    summary = ""

    # 1) 远程 SSH 执行彻底卸载;失败不抛异常,降级为仅清理本地记录
    try:
        ok, msg, out, err, code = _run_asset_ssh(
            db, asset, _build_alloy_uninstall_command(), 120, current_user
        )
        combined = f"{out}\n{err}"
        if not ok:
            remote_ok = False
            remote_degraded = True
            summary = f"远程卸载执行失败：{(msg or err or '未知错误')[:200]}"
        elif "ALLOY_UNINSTALL_INCOMPLETE" in combined:
            remote_ok = False
            remote_degraded = True
            tail = combined.split("ALLOY_UNINSTALL_INCOMPLETE:", 1)[-1].strip().splitlines()[0] if "ALLOY_UNINSTALL_INCOMPLETE:" in combined else ""
            summary = f"远程卸载未完全清理,残留：{tail[:200]}"
        elif code != 0:
            remote_ok = False
            remote_degraded = True
            summary = f"远程卸载脚本退出码 {code}"
        else:
            summary = "Alloy 已从目标主机彻底卸载"
    except Exception as exc:
        remote_ok = False
        remote_degraded = True
        summary = f"SSH 不可达,已仅清理本地记录(远程 alloy 可能仍在运行,请手动卸载)：{str(exc)[:200]}"
        err = str(exc)

    # 2) 清理 OpsPilot 侧全部状态(无论远程成败都执行,避免本地灯残留)
    removed_agents = (
        db.query(CollectorAgent)
        .filter(CollectorAgent.asset_id == asset_id)
        .delete(synchronize_session=False)
    )
    saved_targets = read_monitoring_targets(db)["targets"]
    save_monitoring_targets(
        db,
        [item for item in saved_targets if isinstance(item, dict) and item.get("asset_id") != asset_id],
    )
    saved_logs = read_log_targets(db)["targets"]
    save_log_targets(
        db,
        [item for item in saved_logs if isinstance(item, dict) and item.get("asset_id") != asset_id],
    )
    from app.api.collector import _append_collector_audit_log

    _append_collector_audit_log(
        db,
        username=current_user.username,
        action="卸载 Alloy Agent",
        resource=f"asset {asset_id} ({asset.ip})",
    )
    db.commit()

    return {
        "success": remote_ok,
        "degraded": remote_degraded,
        "message": summary,
        "removed_agents": removed_agents,
        "stdout": (out or "")[-4000:],
        "stderr": (err or "")[-2000:],
        "exit_code": code,
    }


def _classify_install_error(msg: str, out: str, err: str, code: int) -> str:
    """把 SSH 原始错误分类为用户可读的中文提示。"""
    combined = f"{msg}\n{out}\n{err}".lower()
    # curl 下载安装脚本失败（远程机器无法回连 OpsPilot 后端）
    if "curl" in combined and ("connection refused" in combined or "couldn't connect" in combined or "failed to connect" in combined):
        return "远程主机无法连接 OpsPilot 后端（curl 下载安装脚本失败），请检查目标主机到后端的网络可达性。"
    if "connection refused" in combined or "no route to host" in combined:
        return "SSH 连接被拒绝，请检查目标主机 SSH 端口是否开放。"
    if "authentication failed" in combined or "permission denied" in combined:
        return "SSH 认证失败，请确认凭证（密码或私钥）与登录用户是否正确。"
    if "name or service not known" in combined or "could not resolve" in combined:
        return "SSH 主机名解析失败，请检查主机 IP 是否可达。"
    if "timed out" in combined or "timeout" in combined:
        return "SSH 连接超时，请检查网络可达性或防火墙规则。"
    if "curl" in combined and ("failed" in combined or "error" in combined):
        return "安装脚本下载失败，请确认目标主机能访问 OpsPilot 后端地址（检查防火墙/代理）。"
    if "systemd" in combined and ("not found" in combined or "no such" in combined):
        return "目标主机未找到 systemd，Alloy Agent 需要 systemd 环境才能注册为服务。"
    if "no space left" in combined:
        return "目标主机磁盘空间不足，请清理后重试。"
    if code == 127:
        return "安装命令依赖缺失（bash/curl），请确认目标主机已安装基础工具。"
    # 兜底：保留原始 stderr 尾部供运维参考
    raw = (err or out or msg or "").strip()
    return f"安装失败（退出码 {code}）：{raw[-300:]}" if raw else f"安装失败，退出码 {code}。"


@router.post("/targets/{asset_id}/install-alloy")
def install_alloy_agent(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """通过 SSH 安装 Alloy Agent，并发布默认指标/日志采集策略。

    防重入：检查该资产是否已有 installing 状态的 Agent，有则拒绝重复提交。
    """
    asset = _get_asset_or_404(db, asset_id)

    # 本机主机（provider=localhost）由内置 Alloy 采集，不需要 SSH 安装
    if str(asset.provider or "").strip().lower() == "localhost":
        raise HTTPException(status_code=400, detail="本机主机由监控栈内置 Alloy 自动采集（host 指标 + Docker 日志），无需手动安装 Agent。")

    # 如果目标主机 IP 与本机 ins-localhost 一致，说明是同一台机器的重复纳管
    from app.db.bootstrap import LOCAL_HOST_INSTANCE_ID
    localhost_asset = db.query(Asset).filter(Asset.instance_id == LOCAL_HOST_INSTANCE_ID).first()
    if localhost_asset and str(asset.ip or "").strip() == str(localhost_asset.ip or "").strip():
        raise HTTPException(status_code=400, detail="该主机 IP 与本机一致，监控由内置 Alloy 自动采集，无需重复安装。")

    ensure_asset_credential_access(db, asset, current_user)

    # 缺口2：防重入 —— installing 状态说明上一次安装 SSH 任务尚未结束
    existing = _latest_agent_for_asset(db, asset_id)
    if existing and existing.status == "installing":
        raise HTTPException(status_code=409, detail="该主机正在安装中，请等待当前安装任务完成后再重试。")

    # 标记为 installing，防止并发触发（刷新页面后再次点击）
    if existing:
        existing.status = "installing"
        existing.last_error = ""
        existing.updated_at = _collector_now_ts()
        db.commit()

    try:
        register_token = _create_collector_register_token(db, current_user, asset)
        base_url = _external_request_base_url(request)
        command = _build_alloy_install_command(base_url, register_token, asset)
        ok, msg, out, err, code = _run_asset_ssh(db, asset, command, 240, current_user)
    except Exception as exc:
        # SSH 本身异常，还原 installing 状态
        if existing:
            existing.status = "offline"
            existing.last_error = str(exc)[:255]
            existing.updated_at = _collector_now_ts()
            db.commit()
        raise HTTPException(status_code=400, detail=f"SSH 执行异常：{str(exc)[:300]}") from exc

    if not ok or code != 0:
        # 缺口3：智能错误识别 + 诊断上下文
        detail = _classify_install_error(msg, out, err, code)
        # 关键修复：脚本可能在失败前已经注册了一个新 Agent（如 apt 装 alloy 前先注册），
        # 该 Agent 的一次孤立心跳会把状态刷成 online，掩盖安装失败（228 踩过的坑）。
        # 置 offline 仍会被残留心跳刷回（228 的变体，实测踩过），这里直接删除失败安装
        # 注册的 Agent 记录——残余回调因记录不存在而失效，下次真实安装会重新注册。
        failed_agent = _latest_agent_for_asset(db, asset_id) or existing
        if failed_agent:
            db.delete(failed_agent)
            db.commit()
        return JSONResponse(status_code=400, content={
            "detail": detail,
            "diagnostic": {
                "callback_url": base_url,
                "ssh_msg": msg[:200] if msg else "",
                "stdout_tail": (out or "")[-500:],
                "stderr_tail": (err or "")[-500:],
                "exit_code": code,
            },
        })

    agent = _latest_agent_for_asset(db, asset_id)
    if not agent:
        detail = _classify_install_error(msg, out, err, code) if code != 0 else "Alloy Agent 已执行安装脚本，但未完成注册心跳，请稍后在监控列表中刷新确认。"
        raise HTTPException(status_code=400, detail=detail)
    (
        db.query(CollectorAgent)
        .filter(CollectorAgent.asset_id == asset_id, CollectorAgent.agent_id != agent.agent_id)
        .update(
            {
                "status": "disabled",
                "last_error": f"已被新 Agent {agent.agent_id} 替换",
                "updated_at": _collector_now_ts(),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    version = _ensure_asset_alloy_policy(db, asset, agent, current_user)
    record = _agent_status_record(asset, agent, enabled=True)
    save_monitoring_targets(db, _upsert_target(read_monitoring_targets(db)["targets"], record))
    db.commit()
    log_record = _asset_log_target_record(asset, "alloy-default", record["status"], record["last_error"], enabled=True)
    log_sync_info = _persist_log_targets(db, _upsert_target(read_log_targets(db)["targets"], log_record), current_user=current_user)

    # 安装后验证：SSH 检查 Alloy 服务是否实际在运行
    # 即使脚本退出码为 0，也要确认服务真的 active；否则注册阶段的一次孤立心跳
    # 会把 Agent 状态刷成 online，3 分钟后才过期变黄，用户看不出是"装了一半"。
    alloy_active = True
    alloy_status_detail = ""
    try:
        _, _, svc_out, svc_err, svc_code = _run_asset_ssh(
            db, asset, "systemctl is-active alloy 2>&1 && echo '---OK---' || journalctl -u alloy --no-pager -n 8 2>&1", 15, current_user
        )
        svc_combined = f"{svc_out}\n{svc_err}".strip()
        if "---OK---" not in svc_combined:
            alloy_active = False
            alloy_status_detail = svc_combined[-600:]
            fail_reason = f"Alloy 服务未运行: {alloy_status_detail[:200]}"
            # 更新 target 状态为 unreachable
            record["status"] = "unreachable"
            record["last_error"] = fail_reason
            save_monitoring_targets(db, _upsert_target(read_monitoring_targets(db)["targets"], record))
            # 关键修复：同步把 Agent 本身标记为失败态，否则注册时那次孤立心跳
            # 会让列表持续显示 online，掩盖"服务没起来"的事实。
            agent.status = "offline"
            agent.last_error = fail_reason[:255]
            agent.updated_at = _collector_now_ts()
            db.commit()
    except Exception:
        pass

    # 通知用户：Agent 安装结果
    # NotificationCreate 与 create_notification 同在 app.api.notifications；
    # 此前误写成 app.schemas.notifications（不存在的模块），导致安装成功路径 500
    from app.api.notifications import create_notification, NotificationCreate
    success_flag = alloy_active and record["status"] == "up"
    try:
        create_notification(
            db,
            data=NotificationCreate(
                user_id=current_user.id,
                type="success" if success_flag else "error",
                category="monitoring",
                title="Agent 安装完成" if success_flag else "Agent 安装失败",
                content=f"{asset.name}（{asset.ip}）监控 Agent {'已成功安装并运行' if success_flag else '安装失败：' + alloy_status_detail[:200] if alloy_status_detail else '服务未正常启动'}",
                link="/assets/hosts",
                resource_id=asset_id,
            ),
        )
    except Exception:
        pass

    return {
        "success": success_flag,
        "message": "Alloy Agent 已安装，默认指标和日志策略已发布" if alloy_active else "Alloy Agent 已安装但服务启动失败，请检查配置",
        "target": record,
        "agent_id": agent.agent_id,
        "version_id": version.version_id,
        "stdout": out[-2000:],
        "stderr": err[-2000:],
        "alloy_active": alloy_active,
        "alloy_status_detail": alloy_status_detail,
        **log_sync_info,
    }


@router.get("/targets/{asset_id}/install-log")
def stream_install_log(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """缺口4：SSE 流，推送该主机最近一次安装的状态与日志摘要。

    前端建立 EventSource 连接后，每 2 秒推送一次当前 Agent 状态；
    安装完成或失败后推送 done 事件并关闭连接（最多轮询 150 秒）。
    """
    import time
    from fastapi.responses import StreamingResponse

    asset = _get_asset_or_404(db, asset_id)

    def _event(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def generate():
        deadline = time.time() + 150
        while time.time() < deadline:
            db.expire_all()
            agent = _latest_agent_for_asset(db, asset_id)
            status = agent.status if agent else "pending"
            last_error = agent.last_error if agent else ""
            yield _event("status", {
                "status": status,
                "agent_id": agent.agent_id if agent else "",
                "last_error": last_error,
                "hint": _classify_install_error("", "", last_error, 1) if last_error and status not in {"online", "up"} else "",
            })
            if status in {"online", "up", "offline", "disabled"} and status != "installing":
                yield _event("done", {"status": status, "success": status in {"online", "up"}})
                return
            time.sleep(2)
        yield _event("done", {"status": "timeout", "success": False})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/log-targets")
def get_log_targets(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.assets.hosts")),
):
    """返回主机日志采集纳管清单。"""
    saved = read_log_targets(db)["targets"]
    saved_map = {str(item.get("asset_id") or ""): item for item in saved if isinstance(item, dict)}
    agents = db.query(CollectorAgent).order_by(CollectorAgent.id.asc()).all()
    agent_map = {agent.asset_id: agent for agent in agents if agent.asset_id}
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    builtin_ready = _builtin_ready(db)
    result: list[dict[str, Any]] = []
    for asset in assets:
        agent = agent_map.get(asset.instance_id)
        if agent:
            from app.api.monitoring.services.alloy_policy import (
                _log_collect_state,
                _metrics_collect_state,
            )
            record = _agent_status_record(asset, agent)
            log_item = saved_map.get(asset.instance_id) or {}
            log_state = _log_collect_state(
                db,
                asset,
                agent,
                target_enabled=bool(log_item.get("enabled")) if log_item else bool(record["enabled"]),
            )
            metric_state = _metrics_collect_state(agent, target_enabled=bool(record["enabled"]))
            result.append(
                {
                    "asset_id": asset.instance_id,
                    "name": asset.name,
                    "ip": asset.ip,
                    "provider": str(asset.provider or ""),
                    "region": str(asset.region or ""),
                    "agent": "alloy",
                    "profile": "alloy-default",
                    "enabled": bool(record["enabled"]),
                    "status": record["status"],
                    "config_path": "/etc/alloy/config.alloy",
                    "last_checked_at": record["last_checked_at"],
                    "last_error": record["last_error"],
                    "agent_id": record["agent_id"],
                    **log_state,
                    **metric_state,
                }
            )
            continue
        # 本机：内置 Alloy 的 loki.source.docker 自动采容器日志
        if str(asset.provider or "").strip().lower() == "localhost":
            result.append(_builtin_target_record(asset, builtin_ready))
            continue
        item = saved_map.get(asset.instance_id)
        if item:
            result.append(
                {
                    **item,
                    "name": asset.name,
                    "ip": asset.ip,
                    "provider": str(asset.provider or ""),
                    "region": str(asset.region or ""),
                    "agent": str(item.get("agent") or "vector"),
                    "config_path": str(item.get("config_path") or "/etc/vector/vector.toml"),
                    "profile": str(item.get("profile") or "linux-default"),
                    "enabled": bool(item.get("enabled")),
                }
            )
        else:
            result.append(
                {
                    "asset_id": asset.instance_id,
                    "name": asset.name,
                    "ip": asset.ip,
                    "provider": str(asset.provider or ""),
                    "region": str(asset.region or ""),
                    "agent": "alloy",
                    "profile": "linux-default",
                    "enabled": False,
                    "status": "disabled",
                    "config_path": "/etc/alloy/config.alloy",
                    "last_checked_at": "",
                    "last_error": "",
                }
            )
    enabled = [item for item in result if item.get("enabled")]
    return {"targets": result, "enabled_count": len(enabled), "total": len(result)}


@router.post("/log-targets/{asset_id}/install-vector")
def install_log_vector(
    asset_id: str,
    payload: LogTargetEnableRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """兼容旧接口：实际安装 OpsPilot Alloy Agent 并发布日志策略。"""
    return install_alloy_agent(asset_id, request, db, current_user)


@router.post("/log-targets/{asset_id}/check")
def check_log_vector(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """检测远端主机上的 Alloy Agent 状态；无 Agent 时兼容旧 Vector 状态。"""
    asset = _get_asset_or_404(db, asset_id)
    agent = _latest_agent_for_asset(db, asset_id)
    if agent:
        record = _agent_status_record(asset, agent)
        log_record = _asset_log_target_record(asset, "alloy-default", record["status"], record["last_error"], enabled=bool(record["enabled"]))
        sync_info = _persist_log_targets(db, _upsert_target(read_log_targets(db)["targets"], log_record), current_user=current_user)
        return {
            "success": record["status"] == "up",
            "message": record["last_error"] or ("Alloy Agent 心跳正常" if record["status"] == "up" else "Alloy Agent 心跳异常"),
            "target": log_record,
            "stdout": "",
            "stderr": "",
            **sync_info,
        }
    ok, msg, out, err, code = _run_asset_ssh(db, asset, _build_remote_vector_check_command(), 45, current_user)
    saved = read_log_targets(db)["targets"]
    profile = "linux-default"
    enabled = False
    for item in saved:
        if isinstance(item, dict) and str(item.get("asset_id") or "") == asset_id:
            profile = str(item.get("profile") or "linux-default")
            enabled = bool(item.get("enabled"))
            break

    if not ok:
        record = _asset_log_target_record(asset, profile, "unreachable", msg, enabled=enabled)
    else:
        status = "running" if code == 0 else "error"
        error = "" if code == 0 else (err or msg or f"Vector 检测失败，退出码 {code}")
        record = _asset_log_target_record(asset, profile, status, error, enabled=enabled and code == 0)
    sync_info = _persist_log_targets(db, _upsert_target(saved, record), current_user=current_user)
    return {"success": ok and code == 0, "message": record["last_error"] or "Vector 运行正常", "target": record, "stdout": out[-2000:], "stderr": err[-2000:], **sync_info}


@router.delete("/log-targets/{asset_id}")
def disable_log_vector(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.assets.hosts", "action.assets.hosts.monitoring")),
):
    """停用远端主机默认可观测性接入；无 Agent 时兼容旧 Vector。"""
    asset = _get_asset_or_404(db, asset_id)
    agent = _latest_agent_for_asset(db, asset_id)
    if agent:
        ok, msg, out, err, code = _run_asset_ssh(db, asset, _build_alloy_disable_command(), 60, current_user)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        if code != 0:
            raise HTTPException(status_code=400, detail=(err or msg or f"停用 Alloy Agent 失败，退出码 {code}")[:500])
        agent.status = "disabled"
        agent.last_error = ""
        agent.updated_at = _collector_now_ts()
        saved_targets = read_monitoring_targets(db)["targets"]
        save_monitoring_targets(db, [item for item in saved_targets if isinstance(item, dict) and item.get("asset_id") != asset_id])
        saved = read_log_targets(db)["targets"]
        next_targets = [item for item in saved if isinstance(item, dict) and item.get("asset_id") != asset_id]
        sync_info = _persist_log_targets(
            db,
            next_targets,
            current_user=current_user,
            disabled_asset_ids=[asset_id],
        )
        return {"success": True, "message": "Alloy Agent 已停用，主机已从可观测性清单移除", "stdout": out[-2000:], "stderr": err[-2000:], **sync_info}
    ok, msg, out, err, code = _run_asset_ssh(db, asset, _build_remote_vector_disable_command(), 45, current_user)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if code != 0:
        raise HTTPException(status_code=400, detail=(err or msg or f"停用 Vector 失败，退出码 {code}")[:500])

    saved = read_log_targets(db)["targets"]
    next_targets = [item for item in saved if isinstance(item, dict) and item.get("asset_id") != asset_id]
    sync_info = _persist_log_targets(
        db,
        next_targets,
        current_user=current_user,
        disabled_asset_ids=[asset_id],
    )
    return {"success": True, "message": "Vector 已停止，主机已从日志采集清单移除", "stdout": out[-2000:], "stderr": err[-2000:], **sync_info}


@router.get("/vmagent-config")
def get_vmagent_config(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    """导出 vmagent scrape 配置片段。"""
    saved_targets = read_monitoring_targets(db)["targets"]
    normalized = _normalize_targets_for_vmagent(db, saved_targets)
    enabled_targets = [item for item in normalized if item.get("enabled")]
    return {
        "config": _render_opspilot_vmagent_block(enabled_targets) + "\n",
        "target_count": len(enabled_targets),
        "config_path": str(VMAGENT_CONFIG_FILE),
    }


def _log_time_to_utc(value: str) -> str:
    """日志时间窗统一转成带时区的 UTC RFC3339。

    VLogs 的 field_values/hits 对无时区时间串按 UTC 解释：服务器在 UTC+8 时
    「近 1 小时」窗口会整体偏移 8 小时落到无日志区间，导致标签下拉空、直方图全平。
    无时区输入按服务器本地时间理解，带 Z/偏移的输入原样换算。
    """
    from datetime import timezone

    text = (value or "").strip()
    if not text:
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.astimezone()
    # Z 后缀而非 +00:00：避免 query 串里裸 `+` 被部分客户端/代理当空格解析
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_log_time_window(
    start: str,
    end: str,
    default_hours: int = 1,
) -> tuple[str, str]:
    """强制日志查询时间窗，拒绝无界全量扫。

    缺省时自动补近 default_hours 小时窗口（end=now, start=now-Nh）。
    前端已传时间参数时行为不变。
    """
    from datetime import datetime, timedelta

    end_val = (end or "").strip()
    start_val = (start or "").strip()
    now = datetime.now()
    if not end_val:
        end_val = now.isoformat(timespec="seconds")
    if not start_val:
        start_val = (now - timedelta(hours=max(1, int(default_hours)))).isoformat(timespec="seconds")
    return _log_time_to_utc(start_val), _log_time_to_utc(end_val)


def _normalize_hits_ts_seconds(ts: Any) -> int:
    """把 VictoriaLogs hits 时间戳归一为 Unix 秒。

    不同版本可能返回秒(1e9 量级)、毫秒(1e12)、微秒(1e15)或纳秒(1e18)，
    按数量级判断后换算，避免前端 `* 1000` 画错时间轴。
    """
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if value < 1e11:          # 秒
        return int(value)
    if value < 1e14:          # 毫秒
        return int(value / 1000)
    if value < 1e17:          # 微秒
        return int(value / 1_000_000)
    return int(value / 1_000_000_000)  # 纳秒


@router.post("/logs/fields")
def list_log_fields(
    payload: LogFieldsRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """枚举日志字段名或字段取值（Grafana 式标签筛选）。

    - field 为空 → 返回可用字段列表（field_names）
    - field 有值 → 返回该字段的取值列表（field_values）
    """
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vlogs_url") or "").strip():
        return {"configured": False, "fields": [], "values": []}

    start_val, end_val = _require_log_time_window(payload.start, payload.end, default_hours=24)
    api_base = _normalize_vlogs_base(str(cfg.get("vlogs_url") or ""))
    field = (payload.field or "").strip()
    query = (payload.query or "*").strip() or "*"
    if field:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", field):
            raise HTTPException(status_code=400, detail="非法字段名")
        params: dict[str, Any] = {
            "field": field,
            "query": query,
            "start": start_val,
            "end": end_val,
            "limit": 200,
        }
        path = "/field_values"
    else:
        params = {"start": start_val, "end": end_val}
        path = "/field_names"

    try:
        resp = requests.get(f"{api_base}{path}", params=params, **_request_kwargs(cfg))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs 连接失败: {str(exc)}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"字段枚举失败（HTTP {resp.status_code}）")

    payload_data = resp.json()
    values: list[str] = []
    if isinstance(payload_data, dict):
        raw = payload_data.get("values") or payload_data.get("fields") or []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    v = item.get("value")
                else:
                    v = item
                text = str(v if v is not None else "").strip()
                if text:
                    values.append(text)
        elif isinstance(raw, dict):
            values = [str(k) for k in raw.keys()]
    elif isinstance(payload_data, list):
        values = [str(x).strip() for x in payload_data if str(x).strip()]

    # 去重并保序，过滤内部流字段噪音
    skip = {"_stream", "_stream_id", "_time", "_msg"}
    seen: set[str] = set()
    cleaned: list[str] = []
    for v in values:
        if v in skip or v in seen:
            continue
        seen.add(v)
        cleaned.append(v)

    if field:
        return {"configured": True, "fields": [field], "values": cleaned[:200]}
    # 字段列表：优先展示常用标签，再补其余
    preferred = ["host", "service", "job", "level", "instance", "asset_id", "app_id", "stream"]
    ordered = [f for f in preferred if f in cleaned] + [f for f in cleaned if f not in preferred]
    return {"configured": True, "fields": ordered[:80], "values": []}


@router.post("/logs/query")
def query_victorialogs(
    payload: LogQueryRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """查询 VictoriaLogs 日志。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vlogs_url") or "").strip():
        return {"configured": False, "message": "VictoriaLogs 未配置", "logs": []}

    query = (payload.query or "*").strip() or "*"
    if len(query) > MAX_LOG_QUERY_LENGTH:
        raise HTTPException(status_code=400, detail=f"日志查询语句不能超过 {MAX_LOG_QUERY_LENGTH} 字符")
    limit = max(1, min(int(payload.limit or 200), MAX_LOG_LIMIT))
    start_val, end_val = _require_log_time_window(payload.start, payload.end, default_hours=1)
    host_ips = [str(item or "").strip() for item in payload.host_ips if str(item or "").strip()]
    host_names = [str(item or "").strip() for item in payload.host_names if str(item or "").strip()]
    services = [str(item or "").strip() for item in payload.services if str(item or "").strip()]
    # 主机范围下推到 LogsQL，避免全量拉取后再内存过滤（也避免 IP 子串误命中）
    scope_clause = build_logs_scope_clause(host_ips, host_names)
    if scope_clause:
        query = scope_clause if query == "*" else f"({query}) AND ({scope_clause})"
    params: dict[str, Any] = {"query": query, "limit": limit, "start": start_val, "end": end_val}
    logs = _filter_logs_by_scope(
        _vlogs_get(cfg, params),
        host_ips,
        host_names,
        services,
    )
    return {"configured": True, "message": "", "logs": logs[:limit]}


@router.post("/logs/histogram")
def query_logs_histogram(
    payload: LogHistogramRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """日志直方图——按时间分桶统计日志数量，用于时间轴可视化。"""
    cfg = read_monitoring_config(db)
    vlogs_url = str(cfg.get("vlogs_url") or "").strip()
    if not vlogs_url:
        return {"configured": False, "buckets": []}

    query = (payload.query or "*").strip() or "*"
    # 如果有 host_ips 过滤，追加到 query
    for ip in (payload.host_ips or []):
        ip = str(ip or "").strip()
        if ip:
            query += f" AND instance:{ip}"

    start_val, end_val = _require_log_time_window(payload.start, payload.end, default_hours=24)
    params: dict[str, Any] = {"query": query, "step": payload.step or "5m", "start": start_val, "end": end_val}

    api_base = _normalize_vlogs_base(vlogs_url)
    try:
        resp = requests.get(f"{api_base}/hits", params=params, **_request_kwargs(cfg))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs 连接失败: {str(exc)}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs hits 查询失败（HTTP {resp.status_code}）")

    data = resp.json()
    # hits API 返回 {"hits": [{"timestamps": [...], "values": [...]}]}
    # 时间戳单位在不同 VictoriaLogs 版本可能是秒/毫秒/纳秒，统一归一为**秒**，
    # 前端用 `new Date(ts * 1000)` 即可对齐。
    hits = data.get("hits") or []
    buckets: list[dict[str, Any]] = []
    if hits:
        first = hits[0]
        timestamps = first.get("timestamps") or []
        values = first.get("values") or []
        for ts, val in zip(timestamps, values):
            buckets.append({"time": _normalize_hits_ts_seconds(ts), "count": val})

    return {"configured": True, "buckets": buckets}


@router.post("/logs/context")
def query_log_context(
    payload: LogContextRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """查询某条日志的上下文（前后 N 行），用于定位问题上下文。"""
    cfg = read_monitoring_config(db)
    vlogs_url = str(cfg.get("vlogs_url") or "").strip()
    if not vlogs_url:
        return {"configured": False, "before": [], "after": []}

    stream_id = (payload.stream_id or "").strip()
    timestamp = (payload.timestamp or "").strip()
    if not stream_id or not timestamp:
        raise HTTPException(status_code=400, detail="stream_id 和 timestamp 是必填项")

    before_count = max(1, min(payload.before, 50))
    after_count = max(1, min(payload.after, 50))

    api_base = _normalize_vlogs_base(vlogs_url)
    # 查询 before：同一 stream，时间 < timestamp
    before_params = {
        "query": f"_stream_id:{stream_id}",
        "end": timestamp,
        "limit": before_count,
    }
    after_params = {
        "query": f"_stream_id:{stream_id}",
        "start": timestamp,
        "limit": after_count + 1,  # +1 因为包含当前行
    }

    before_logs: list[dict[str, Any]] = []
    after_logs: list[dict[str, Any]] = []
    try:
        resp_before = requests.get(f"{api_base}/query", params=before_params, **_request_kwargs(cfg))
        if resp_before.status_code < 400:
            before_logs = _vlogs_get(cfg, before_params)
        resp_after_logs = _vlogs_get(cfg, after_params)
        # 排除与当前 timestamp 完全相同的行
        after_logs = [log for log in resp_after_logs if log.get("time") != timestamp][:after_count]
    except Exception:
        pass

    return {"configured": True, "before": before_logs[-before_count:], "after": after_logs[:after_count]}


@router.post("/logs/suggest-query")
async def suggest_logs_query(
    payload: LogSuggestRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """自然语言 → LogsQL（复用已配置 LLM）。失败返回可读原因，不 500。"""
    from app.api.ai_agent import _read_llm_runtime_config, _run_llm_raw_prompt

    llm_config, _ = _read_llm_runtime_config(db, None)
    if not (llm_config.get("enabled") and llm_config.get("has_api_key")):
        return {
            "ok": False,
            "query": "",
            "message": "未配置可用模型，请先在「AI 能力 → 模型配置」接入 LLM",
        }

    system_prompt = (
        "你是 VictoriaLogs（LogsQL）查询助手。根据用户中文描述生成**一条** LogsQL 查询语句。"
        "只输出查询语句本身，不要解释、不要代码块围栏。"
        "常用语法：`error` 关键字匹配、`level:error` 字段、`AND/OR/NOT`、"
        "`status:>=500`、`*` 全量。若描述模糊，给最常用的安全默认（如 `*error*` 或 `level:error`）。"
    )
    try:
        raw = await _run_llm_raw_prompt(llm_config, system_prompt, payload.text.strip())
        query = (raw or "").strip().strip("`").strip()
        # 去掉可能的 markdown 围栏
        if query.startswith("```"):
            query = query.strip("`").strip()
        if not query:
            return {"ok": False, "query": "", "message": "模型未返回查询语句"}
        return {"ok": True, "query": query[:500], "message": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "query": "", "message": f"生成失败：{str(exc)[:160]}"}


@router.get("/logs/tail")
def stream_logs_tail(
    query: str = "*",
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.logs")),
):
    """Live Tail SSE 流——实时推送新日志行。

    基于 VictoriaLogs /select/logsql/tail API 代理为 SSE 格式。
    """
    import time
    from fastapi.responses import StreamingResponse

    cfg = read_monitoring_config(db)
    vlogs_url = str(cfg.get("vlogs_url") or "").strip()
    if not vlogs_url:
        raise HTTPException(status_code=400, detail="VictoriaLogs 未配置")

    api_base = _normalize_vlogs_base(vlogs_url)
    safe_query = (query or "*").strip() or "*"

    def generate():
        try:
            with requests.get(
                f"{api_base}/tail",
                params={"query": safe_query},
                stream=True,
                timeout=(5, None),
                **{k: v for k, v in _request_kwargs(cfg).items() if k != "timeout"},
            ) as resp:
                if resp.status_code >= 400:
                    yield f"event: error\ndata: {{\"detail\": \"VictoriaLogs tail 失败 HTTP {resp.status_code}\"}}\n\n"
                    return
                deadline = time.time() + 300  # 最多保持 5 分钟
                for line in resp.iter_lines(decode_unicode=True):
                    if time.time() > deadline:
                        yield f"event: done\ndata: {{\"reason\": \"timeout\"}}\n\n"
                        return
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        raw_message = str(item.get("_msg") or item.get("log") or "").strip()
                        log_entry = {
                            "id": item.get("_stream_id") or item.get("id") or "",
                            "time": item.get("_time") or item.get("time") or "",
                            "level": str(item.get("level") or "INFO").upper(),
                            "service": item.get("service") or item.get("job") or "-",
                            "host": item.get("host") or item.get("instance") or "-",
                            "message": raw_message,
                            # stream_id 保留兼容旧前端；上下文抽屉以 id 为准
                            "stream_id": item.get("_stream_id") or "",
                            "fields": item,
                        }
                        yield f"data: {json.dumps(log_entry, ensure_ascii=False)}\n\n"
                    except Exception:
                        yield f"data: {json.dumps({'id': '', 'message': line, 'level': 'INFO', 'time': '', 'service': '-', 'host': '-', 'fields': {}}, ensure_ascii=False)}\n\n"
        except requests.RequestException as exc:
            yield f"event: error\ndata: {{\"detail\": \"{str(exc)[:200]}\"}}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 指标查询（PromQL ad-hoc query against VictoriaMetrics）
#
# 安全边界：
# - 只读：仅透传到 VM 的 /query、/query_range、/labels、/label/<name>/values；
# - 通过 module.monitoring.live 权限保护；
# - PromQL 语句长度限制，避免超长表达式打爆 VM；
# - 时间范围/步长统一走 _clamp_dashboard_range 收口。
# ─────────────────────────────────────────────────────────────────────────────

MAX_PROMQL_LENGTH = 4096


@router.get("/metrics/query")
def metrics_instant_query(
    query: str = Query(..., min_length=1),
    time: str = Query(default=""),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """对 VictoriaMetrics 执行即时查询（instant query）。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置", "data": None}

    expr = (query or "").strip()
    if not expr:
        raise HTTPException(status_code=400, detail="查询语句不能为空")
    if len(expr) > MAX_PROMQL_LENGTH:
        raise HTTPException(status_code=400, detail=f"PromQL 语句不能超过 {MAX_PROMQL_LENGTH} 字符")

    params: dict[str, Any] = {"query": expr}
    if time.strip():
        params["time"] = time.strip()
    payload = _vm_get(cfg, "/query", params)
    return {"configured": True, "data": payload.get("data", {})}


@router.get("/metrics/query_range")
def metrics_range_query(
    query: str = Query(..., min_length=1),
    range_minutes: int = Query(default=60, ge=1, le=43200),
    step_seconds: int = Query(default=60, ge=10, le=86400),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """对 VictoriaMetrics 执行区间查询（range query），用于折线图。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置", "data": None}

    expr = (query or "").strip()
    if not expr:
        raise HTTPException(status_code=400, detail="查询语句不能为空")
    if len(expr) > MAX_PROMQL_LENGTH:
        raise HTTPException(status_code=400, detail=f"PromQL 语句不能超过 {MAX_PROMQL_LENGTH} 字符")

    safe_range, safe_step = _clamp_dashboard_range(range_minutes, step_seconds)
    end = datetime.now()
    start = end - timedelta(minutes=safe_range)
    start_ts = (int(start.timestamp()) // safe_step) * safe_step
    end_ts = (int(end.timestamp()) // safe_step) * safe_step

    payload = _vm_get(
        cfg,
        "/query_range",
        {
            "query": expr,
            "start": start_ts,
            "end": end_ts,
            "step": safe_step,
            "round_digits": 4,
        },
    )
    return {
        "configured": True,
        "data": payload.get("data", {}),
        "range": {"minutes": safe_range, "step_seconds": safe_step},
    }


@router.get("/metrics/labels")
def metrics_label_names(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """返回 VictoriaMetrics 全部 label 名称，供查询页自动补全。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置", "labels": []}
    payload = _vm_get(cfg, "/labels", {})
    return {"configured": True, "labels": payload.get("data", []) or []}


@router.get("/metrics/label-values")
def metrics_label_values(
    label: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.live")),
):
    """返回指定 label 的全部取值（常用于 __name__ 列出所有指标名）。"""
    cfg = read_monitoring_config(db)
    if not str(cfg.get("vm_url") or "").strip():
        return {"configured": False, "message": "VictoriaMetrics 未配置", "values": []}
    safe_label = (label or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", safe_label):
        raise HTTPException(status_code=400, detail="非法的 label 名称")
    payload = _vm_get(cfg, f"/label/{safe_label}/values", {})
    return {"configured": True, "values": payload.get("data", []) or []}
