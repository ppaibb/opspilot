"""监控 Webhook 接收与查看接口。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import require_permissions
from app.db.database import get_db
from app.models.monitoring_webhook_event import MonitoringWebhookEvent
from app.models.system_config import SystemConfig

router = APIRouter()

WEBHOOK_RECEIVER_CONFIG_KEY = "monitoring_webhook_receiver"
MAX_EVENT_COUNT = 50
MAX_PREVIEW_CHARS = 4000


class WebhookReceiverInfoResponse(BaseModel):
    """Webhook 接收器基础信息。"""

    token: str
    path: str
    receiver_url: str
    updated_at: str


class WebhookReceiverEventItem(BaseModel):
    """Webhook 接收器单条事件。"""

    event_id: str
    received_at: str
    remote_addr: str
    source: str
    status: str
    alert_count: int
    summary: str
    payload_preview: str


class WebhookReceiverEventsResponse(BaseModel):
    """Webhook 接收器事件列表。"""

    events: list[WebhookReceiverEventItem]
    total: int


def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON，避免脏数据导致接口报错。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_receiver_row(db: Session) -> SystemConfig | None:
    """读取内置 Webhook 接收器配置行。"""
    return db.query(SystemConfig).filter(SystemConfig.config_key == WEBHOOK_RECEIVER_CONFIG_KEY).first()


def _ensure_receiver_token(db: Session) -> tuple[str, str]:
    """确保内置 Webhook 接收器拥有稳定 token。

    该 token 用于避免把公开接收端完全暴露成无门槛入口。
    """
    row = _read_receiver_row(db)
    if row:
        payload = _safe_load_json_dict(row.config_value)
        token = str(payload.get("token") or "").strip()
        if token:
            return token, str(row.updated_at or "")

    token = secrets.token_urlsafe(24)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = json.dumps({"token": token}, ensure_ascii=False)
    if not row:
        row = SystemConfig(config_key=WEBHOOK_RECEIVER_CONFIG_KEY, config_value=content, updated_at=updated_at)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = updated_at
    db.commit()
    return token, updated_at


def _build_receiver_summary(payload: Any) -> tuple[int, str]:
    """从 Alertmanager/Webhook 负载中提取简单摘要。"""
    if isinstance(payload, dict):
        alerts = payload.get("alerts")
        if isinstance(alerts, list):
            alert_count = len(alerts)
            if alerts:
                first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
                labels = first_alert.get("labels") if isinstance(first_alert, dict) else {}
                annotations = first_alert.get("annotations") if isinstance(first_alert, dict) else {}
                if not isinstance(labels, dict):
                    labels = {}
                if not isinstance(annotations, dict):
                    annotations = {}
                summary = str(
                    annotations.get("summary")
                    or labels.get("alertname")
                    or payload.get("status")
                    or "收到告警推送"
                ).strip()
                if summary:
                    return alert_count, summary[:255]
            return alert_count, f"收到 {alert_count} 条告警"
        status = str(payload.get("status") or "").strip()
        if status:
            return 0, f"收到状态回调：{status[:200]}"
    return 0, "收到 Webhook 推送"


def _prune_old_events(db: Session) -> None:
    """仅保留最近 N 条事件，避免联调数据无限增长。"""
    rows = (
        db.query(MonitoringWebhookEvent)
        .order_by(MonitoringWebhookEvent.id.desc())
        .offset(MAX_EVENT_COUNT)
        .all()
    )
    for row in rows:
        db.delete(row)


@router.get("/receiver", response_model=WebhookReceiverInfoResponse)
def get_webhook_receiver_info(
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    """返回内置 Webhook 接收器地址。"""
    token, updated_at = _ensure_receiver_token(db)
    path = f"/api/monitoring/webhooks/alertmanager/{token}"
    base_url = str(request.base_url).rstrip("/")
    return {
        "token": token,
        "path": path,
        "receiver_url": f"{base_url}{path}",
        "updated_at": updated_at,
    }


@router.get("/receiver/events", response_model=WebhookReceiverEventsResponse)
def get_webhook_receiver_events(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system")),
):
    """读取最近收到的 Webhook 事件。"""
    rows = (
        db.query(MonitoringWebhookEvent)
        .order_by(MonitoringWebhookEvent.id.desc())
        .limit(MAX_EVENT_COUNT)
        .all()
    )
    events = [
        {
            "event_id": row.event_id,
            "received_at": row.received_at,
            "remote_addr": row.remote_addr,
            "source": row.source,
            "status": row.status,
            "alert_count": int(row.alert_count or 0),
            "summary": row.summary,
            "payload_preview": row.payload_preview,
        }
        for row in rows
    ]
    return {"events": events, "total": len(events)}


@router.delete("/receiver/events")
def clear_webhook_receiver_events(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("module.settings.system", "action.settings.system.manage")),
):
    """清空联调阶段的 Webhook 接收记录。"""
    db.query(MonitoringWebhookEvent).delete()
    db.commit()
    return {"success": True, "message": "Webhook 接收记录已清空"}


@router.post("/alertmanager/{token}")
async def receive_alertmanager_webhook(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """接收外部 Webhook 推送（兼容旧链路与通知渠道测试）。

    安全策略：
    1. 仅校验 token 与截断保存原始负载；
    2. 不执行任何模板渲染、脚本或命令；
    3. 返回固定 JSON，便于上游快速判断“是否投递成功”。
    """
    expected_token, _ = _ensure_receiver_token(db)
    if token != expected_token:
        raise HTTPException(status_code=404, detail="Webhook 接收地址不存在")

    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    payload_preview = raw_text[:MAX_PREVIEW_CHARS]
    content_type = str(request.headers.get("content-type") or "").lower()

    parsed_payload: Any
    if "json" in content_type:
        try:
            parsed_payload = json.loads(raw_text or "{}")
        except Exception:
            parsed_payload = {"raw": payload_preview}
    else:
        parsed_payload = {"raw": payload_preview}

    alert_count, summary = _build_receiver_summary(parsed_payload)
    event = MonitoringWebhookEvent(
        event_id=secrets.token_hex(12),
        received_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        remote_addr=request.client.host if request.client else "-",
        source="alertmanager",
        status="received",
        alert_count=alert_count,
        summary=summary,
        payload_preview=payload_preview,
    )
    db.add(event)
    _prune_old_events(db)
    db.commit()

    return {
        "success": True,
        "message": "Webhook 已接收",
        "event_id": event.event_id,
        "alert_count": alert_count,
    }
