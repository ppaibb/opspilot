"""告警中心接口：活跃告警、历史告警、规则状态、静默与测试触发。

设计说明：
1. 所有查询走后端代理，前端不直接访问 VictoriaMetrics / Alertmanager；
2. 复用 monitoring.py 中的连接配置读取模式；
3. 静默与测试触发均为受控操作，不暴露内网地址。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any

import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.monitoring import read_monitoring_rules
from app.api.monitoring.services.alert_dispatcher import send_alert_to_channel
from app.core.crypto import decrypt_secret
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.system_config import SystemConfig
from app.models.user import User
from app.models.monitoring_active_alert import MonitoringActiveAlert
from app.api.monitoring.services.dashboard import _fetch_alerts


router = APIRouter()

# 复用 monitoring 模块的配置键
CONFIG_KEY = "victoriametrics_config"
NOTIFICATIONS_CONFIG_KEY = "monitoring_notifications"
DEFAULT_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------

class ActiveAlertItem(BaseModel):
    """单条活跃告警。"""
    fingerprint: str = ""
    name: str
    severity: str
    instance: str
    job: str
    started_at: str
    summary: str
    labels: dict[str, str] = Field(default_factory=dict)
    status: str = "firing"  # firing / pending
    acknowledged: bool = False
    acknowledged_by: str = ""
    acknowledged_at: str = ""
    acknowledged_note: str = ""



class ActiveAlertsResponse(BaseModel):
    """活跃告警响应。"""
    alerts: list[ActiveAlertItem]
    total: int


class HistoryAlertItem(BaseModel):
    """单条历史告警。"""
    id: int | None = None
    name: str
    severity: str
    instance: str
    started_at: str
    resolved_at: str
    status: str
    summary: str
    group: str = ""


class HistoryAlertsResponse(BaseModel):
    """历史告警响应。"""
    alerts: list[HistoryAlertItem]
    total: int



class SilenceRequest(BaseModel):
    """创建静默请求。"""
    alert_name: str
    instance: str = ""
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    comment: str = ""


class SilenceResponse(BaseModel):
    """创建静默响应。"""
    silence_id: str
    ends_at: str
    message: str


class AcknowledgeRequest(BaseModel):
    """认领告警请求：可附处理备注（做了什么/如何跟进），形成处理闭环。"""
    note: str = ""


class AcknowledgeResponse(BaseModel):
    """确认告警响应。"""
    fingerprint: str
    acknowledged: bool
    acknowledged_by: str
    acknowledged_at: str
    note: str = ""


class TestFireRequest(BaseModel):
    """测试触发请求。"""
    channel_id: str


class TestFireResponse(BaseModel):
    """测试触发响应。"""
    sent: bool
    received: bool
    message: str
    event: dict[str, Any] | None = None


# 这些是 API Schema，不是 pytest 测试类。
TestFireRequest.__test__ = False
TestFireResponse.__test__ = False

class AnalyzeAlertRequest(BaseModel):
    """AI 分析告警请求"""
    alert_name: str
    instance: str
    labels: dict[str, str] = Field(default_factory=dict)
    summary: str = ""

class AIAnalysisResponse(BaseModel):
    """AI 分析告警响应"""
    root_cause: str
    impact: str
    suggestions: list[dict[str, str]]
    mode: str = "llm"
    root_cause_id: int = 0
    history_count: int = 0


class AlertSettingsModel(BaseModel):
    """全局告警设置。"""
    default_channel_ids: list[str] = Field(default_factory=list)
    global_routing_mode: str = Field(default="fallback")  # fallback / all
    send_resolved_global: bool = Field(default=True)
    recovery_debounce_seconds: int = Field(default=60, ge=0, le=3600)  # 恢复防抖时间 (秒)
    reminder_interval_minutes: int = Field(default=30, ge=1, le=1440)  # 持续提醒间隔 (分钟)
    max_reminders_cap: int = Field(default=0, ge=0, le=100)  # 最大提醒次数 (0 表示不限制)


class SilenceItem(BaseModel):
    """静默规则信息。"""
    id: str
    alert_name: str
    instance: str
    starts_at: str
    ends_at: str
    created_by: str
    comment: str
    status: str  # active / expired


class SilencesResponse(BaseModel):
    """静默规则列表响应。"""
    silences: list[SilenceItem]
    total: int


class AlertStatsBucket(BaseModel):
    """统计分布桶（级别/分组）。"""
    key: str
    label: str
    count: int


class AlertStatsTopRule(BaseModel):
    """触发频率 Top 规则项。"""
    name: str
    count: int
    severity: str


class AlertStatsResponse(BaseModel):
    """告警统计/趋势响应。"""
    window_hours: int
    active_count: int
    triggered_count: int       # 窗口内新触发(首次出现)的告警数
    resolved_count: int        # 窗口内已恢复的告警数
    by_severity: list[AlertStatsBucket]
    by_group: list[AlertStatsBucket]
    top_rules: list[AlertStatsTopRule]
    hourly_trend: list[int]    # 近 window_hours 每小时触发数(按小时分桶,旧→新)



# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON 配置。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_monitoring_config(db: Session) -> dict[str, Any]:
    """读取 VictoriaMetrics 连接配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == CONFIG_KEY).first()
    if not row:
        return {}
    return _safe_load_json_dict(row.config_value)


