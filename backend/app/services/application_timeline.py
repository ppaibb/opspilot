"""应用时间线只读聚合服务。"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.json_fields import normalize_json_list
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationConfigRevision,
    ApplicationRelease,
)
from app.services.application_time import format_app_datetime


def load_json_list(raw_value: str | None) -> list[str]:
    """安全解析 JSON 数组，兼容 ORM 已经返回 list 的新字段。"""
    return normalize_json_list(raw_value)


def normalize_alert_time(raw_value: str) -> str:
    """把 Alertmanager 返回的 ISO 时间裁剪到秒精度。

    时间线只做前端展示，不做再查询，因此去掉毫秒和时区即可。
    """
    value = str(raw_value or "").strip()
    if not value or value == "-":
        return ""
    # 典型格式：2026-05-09T07:23:11.245Z 或 2026-05-09T07:23:11+00:00
    trimmed = value.replace("T", " ")
    if "." in trimmed:
        trimmed = trimmed.split(".", 1)[0]
    for suffix in ("Z", "+00:00"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
    return trimmed.strip()[:19]


def release_timeline_event(row: ApplicationRelease) -> dict[str, Any]:
    """把发布记录转成时间线事件。"""
    status = str(row.status or "pending").lower()
    severity_map = {
        "success": "success",
        "failed": "error",
        "canceled": "warning",
        "running": "info",
        "pending": "info",
    }
    status_label_map = {
        "success": "成功",
        "failed": "失败",
        "canceled": "已取消",
        "running": "执行中",
        "pending": "等待中",
    }
    release_type_map = {
        "external": "外部发布",
        "ansible": "Ansible 发布",
        "supervisor": "Supervisor 发布",
        "systemd": "systemd 发布",
        "docker": "Docker 发布",
        "docker_compose": "Compose 发布",
        "kubernetes": "Kubernetes 发布",
        "helm": "Helm 发布",
        "rollback": "回滚",
    }
    release_type = str(row.release_type or "external")
    target_names = load_json_list(row.target_names)
    summary_parts: list[str] = []
    if target_names:
        summary_parts.append(f"目标：{', '.join(target_names[:3])}{'…' if len(target_names) > 3 else ''}")
    if row.summary:
        summary_parts.append(str(row.summary).strip()[:200])
    elif row.template_name:
        summary_parts.append(f"模板：{row.template_name}")
    title = (
        f"{release_type_map.get(release_type, release_type)} {row.version or ''}".strip()
        or f"{release_type_map.get(release_type, release_type)} 记录"
    )
    title = f"{title} · {status_label_map.get(status, status)}"
    return {
        "id": f"release:{row.release_id}",
        "category": "release",
        "event_type": release_type,
        "time": format_app_datetime(row.finished_at or row.started_at),
        "title": title,
        "summary": " · ".join(summary_parts),
        "severity": severity_map.get(status, "info"),
        "operator": row.operator or "",
        "entity_id": row.component_id or "",
        "entity_name": row.component_name or "",
    }


def config_revision_timeline_event(row: ApplicationConfigRevision) -> dict[str, Any]:
    """把配置版本变更转成时间线事件。

    凭证引用不展示任何明文，只保留 key 和 credential_ref 标识。
    """
    change_label_map = {"create": "新增", "update": "更新", "delete": "删除"}
    severity_map = {"create": "success", "update": "info", "delete": "warning"}
    change_type = str(row.change_type or "update")
    value_type = str(row.value_type or "plain")
    if value_type == "credential_ref":
        before = str(row.old_credential_ref or "").strip() or "-"
        after = str(row.new_credential_ref or "").strip() or "-"
        summary = f"凭证引用：{before} → {after}" if change_type == "update" else f"凭证引用：{after if change_type != 'delete' else before}"
    else:
        before = str(row.old_value_snapshot or "").strip()
        after = str(row.new_value_snapshot or "").strip()
        if change_type == "delete":
            summary = f"旧值：{before[:80]}" if before else "（值为空）"
        elif change_type == "create":
            summary = f"新值：{after[:80]}" if after else "（值为空）"
        else:
            summary = f"{before[:40]} → {after[:40]}"
    title = f"{change_label_map.get(change_type, change_type)}配置 {row.key or ''}".strip()
    if row.env:
        title = f"{title} · {row.env}"
    return {
        "id": f"config:{row.revision_id}",
        "category": "config",
        "event_type": change_type,
        "time": format_app_datetime(row.created_at),
        "title": title,
        "summary": summary,
        "severity": severity_map.get(change_type, "info"),
        "operator": row.operator or "",
        "entity_id": row.config_id or "",
        "entity_name": row.key or "",
    }


def runtime_control_timeline_event(component: ApplicationComponent) -> dict[str, Any] | None:
    """把运行实例最近一次控制动作转成时间线事件。"""
    action = str(getattr(component, "last_control_action", "") or "").strip()
    timestamp = format_app_datetime(getattr(component, "last_control_at", None))
    if not action or not timestamp:
        return None
    action_label_map = {
        "status": "状态检查",
        "start": "启动",
        "stop": "停止",
        "restart": "重启",
        "update": "更新",
    }
    result = str(getattr(component, "last_control_result", "") or "").lower()
    severity = "success" if result == "success" else "error" if result else "info"
    duration_ms = int(getattr(component, "last_control_duration_ms", 0) or 0)
    summary_bits = [f"耗时 {duration_ms}ms"] if duration_ms else []
    output = str(getattr(component, "last_control_output", "") or "").strip()
    if output:
        # 取输出的最后一段非空行，降低噪音。
        for line in reversed(output.splitlines()):
            trimmed = line.strip()
            if trimmed:
                summary_bits.append(trimmed[:200])
                break
    component_id = getattr(component, "component_id", "") or ""
    component_name = getattr(component, "name", "") or component_id
    return {
        "id": f"runtime:{component_id}:{timestamp}",
        "category": "runtime_control",
        "event_type": action,
        "time": timestamp,
        "title": f"{action_label_map.get(action, action)} {component_name}",
        "summary": " · ".join(summary_bits),
        "severity": severity,
        "operator": "",
        "entity_id": component_id,
        "entity_name": component_name,
    }


def application_alert_timeline_events(
    host_ips: list[str],
    host_names: list[str],
    db: Session,
) -> list[dict[str, Any]]:
    """从 Alertmanager 拉取与当前应用绑定主机相关的活动告警。"""
    if not host_ips and not host_names:
        return []
    try:
        # 延迟导入避免与 applications 模块产生循环依赖风险。
        from app.api.monitoring import (  # noqa: WPS433
            _fetch_alerts,
            _filter_dashboard_alerts,
            read_monitoring_config,
        )

        cfg = read_monitoring_config(db)
        alerts = _filter_dashboard_alerts(_fetch_alerts(cfg), host_ips, host_names)
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for item in alerts[:20]:
        severity_raw = str(item.get("severity") or "warning").lower()
        severity = "error" if severity_raw == "critical" else "warning" if severity_raw == "warning" else "info"
        time_value = normalize_alert_time(str(item.get("time") or ""))
        events.append(
            {
                "id": f"alert:{item.get('id') or item.get('rule') or time_value}",
                "category": "alert",
                "event_type": severity_raw,
                "time": time_value,
                "title": f"告警 {item.get('rule') or '未命名'}",
                "summary": str(item.get("value") or "")[:200],
                "severity": severity,
                "operator": "",
                "entity_id": str(item.get("host") or ""),
                "entity_name": str(item.get("host") or ""),
            }
        )
    return events


def build_application_timeline(
    app: Application,
    db: Session,
    limit: int,
    observability_builder: Callable[[str, str, list[Any], list[Any], Session], dict[str, Any]],
) -> dict[str, Any]:
    """聚合应用时间线。

    数据源全部只读：发布记录、配置变更、运行实例最近一次控制、Alertmanager 活动告警。
    任何数据源失败都降级为空，不阻塞其他来源。
    """
    from app.models.application import ApplicationResource  # noqa: WPS433

    limit = max(1, min(int(limit or 50), 100))
    release_rows = (
        db.query(ApplicationRelease)
        .filter(ApplicationRelease.app_id == app.app_id)
        .order_by(ApplicationRelease.id.desc())
        .limit(limit)
        .all()
    )
    revision_rows = (
        db.query(ApplicationConfigRevision)
        .filter(ApplicationConfigRevision.app_id == app.app_id)
        .order_by(ApplicationConfigRevision.id.desc())
        .limit(limit)
        .all()
    )
    components = db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app.app_id).all()
    resources = db.query(ApplicationResource).filter(ApplicationResource.app_id == app.app_id).all()
    observability = observability_builder(app.app_id, app.name or "", resources, components, db)

    events: list[dict[str, Any]] = []
    events.extend(release_timeline_event(row) for row in release_rows)
    events.extend(config_revision_timeline_event(row) for row in revision_rows)
    for component in components:
        event = runtime_control_timeline_event(component)
        if event:
            events.append(event)
    events.extend(
        application_alert_timeline_events(
            host_ips=observability.get("host_ips", []),
            host_names=observability.get("host_names", []),
            db=db,
        )
    )

    events.sort(key=lambda item: item.get("time") or "", reverse=True)
    truncated = len(events) > limit
    return {
        "app_id": app.app_id,
        "app_name": app.name or "",
        "events": events[:limit],
        "truncated": truncated,
    }
