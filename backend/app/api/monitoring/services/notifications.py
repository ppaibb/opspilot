"""通知渠道归一化与联动同步。

【设计说明】
 通知渠道目前只支持通用 Webhook，但数据结构已预留 type 字段。
 归一化逻辑独立出来的原因：
1. 渠道校验（URL 格式、重复 ID）与规则渲染无关，职责分离；
2. 测试告警构造（_build_test_alertmanager_payload）需要模拟完整的 Alertmanager webhook 格式，
   代码量不小，不宜放在路由层；
3. 未来接入企业微信、钉钉时，只需在本模块增加 type 分支，不影响 rules.py。
"""
from __future__ import annotations

import json
import re
from typing import Any

from datetime import datetime

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.system_config import SystemConfig
from app.api.monitoring.constants import (
    ALERT_SEVERITIES,
    AUTH_TYPES,
    CONFIG_KEY,
    DEFAULT_TIMEOUT_SECONDS,
    NOTIFICATIONS_CONFIG_KEY,
    NOTIFICATION_TYPES,
)
from app.api.monitoring.schemas import NotificationChannelItem
from app.api.monitoring.services.config_store import (
    _normalize_config_item_id,
    _read_system_config_row,
    _safe_load_json_dict,
    _save_system_config_json,
)

def _normalize_notification_channels(raw_channels: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
    """
    规整通知渠道配置。

    安全约束：
    - 只允许 webhook 类型；
    - URL 仅允许 http/https；
    - 限制单字段长度，避免系统配置表被异常内容撑满。
    """
    if not isinstance(raw_channels, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_channels):
        if not isinstance(item, dict):
            continue
        channel_id = _normalize_config_item_id(str(item.get("id") or ""), "channel", index)
        if channel_id in seen_ids:
            channel_id = _normalize_config_item_id(f"{channel_id}-{index + 1}", "channel", index)
        seen_ids.add(channel_id)

        channel_type = str(item.get("type") or "webhook").strip().lower()
        if channel_type not in NOTIFICATION_TYPES:
            channel_type = "webhook"
        webhook_url = str(item.get("webhook_url") or "").strip()
        if webhook_url and not webhook_url.startswith(("http://", "https://")):
            continue

        normalized.append(
            {
                "id": channel_id,
                "name": (str(item.get("name") or "").strip() or f"Webhook {index + 1}")[:80],
                "type": channel_type,
                "webhook_url": webhook_url[:500],
                "enabled": bool(item.get("enabled", True)),
                "send_resolved": bool(item.get("send_resolved", True)),
                "template_id": str(item.get("template_id") or "").strip()[:80],
            }
        )
    return normalized


def read_monitoring_notifications(db: Session) -> tuple[list[dict[str, Any]], str]:
    """读取通知渠道配置。

    未配置时返回空列表（不再回退内置企微测试渠道）；调用方应把「无渠道」
    作为明确状态提示用户去「系统设置 → 通知渠道」配置，而不是静默发到内置 key。
    """
    row = _read_system_config_row(db, NOTIFICATIONS_CONFIG_KEY)
    if not row:
        return [], ""
    payload = _safe_load_json_dict(row.config_value)
    channels = _normalize_notification_channels(payload.get("channels"))
    return channels, str(row.updated_at or "")



def _persist_notifications_and_sync_alerting(db: Session, channels: list[dict[str, Any]]) -> dict[str, Any]:
    """保存通知渠道配置（直连架构下不再联动 Alertmanager）。"""
    try:
        updated_at = _save_system_config_json(db, NOTIFICATIONS_CONFIG_KEY, {"channels": channels})
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"同步通知渠道配置失败: {str(exc)}")
    return {
        "updated_at": updated_at,
        "rule_file": {"path": "", "changed": False},
        "notification_file": {"path": "", "changed": False},
        "runtime_reload": {
            "alertmanager": {"attempted": False, "ok": True, "message": "直连架构，无需 Alertmanager"},
        },
    }


def _build_test_alertmanager_payload(channel: dict[str, Any]) -> list[dict[str, Any]]:
    """构造用于测试 Webhook 渠道的 Alertmanager 样例负载。"""
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    channel_name = str(channel.get("name") or "Webhook")
    import os
    console_base = (os.environ.get("CONSOLE_URL") or "http://127.0.0.1:5173").strip().rstrip("/")
    console_url = f"{console_base}/monitoring/alerts"
    return [
        {
            "version": "4",
            "groupKey": "{}:{alertname=\"OpsPilotTestAlert\"}",
            "status": "firing",
            "receiver": channel_name,
            "groupLabels": {"alertname": "OpsPilotTestAlert"},
            "commonLabels": {
                "alertname": "OpsPilotTestAlert",
                "severity": "info",
                "opspilot_rule_id": "rule-test",
                "instance": "opspilot-local",
            },
            "commonAnnotations": {
                "summary": "OpsPilot 通知渠道测试",
                "description": f"这是由 OpsPilot 触发的测试告警，请确认 {channel_name} 已收到通知",
            },
            "externalURL": console_url,
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "OpsPilotTestAlert",
                        "severity": "info",
                        "opspilot_rule_id": "rule-test",
                        "instance": "opspilot-local",
                    },
                    "annotations": {
                        "summary": "OpsPilot 通知渠道测试",
                        "description": f"这是由 OpsPilot 在 {now_iso} 触发的测试告警",
                    },
                    "startsAt": now_iso,
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": console_url,
                    "fingerprint": f"test-{channel.get('id') or 'channel'}",
                }
            ],
        }
    ]