def _read_rules_config(db: Session) -> list[dict[str, Any]]:
    """读取本地告警规则配置。"""
    # 规则状态页必须和“告警规则”页面使用同一套默认值和归一化逻辑。
    # 如果这里直接读 SystemConfig，未保存自定义规则的新环境会返回空列表，
    # 导致前端规则状态页看不到 DEFAULT_ALERT_RULES。
    rules, _ = read_monitoring_rules(db)
    return rules


def _read_notifications_config(db: Session) -> list[dict[str, Any]]:
    """读取通知渠道配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == NOTIFICATIONS_CONFIG_KEY).first()
    if not row:
        return []
    payload = _safe_load_json_dict(row.config_value)
    channels = payload.get("channels")
    return channels if isinstance(channels, list) else []


ALERT_SETTINGS_CONFIG_KEY = "monitoring_alert_settings"


def _read_alert_settings(db: Session) -> dict[str, Any]:
    """读取全局告警设置。"""
    defaults = {
        "default_channel_ids": [],
        "global_routing_mode": "fallback",
        "send_resolved_global": True,
        "recovery_debounce_seconds": 60,
        "reminder_interval_minutes": 30,
        "max_reminders_cap": 0
    }
    row = db.query(SystemConfig).filter(SystemConfig.config_key == ALERT_SETTINGS_CONFIG_KEY).first()
    if not row:
        return defaults
    data = _safe_load_json_dict(row.config_value)
    # 合并默认值，防止旧配置缺少字段
    for key, val in defaults.items():
        if key not in data:
            data[key] = val
    return data


SILENCES_CONFIG_KEY = "monitoring_silences"
ACKNOWLEDGEMENTS_CONFIG_KEY = "monitoring_alert_acknowledgements"



def _read_silences_config(db: Session) -> list[dict[str, Any]]:
    """读取本地告警静默配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == SILENCES_CONFIG_KEY).first()
    if not row:
        return []
    payload = _safe_load_json_dict(row.config_value)
    silences = payload.get("silences")
    return silences if isinstance(silences, list) else []


def _save_silences_config(db: Session, silences: list[dict[str, Any]]) -> str:
    """保存本地告警静默配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == SILENCES_CONFIG_KEY).first()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = json.dumps({"silences": silences}, ensure_ascii=False)
    if not row:
        row = SystemConfig(config_key=SILENCES_CONFIG_KEY, config_value=content, updated_at=now_str)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()
    return now_str


def _read_acknowledgements(db: Session) -> dict[str, dict[str, str]]:
    """读取按告警指纹保存的确认记录。"""
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == ACKNOWLEDGEMENTS_CONFIG_KEY
    ).first()
    payload = _safe_load_json_dict(row.config_value if row else None)
    acknowledgements = payload.get("acknowledgements")
    return acknowledgements if isinstance(acknowledgements, dict) else {}


def _save_acknowledgements(
    db: Session,
    acknowledgements: dict[str, dict[str, str]],
) -> None:
    """保存告警确认记录。"""
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == ACKNOWLEDGEMENTS_CONFIG_KEY
    ).first()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = json.dumps(
        {"acknowledgements": acknowledgements},
        ensure_ascii=False,
    )
    if not row:
        row = SystemConfig(
            config_key=ACKNOWLEDGEMENTS_CONFIG_KEY,
            config_value=content,
            updated_at=now_str,
        )
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()



def _request_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
    """根据配置生成 requests 参数，复用 monitoring 模块的认证模式。"""
    headers: dict[str, str] = {}
    kwargs: dict[str, Any] = {
        "timeout": max(3, int(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)),
        "verify": bool(cfg.get("verify_tls", True)),
        "headers": headers,
    }
    auth_type = str(cfg.get("auth_type") or "none").lower()
    secret = decrypt_secret(str(cfg.get("secret") or ""))
    if auth_type == "basic":
        username = str(cfg.get("username") or "")
        kwargs["auth"] = (username, secret)
    elif auth_type == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    return kwargs


def _normalize_vm_base(vm_url: str) -> str:
    """规范化 VictoriaMetrics 查询 API 基地址。"""
    url = (vm_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/api/v1"):
        return url
    if "/select/" in url and not url.endswith("/prometheus"):
        return f"{url}/prometheus/api/v1"
    return f"{url}/api/v1"


# ---------------------------------------------------------------------------
# 活跃告警
# ---------------------------------------------------------------------------

@router.get("/active", response_model=ActiveAlertsResponse)
def get_active_alerts(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """读取活跃及 pending 告警并存入本地数据库，然后查询返回。"""
    cfg = _read_monitoring_config(db)

    # 复杂逻辑中文注释：
    # 1. 活动告警由内置告警引擎(app/services/alert_engine/)在每个评估周期写入
    #    monitoring_active_alert 表；_fetch_alerts(cfg, db) 直接读取该表，不发起外部 HTTP。
    # 2. 调用它仅为统一入口，最终仍以本地 DB 为准。
    _fetch_alerts(cfg, db)

    # 3. 从数据库中读取所有活动的告警列表（包含 pending 和 firing），保证数据以本地 DB 为准。
    active_db_alerts = db.query(MonitoringActiveAlert).order_by(MonitoringActiveAlert.first_seen_at.desc()).all()

    # 4. 读取本地活跃静默规则并过滤/抑制对应告警的级别。
    silences = _read_silences_config(db)
    now = datetime.now()
    active_silence_rules = []
    for s in silences:
        try:
            start_dt = datetime.strptime(s.get("starts_at", ""), "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(s.get("ends_at", ""), "%Y-%m-%d %H:%M:%S")
            if start_dt <= now <= end_dt:
                active_silence_rules.append(s)
        except Exception:
            pass

    alerts: list[dict[str, Any]] = []
    acknowledgements = _read_acknowledgements(db)
    active_fingerprints = {str(item.fingerprint or "") for item in active_db_alerts}
    stale_fingerprints = set(acknowledgements) - active_fingerprints
    if stale_fingerprints:
        acknowledgements = {
            key: value
            for key, value in acknowledgements.items()
            if key in active_fingerprints
        }
        _save_acknowledgements(db, acknowledgements)

    for item in active_db_alerts:
        alert_name = item.name
        instance = item.instance
        severity = item.severity
        state = item.state

        # 校验是否被静默，若静默则设置级别为 suppressed
        is_silenced = False
        for rule in active_silence_rules:
            if rule.get("alert_name") == alert_name:
                rule_inst = rule.get("instance") or ""
                if not rule_inst or rule_inst == instance:
                    is_silenced = True
                    break

        if is_silenced:
            severity = "suppressed"

        labels_dict = {}
        try:
            labels_dict = json.loads(item.labels or "{}")
        except Exception:
            pass

        # 格式化展示时间，若 active_at 解析失败则原样展示
        started_at = item.active_at
        if started_at and started_at != "-":
            try:
                # active_at 可能是 2026-06-16T07:07:00Z 这种 ISO 格式或时间戳
                if "T" in started_at:
                    parsed_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    started_at = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        acknowledgement = acknowledgements.get(str(item.fingerprint or ""), {})
        alerts.append({
            "fingerprint": item.fingerprint,
            "name": alert_name,
            "severity": severity,
            "instance": instance,
            "job": item.job,
            "started_at": started_at,
            "summary": item.summary,
            "status": state,
            "labels": {k: str(v) for k, v in labels_dict.items() if k not in ("alertname", "alertstate")},
            "acknowledged": bool(acknowledgement),
            "acknowledged_by": str(acknowledgement.get("acknowledged_by") or ""),
            "acknowledged_at": str(acknowledgement.get("acknowledged_at") or ""),
            "acknowledged_note": str(acknowledgement.get("note") or ""),
        })

    return {"alerts": alerts, "total": len(alerts)}


@router.post("/{fingerprint}/acknowledge", response_model=AcknowledgeResponse)
def acknowledge_alert(
    fingerprint: str,
    payload: AcknowledgeRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")
    ),
):
    """认领一条仍处于活跃状态的告警，可附处理备注（做了什么/如何跟进）。"""
    alert = db.query(MonitoringActiveAlert).filter(
        MonitoringActiveAlert.fingerprint == fingerprint
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="活跃告警不存在或已恢复")

    acknowledged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    acknowledged_by = str(current_user.name or current_user.username or "未知用户")
    note = str((payload.note if payload else "") or "").strip()[:500]
    acknowledgements = _read_acknowledgements(db)
    acknowledgements[fingerprint] = {
        "acknowledged_by": acknowledged_by,
        "acknowledged_at": acknowledged_at,
        "note": note,
    }
    _save_acknowledgements(db, acknowledgements)
    return {
        "fingerprint": fingerprint,
        "acknowledged": True,
        "acknowledged_by": acknowledged_by,
        "acknowledged_at": acknowledged_at,
        "note": note,
    }


@router.post("/{fingerprint}/unacknowledge")
def unacknowledge_alert(
    fingerprint: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")
    ),
):
    """释放认领：告警回到未认领状态（换人接手或误操作时用）。"""
    acknowledgements = _read_acknowledgements(db)
    if fingerprint not in acknowledgements:
        raise HTTPException(status_code=404, detail="该告警未被认领")
    del acknowledgements[fingerprint]
    _save_acknowledgements(db, acknowledgements)
    return {"fingerprint": fingerprint, "acknowledged": False}



# ---------------------------------------------------------------------------
# 历史告警
# ---------------------------------------------------------------------------

@router.get("/history", response_model=HistoryAlertsResponse)
def get_alert_history(
    start: str = "",
    end: str = "",
    severity: str = "",
    state: str = "",
    group: str = "",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """重建历史告警列表。

    采用夜莺(n9e)模型：告警历史 = 当前仍在活跃(firing/resolving)的告警 +
    已恢复(resolved)归档记录。活跃告警从 monitoring_active_alerts 实时读取
    (resolved_at 为空、status=firing)，已恢复告警从 monitoring_history_alerts
    读取，两者合并后按开始时间倒序返回。这样"有活跃告警时历史也能看到"，
    告警恢复后再以归档记录的形式留痕。
    """
    from app.models.monitoring_history_alert import MonitoringHistoryAlert

    merged: list[dict[str, Any]] = []
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))

    # 顺带清理过期历史，防止表无限增长。
    try:
        purge_old_history_alerts(db)
    except Exception:
        db.rollback()

    # 1. 当前活跃告警（firing / resolving）—— SQL 条件过滤，禁止全表拉入内存。
    try:
        active_q = db.query(MonitoringActiveAlert)
        if severity:
            active_q = active_q.filter(MonitoringActiveAlert.severity == severity)
        if start:
            active_q = active_q.filter(MonitoringActiveAlert.active_at >= start)
        if end:
            active_q = active_q.filter(MonitoringActiveAlert.active_at <= end)
        if group and group != "__none__":
            active_q = active_q.filter(MonitoringActiveAlert.labels.like(f'%"{group}"%') | MonitoringActiveAlert.labels.like(f'%{group}%'))
        active_rows = active_q.order_by(MonitoringActiveAlert.first_seen_at.desc()).limit(limit + offset + 200).all()
    except Exception:
        active_rows = []
    for item in active_rows:
        # 活跃告警统一以 firing 状态进入历史视图（resolving 仍属未恢复）
        item_status = "firing"
        started_at = item.active_at or ""
        if started_at and "T" in started_at:
            try:
                started_at = datetime.fromisoformat(
                    started_at.replace("Z", "+00:00")
                ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        try:
            _act_labels = json.loads(item.labels or "{}")
        except Exception:
            _act_labels = {}
        merged.append({
            "id": None,
            "name": item.name,
            "severity": item.severity,
            "instance": item.instance,
            "started_at": started_at,
            "resolved_at": "",
            "status": item_status,
            "summary": item.summary or item.name,
            "group": _act_labels.get("group") or "",
        })

    # 2. 已恢复归档记录—— SQL 条件过滤 + 时间范围。
    try:
        history_q = db.query(MonitoringHistoryAlert)
        if severity:
            history_q = history_q.filter(MonitoringHistoryAlert.severity == severity)
        if state:
            history_q = history_q.filter(MonitoringHistoryAlert.status == state)
        if group:
            if group == "__none__":
                history_q = history_q.filter((MonitoringHistoryAlert.group == "") | (MonitoringHistoryAlert.group.is_(None)))
            else:
                history_q = history_q.filter(MonitoringHistoryAlert.group == group)
        if start:
            history_q = history_q.filter(MonitoringHistoryAlert.started_at >= start)
        if end:
            history_q = history_q.filter(MonitoringHistoryAlert.started_at <= end)
        history_rows = history_q.order_by(MonitoringHistoryAlert.created_at.desc()).limit(limit + offset + 200).all()
    except Exception:
        history_rows = []
    for item in history_rows:
        merged.append({
            "id": item.id,
            "name": item.name,
            "severity": item.severity,
            "instance": item.instance,
            "started_at": item.started_at,
            "resolved_at": item.resolved_at,
            "status": item.status,
            "summary": item.summary,
            "group": getattr(item, "group", "") or "",
        })

    # 3. 统一过滤（severity / 时间范围 / 状态）——仅做内存侧无法下推的收尾。
    def _keep(a: dict[str, Any]) -> bool:
        if severity and a["severity"] != severity:
            return False
        if state and a["status"] != state:
            return False
        if group:
            a_group = a.get("group") or ""
            if group == "__none__":
                if a_group:
                    return False
            elif a_group != group:
                return False
        if start and (a["started_at"] or "") < start:
            return False
        if end and (a["started_at"] or "") > end:
            return False
        return True

    filtered = [a for a in merged if _keep(a)]

    # 4. 按开始时间倒序 + 分页。
    filtered.sort(key=lambda a: a["started_at"] or "", reverse=True)
    total = len(filtered)
    page = filtered[offset:offset + limit]

    return {"alerts": page, "total": total}





# ---------------------------------------------------------------------------
# 静默
# ---------------------------------------------------------------------------

@router.post("/silence", response_model=SilenceResponse)
def create_silence(
    payload: SilenceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")
    ),
):
    """创建本地告警静默规则。"""
    now = datetime.now()
    ends_at = now + timedelta(minutes=payload.duration_minutes)

    silence_id = secrets.token_hex(8)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    ends_at_str = ends_at.strftime("%Y-%m-%d %H:%M:%S")

    # 读取并过滤历史已失效的静默，防止数据不断累积超出字段容量限制
    silences = _read_silences_config(db)
    active_silences = []
    for s in silences:
        try:
            s_end = datetime.strptime(s.get("ends_at", ""), "%Y-%m-%d %H:%M:%S")
            if s_end > now:
                active_silences.append(s)
        except Exception:
            pass

    new_silence = {
        "id": silence_id,
        "alert_name": payload.alert_name,
        "instance": payload.instance,
        "starts_at": now_str,
        "ends_at": ends_at_str,
        "created_by": f"opspilot-{current_user.username}",
        "comment": payload.comment or f"OpsPilot 静默：{payload.alert_name}",
    }
    active_silences.append(new_silence)
    _save_silences_config(db, active_silences)

    return {
        "silence_id": silence_id,
        "ends_at": ends_at_str,
        "message": f"已静默 {payload.alert_name}，{payload.duration_minutes} 分钟后自动解除",
    }



# ---------------------------------------------------------------------------
# 测试触发
# ---------------------------------------------------------------------------

@router.post("/test-fire", response_model=TestFireResponse)
def test_fire_webhook(
    payload: TestFireRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """向指定通知渠道直接发送测试告警，验证连通性。"""
    # 查找渠道配置
    channels = _read_notifications_config(db)
    target_channel = None
    for ch in channels:
        if str(ch.get("id") or "") == payload.channel_id:
            target_channel = ch
            break

    if not target_channel:
        raise HTTPException(status_code=404, detail="通知渠道不存在")

    webhook_url = str(target_channel.get("webhook_url") or "").strip()
    if not webhook_url:
        raise HTTPException(status_code=400, detail="该渠道未配置 Webhook URL")

    # 构造测试告警数据
    test_token = secrets.token_hex(4)
    test_alert = {
        "name": "OpsPilotTestAlert",
        "severity": "info",
        "instance": "opspilot-test:8001",
        "summary": f"OpsPilot 通知渠道连通性测试 token-{test_token}",
        "description": "这是一条由 OpsPilot 告警中心直接触发的通知渠道连通性测试消息。看到此内容说明该 Webhook 渠道配置正确且网络可达。",
        "startsAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # 调用分发器进行直接发送，适应不同渠道的渲染规范
    sent, send_message = send_alert_to_channel(target_channel, test_alert)

    received = False
    event_data = None
    if sent:
        from app.models.monitoring_webhook_event import MonitoringWebhookEvent
        event_row = (
            db.query(MonitoringWebhookEvent)
            .filter(MonitoringWebhookEvent.payload_preview.like(f"%token-{test_token}%"))
            .first()
        )
        if event_row:
            received = True
            event_data = {
                "event_id": event_row.event_id,
                "received_at": event_row.received_at,
                "summary": event_row.summary,
                "alert_count": int(event_row.alert_count or 0),
            }

    return {
        "sent": sent,
        "received": received,
        "message": send_message,
        "event": event_data,
    }


# 该函数是 FastAPI 路由处理器，保留历史名称供现有测试直接调用。
# pytest 会收集测试模块中所有 test_ 开头的可调用对象，因此显式排除。
test_fire_webhook.__test__ = False

# ---------------------------------------------------------------------------
# AI 智能分析 (MVP)
# ---------------------------------------------------------------------------

@router.post("/analyze", response_model=AIAnalysisResponse)
def analyze_alert(
    payload: AnalyzeAlertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """AI 根因分析（真 LLM）：注入同类告警的历史人工根因，分析即落 pending 记录。

    闭环设计（对标塔斯汀模式）：
    1. 分析时检索根因库中同 alert_name 的人工根因作为 few-shot 经验；
    2. 结果立即落 alert_root_causes（ai_rating=pending），等待人工补真实根因与评价；
    3. LLM 未配置/失败时返回结构化降级提示（mode=fallback），不返回编造内容。
    """
    from app.services.alert_root_cause import run_ai_analysis, save_analysis_draft

    alert = {
        "alert_name": payload.alert_name,
        "instance": payload.instance,
        "severity": getattr(payload, "severity", "") or "",
        "labels": getattr(payload, "labels", None) or {},
        "description": getattr(payload, "description", "") or "",
    }
    diagnosis = run_ai_analysis(db, alert)
    record = save_analysis_draft(db, alert, diagnosis, current_user.name or current_user.username)
    return {
        "root_cause": diagnosis.get("root_cause") or "",
        "impact": diagnosis.get("impact") or "",
        "suggestions": diagnosis.get("suggestions") or [],
        "mode": diagnosis.get("mode") or "llm",
        "root_cause_id": record.id,
        "history_count": diagnosis.get("history_count") or 0,
    }


# ---------------------------------------------------------------------------
# 全局告警设置与静默管理
# ---------------------------------------------------------------------------

@router.get("/settings", response_model=AlertSettingsModel)
def get_alert_settings(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """获取全局告警设置。"""
    return _read_alert_settings(db)


@router.put("/settings", response_model=AlertSettingsModel)
def save_alert_settings(
    payload: AlertSettingsModel,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """更新全局告警设置。"""
    from app.api.assets import now_ts
    row = db.query(SystemConfig).filter(SystemConfig.config_key == ALERT_SETTINGS_CONFIG_KEY).first()
    content = json.dumps(payload.model_dump(), ensure_ascii=False)
    now_str = now_ts()
    if not row:
        row = SystemConfig(config_key=ALERT_SETTINGS_CONFIG_KEY, config_value=content, updated_at=now_str)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = now_str
    db.commit()
    return payload


@router.get("/silence", response_model=SilencesResponse)
def get_silence_rules(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """获取本地所有静默规则列表，包含状态计算。"""
    silences = _read_silences_config(db)
    now = datetime.now()
    items = []
    for s in silences:
        status = "expired"
        try:
            end_dt = datetime.strptime(s.get("ends_at", ""), "%Y-%m-%d %H:%M:%S")
            if end_dt > now:
                status = "active"
        except Exception:
            pass
        items.append({
            "id": s.get("id") or "",
            "alert_name": s.get("alert_name") or "",
            "instance": s.get("instance") or "",
            "starts_at": s.get("starts_at") or "",
            "ends_at": s.get("ends_at") or "",
            "created_by": s.get("created_by") or "",
            "comment": s.get("comment") or "",
            "status": status
        })
    # 按开始时间倒序排列
    items.sort(key=lambda x: x["starts_at"], reverse=True)
    return {"silences": items, "total": len(items)}


@router.delete("/silence/{silence_id}")
def delete_silence_rule(
    silence_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """解除静默：删除对应的静默配置条目。"""
    silences = _read_silences_config(db)
    filtered = [s for s in silences if s.get("id") != silence_id]
    if len(filtered) == len(silences):
        raise HTTPException(status_code=404, detail="静默规则不存在")
    _save_silences_config(db, filtered)
    return {"success": True, "message": "已解除静默规则"}


class SilenceUpdateRequest(BaseModel):
    """修改静默规则请求：延长时长(分钟,从现在算起)或修改备注。"""
    extend_minutes: int | None = Field(default=None, ge=1, le=43200)
    comment: str | None = None


@router.patch("/silence/{silence_id}")
def update_silence_rule(
    silence_id: str,
    payload: SilenceUpdateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """修改静默规则:延长过期时间(从当前时间 + extend_minutes)或更新备注。"""
    silences = _read_silences_config(db)
    found = None
    for s in silences:
        if s.get("id") == silence_id:
            found = s
            break
    if found is None:
        raise HTTPException(status_code=404, detail="静默规则不存在")
    if payload.extend_minutes is not None:
        new_end = datetime.now() + timedelta(minutes=payload.extend_minutes)
        found["ends_at"] = new_end.strftime("%Y-%m-%d %H:%M:%S")
    if payload.comment is not None:
        found["comment"] = payload.comment
    _save_silences_config(db, silences)
    return {"success": True, "message": "已更新静默规则", "ends_at": found.get("ends_at")}



class BatchDeleteHistoryRequest(BaseModel):
    """批量删除历史告警请求。"""
    ids: list[int]


@router.delete("/history/batch")
def batch_delete_history(
    payload: BatchDeleteHistoryRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules", "action.monitoring.rules.manage")),
):
    """批量删除历史告警记录。"""
    from app.models.monitoring_history_alert import MonitoringHistoryAlert
    from app.models.monitoring_webhook_event import MonitoringWebhookEvent
    try:
        db_ids = [x for x in payload.ids if x > 0]
        event_ids = [(-x) // 10000 for x in payload.ids if x < 0]
        
        if db_ids:
            db.query(MonitoringHistoryAlert).filter(MonitoringHistoryAlert.id.in_(db_ids)).delete(synchronize_session=False)
        if event_ids:
            db.query(MonitoringWebhookEvent).filter(MonitoringWebhookEvent.id.in_(event_ids)).delete(synchronize_session=False)
            
        db.commit()
        return {"success": True, "message": "批量删除历史告警成功"}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"批量删除失败: {str(exc)}")


_SEVERITY_LABELS = {"critical": "严重", "warning": "警告", "info": "信息"}

# 历史告警保留天数，超期自动清理（防全表膨胀）。
HISTORY_RETENTION_DAYS = 30


def purge_old_history_alerts(db: Session, retention_days: int = HISTORY_RETENTION_DAYS) -> int:
    """按保留天数清理过期历史告警，返回删除行数。

    由历史查询入口顺带触发，避免额外调度器依赖。
    """
    from app.models.monitoring_history_alert import MonitoringHistoryAlert

    cutoff = datetime.utcnow() - timedelta(days=max(1, int(retention_days)))
    deleted = (
        db.query(MonitoringHistoryAlert)
        .filter(MonitoringHistoryAlert.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


@router.get("/stats", response_model=AlertStatsResponse)
def get_alert_stats(
    window_hours: int = 24,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """告警统计/趋势：窗口内触发/恢复数、按级别与分组分布、Top 触发规则、按小时趋势。

    时间基准统一用 UTC DateTime 列（active.first_seen_at / history.created_at），
    避免本地时间字符串与 utcnow 比较产生的 8 小时偏移。
    """
    from app.models.monitoring_active_alert import MonitoringActiveAlert
    from app.models.monitoring_history_alert import MonitoringHistoryAlert

    window_hours = max(1, min(int(window_hours or 24), 720))
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=window_hours)

    # SQL 条件 + 时间范围，禁止全表拉入内存。
    active_count = db.query(MonitoringActiveAlert).count()
    triggered_active_rows = (
        db.query(MonitoringActiveAlert)
        .filter(MonitoringActiveAlert.first_seen_at >= cutoff)
        .all()
    )
    history_rows = (
        db.query(MonitoringHistoryAlert)
        .filter(MonitoringHistoryAlert.created_at >= cutoff)
        .all()
    )
    triggered_active = triggered_active_rows
    triggered_count = len(triggered_active) + len(history_rows)
    resolved_count = len(history_rows)

    # 级别 / 分组分布（合并活跃 + 历史）
    sev_counter: dict[str, int] = {}
    grp_counter: dict[str, int] = {}
    rule_counter: dict[str, int] = {}
    rule_sev: dict[str, str] = {}

    def _group_of_active(row) -> str:
        try:
            lbl = json.loads(row.labels) if row.labels else {}
        except Exception:
            lbl = {}
        return (lbl.get("group") or "").strip()

    for r in triggered_active_rows:
        sev = (r.severity or "warning").strip() or "warning"
        sev_counter[sev] = sev_counter.get(sev, 0) + 1
        grp = _group_of_active(r) or "未分组"
        grp_counter[grp] = grp_counter.get(grp, 0) + 1
        nm = r.name or "未命名"
        rule_counter[nm] = rule_counter.get(nm, 0) + 1
        rule_sev.setdefault(nm, sev)

    for r in history_rows:
        sev = (r.severity or "warning").strip() or "warning"
        sev_counter[sev] = sev_counter.get(sev, 0) + 1
        grp = (r.group or "").strip() or "未分组"
        grp_counter[grp] = grp_counter.get(grp, 0) + 1
        nm = r.name or "未命名"
        rule_counter[nm] = rule_counter.get(nm, 0) + 1
        rule_sev.setdefault(nm, sev)

    sev_order = {"critical": 0, "warning": 1, "info": 2}
    by_severity = [
        AlertStatsBucket(key=k, label=_SEVERITY_LABELS.get(k, k), count=v)
        for k, v in sorted(sev_counter.items(), key=lambda kv: sev_order.get(kv[0], 9))
    ]
    by_group = [
        AlertStatsBucket(key=k, label=k, count=v)
        for k, v in sorted(grp_counter.items(), key=lambda kv: kv[1], reverse=True)
    ][:8]
    top_rules = [
        AlertStatsTopRule(name=k, count=v, severity=rule_sev.get(k, "warning"))
        for k, v in sorted(rule_counter.items(), key=lambda kv: kv[1], reverse=True)
    ][:5]

    # 按小时趋势（旧→新），桶数 = window_hours
    trend = [0] * window_hours
    for ts in [r.first_seen_at for r in triggered_active] + [r.created_at for r in history_rows]:
        if not ts:
            continue
        idx = int((ts - cutoff).total_seconds() // 3600)
        if 0 <= idx < window_hours:
            trend[idx] += 1

    return {
        "window_hours": window_hours,
        "active_count": active_count,
        "triggered_count": triggered_count,
        "resolved_count": resolved_count,
        "by_severity": by_severity,
        "by_group": by_group,
        "top_rules": top_rules,
        "hourly_trend": trend,
    }


# ============== 通知发送记录 / 审计 ==============

class NotificationLogItem(BaseModel):
    """单条通知发送记录。"""
    id: int
    alert_name: str
    instance: str
    severity: str
    event_type: str  # firing / resolved
    channel_name: str
    channel_type: str
    target: str
    success: bool
    message: str
    created_at: str


class NotificationLogsResponse(BaseModel):
    """通知发送记录列表响应。"""
    logs: list[NotificationLogItem]
    total: int
    page: int
    page_size: int


@router.get("/notifications/logs", response_model=NotificationLogsResponse)
def list_notification_logs(
    page: int = 1,
    page_size: int = 20,
    success: str = "all",        # all / success / failed
    channel_name: str = "",
    q: str = "",
    fingerprint: str = "",
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.monitoring.rules")),
):
    """分页查询通知发送记录(审计):谁、推到哪个渠道、成功与否。

    fingerprint 非空时按告警指纹过滤，供告警详情时间线（触发→通知→确认→恢复）使用。
    """
    from app.models.monitoring_notification_log import MonitoringNotificationLog

    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    query = db.query(MonitoringNotificationLog)
    if fingerprint.strip():
        query = query.filter(MonitoringNotificationLog.fingerprint == fingerprint.strip())
    if success == "success":
        query = query.filter(MonitoringNotificationLog.success.is_(True))
    elif success == "failed":
        query = query.filter(MonitoringNotificationLog.success.is_(False))
    if channel_name.strip():
        query = query.filter(MonitoringNotificationLog.channel_name == channel_name.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(MonitoringNotificationLog.alert_name.ilike(like))

    total = query.count()
    rows = (
        query.order_by(MonitoringNotificationLog.created_at.desc(), MonitoringNotificationLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    logs = [
        NotificationLogItem(
            id=r.id,
            alert_name=r.alert_name or "",
            instance=r.instance or "",
            severity=r.severity or "warning",
            event_type=r.event_type or "firing",
            channel_name=r.channel_name or "",
            channel_type=r.channel_type or "",
            target=r.target or "",
            success=bool(r.success),
            message=r.message or "",
            created_at=r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        )
        for r in rows
    ]
    return NotificationLogsResponse(logs=logs, total=total, page=page, page_size=page_size)





# ---------------------------------------------------------------------------
# 根因反馈闭环（塔斯汀模式：上下文 + AI 诊断 + 人工根因/评价 三元组）
# ---------------------------------------------------------------------------

class RootCauseFeedbackRequest(BaseModel):
    """人工反馈：补真实根因/处理方式 + 评价 AI。"""

    human_root_cause: str = Field(min_length=1, max_length=5000)
    human_resolution: str = Field(default="", max_length=5000)
    ai_rating: str = Field(pattern="^(accurate|partial|inaccurate)$")
    ai_diagnosis: dict | None = None  # 未走 AI 分析直接人工记录时补充


@router.get("/root-causes")
def list_root_causes(
    alert_name: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """根因库检索：按告警名查历史处置记录（AI 诊断 + 人工根因 + 评价）。"""
    from app.models.root_cause import AlertRootCause

    query = db.query(AlertRootCause)
    if alert_name:
        query = query.filter(AlertRootCause.alert_name.contains(alert_name))
    rows = query.order_by(AlertRootCause.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "fingerprint": r.fingerprint or "",
            "alert_name": r.alert_name or "",
            "service": r.service or "",
            "instance": r.instance or "",
            "severity": r.severity or "",
            "ai_diagnosis": r.ai_diagnosis or {},
            "ai_model": r.ai_model or "",
            "human_root_cause": r.human_root_cause or "",
            "human_resolution": r.human_resolution or "",
            "ai_rating": r.ai_rating or "pending",
            "username": r.username or "",
            "created_at": r.created_at or "",
        }
        for r in rows
    ]


@router.post("/root-causes/{record_id}/feedback")
def submit_root_cause_feedback(
    record_id: int,
    payload: RootCauseFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """补齐/更新一条根因记录的人工侧（真实根因 + 处理方式 + AI 评价）。"""
    from app.models.root_cause import AlertRootCause

    record = db.query(AlertRootCause).filter(AlertRootCause.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="根因记录不存在")
    record.human_root_cause = payload.human_root_cause.strip()
    record.human_resolution = payload.human_resolution.strip()
    record.ai_rating = payload.ai_rating
    db.commit()
    return {"message": "根因反馈已沉淀，AI 分析将参考该经验", "id": record.id}


@router.get("/root-causes/stats")
def root_cause_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI 准确率统计（按告警名），支撑场景化评估与提示词优化。"""
    from app.services.alert_root_cause import accuracy_stats

    return accuracy_stats(db)
