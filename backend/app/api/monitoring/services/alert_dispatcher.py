"""告警通道分发逻辑：自适应格式化（钉钉/企业微信/通用 JSON Webhook）并执行发送。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from datetime import datetime, timezone
import requests

logger = logging.getLogger("opspilot.alert_dispatcher")

# 重复日志节流：告警引擎每个评估周期（默认 30s）都会跑一遍 dispatch，
# "未绑渠道/未满间隔/已达上限"这类常规跳过若每次都打 info 会把日志刷爆。
# 这里按 key 做时间窗口内只打一次，进程重启后归零（可接受，只是日志）。
_throttled_log_at: dict[str, datetime] = {}


def _log_throttled(key: str, message: str, every_seconds: int = 3600) -> None:
    """同一 key 在 every_seconds 窗口内只记录一次 info 日志。"""
    now_ts = datetime.utcnow()
    last = _throttled_log_at.get(key)
    if last is not None and (now_ts - last).total_seconds() < every_seconds:
        return
    _throttled_log_at[key] = now_ts
    logger.info(message)


def _effective_reminder_interval_minutes(base_minutes: int, firing_age_hours: float) -> int:
    """持续 firing 的告警按"告警年龄"升级重复提醒间隔，避免无限期匀速骚扰。

    2 小时内用配置间隔；2-8 小时至少 2 小时一次；8-48 小时至少 8 小时一次；
    48 小时以上至少 24 小时一次（一天最多提醒一次，直到恢复或停发）。
    """
    if firing_age_hours >= 48:
        return max(base_minutes, 1440)
    if firing_age_hours >= 8:
        return max(base_minutes, 480)
    if firing_age_hours >= 2:
        return max(base_minutes, 120)
    return base_minutes

# 主事件循环句柄。由 app 启动时(lifespan)调用 set_main_event_loop 注入。
# dispatch_active_alerts 可能在工作线程里被调用(告警引擎 runner 用 asyncio.to_thread,
# 或 webhook 的 background_tasks 线程池),此时当前线程没有运行中的事件循环,
# 不能直接 asyncio.create_task。统一通过本句柄把协程安全地调度回主循环。
_main_event_loop: asyncio.AbstractEventLoop | None = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在应用启动时记录主事件循环,供工作线程调度协程使用。"""
    global _main_event_loop
    _main_event_loop = loop


def schedule_coroutine(coro) -> bool:
    """线程安全地调度一个协程到主事件循环。

    - 若当前线程已有运行中的事件循环(在循环线程内调用)→ create_task;
    - 否则(工作线程/线程池内)→ run_coroutine_threadsafe 投递到主循环;
    - 实在拿不到循环 → 记录告警并丢弃该协程(不抛异常,避免影响主流程)。

    返回是否成功调度。失败时调用方应依赖 `_scan_stale_resolving` 兜底，
    避免恢复通知被静默丢弃。
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None:
        running.create_task(coro)
        return True

    if _main_event_loop is not None and _main_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, _main_event_loop)
        return True

    logger.warning("无可用事件循环,恢复防抖协程未能调度(将由兜底扫描补发恢复通知)。")
    coro.close()
    return False


def _get_template_by_id(template_id: str) -> dict[str, Any] | None:
    if not template_id:
        return None
    
    from app.db.database import SessionLocal
    from app.models.system_config import SystemConfig
    
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.config_key == "monitoring_notification_templates").first()
        if not row:
            return None
        payload = json.loads(row.config_value)
        templates = payload.get("templates") or []
        for t in templates:
            if t.get("id") == template_id:
                return t
    except Exception:
        pass
    finally:
        db.close()
    return None


def settings_console_url(path: str = "/monitoring/alerts") -> str:
    """前端控制台地址（用于 IM 通知卡片的处理入口链接）。

    CONSOLE_URL 环境变量可配控制台根地址（不含路径），默认 http://127.0.0.1:5173。
    这里只拼接一次目标路径，避免出现 /monitoring/alerts/monitoring/alerts 双拼。
    """
    import os

    base = (os.environ.get("CONSOLE_URL") or "http://127.0.0.1:5173").strip().rstrip("/")
    suffix = (path or "").strip()
    if not suffix:
        return base
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{base}{suffix}"


def render_template_string(template_str: str, alert: dict[str, Any]) -> str:
    if not template_str:
        return ""
    
    starts_at = alert.get("startsAt") or alert.get("started_at") or "-"
    status_label = "恢复 (Resolved)" if alert.get("status") == "resolved" else "触发 (Firing)"
    
    variables = {
        "alertname": alert.get("name") or "未命名告警",
        "severity": alert.get("severity") or "warning",
        "instance": alert.get("instance") or "-",
        "summary": alert.get("summary") or "无摘要",
        "description": alert.get("description") or "无详细描述",
        "time": starts_at,
        "status": status_label,
        "group": alert.get("group") or "未分组",
        # IM 卡片处理入口：模板可引用 {{console_url}} 把人带回平台认领/分析/闭环。
        "console_url": str(alert.get("console_url") or settings_console_url()),
    }
    
    rendered = template_str
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def send_alert_to_channel(channel: dict[str, Any], alert: dict[str, Any]) -> tuple[bool, str]:
    """向指定通知渠道发送单条告警。

    参数:
        channel: 渠道配置字典，如 {"id": "...", "name": "...", "type": "webhook", "webhook_url": "..."}
        alert: 告警信息字典，如 {"name": "...", "severity": "...", "instance": "...", "summary": "...", "description": "...", "startsAt": "..."}

    返回:
        (success, message)
    """
    webhook_url = str(channel.get("webhook_url") or "").strip()
    if not webhook_url:
        return False, "渠道 Webhook URL 为空"

    if not channel.get("enabled", True):
        return False, "该通知渠道已被禁用"

    template_id = channel.get("template_id")
    template = _get_template_by_id(template_id)

    if template and template.get("enabled", True):
        rendered_title = render_template_string(template.get("title_template") or "", alert)
        rendered_body = render_template_string(template.get("body_template") or "", alert)

        # 根据 webhook_url 的域名和 type 包装 payload
        if "dingtalk.com" in webhook_url:
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": rendered_title or "监控告警",
                    "text": rendered_body
                }
            }
        elif "qyapi.weixin.qq.com" in webhook_url:
            content = f"### {rendered_title}\n{rendered_body}" if rendered_title else rendered_body
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": content
                }
            }
        else:
            payload = _build_generic_payload(alert, channel)
            payload["title"] = rendered_title
            payload["text"] = rendered_body
            if "commonAnnotations" in payload:
                payload["commonAnnotations"]["summary"] = rendered_title or payload["commonAnnotations"].get("summary", "")
                payload["commonAnnotations"]["description"] = rendered_body or payload["commonAnnotations"].get("description", "")
            if payload.get("alerts"):
                payload["alerts"][0]["annotations"]["summary"] = rendered_title or payload["alerts"][0]["annotations"].get("summary", "")
                payload["alerts"][0]["annotations"]["description"] = rendered_body or payload["alerts"][0]["annotations"].get("description", "")
    else:
        # 根据 webhook_url 的域名智能判断消息格式
        if "dingtalk.com" in webhook_url:
            payload = _build_dingtalk_payload(alert)
        elif "qyapi.weixin.qq.com" in webhook_url:
            payload = _build_wechat_payload(alert)
        else:
            payload = _build_generic_payload(alert, channel)

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8)
        if resp.status_code >= 400:
            logger.error(f"发送告警到渠道 [{channel.get('name')}] 失败: HTTP {resp.status_code} - {resp.text}")
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return True, "发送成功"
    except Exception as exc:
        logger.exception(f"发送告警到渠道 [{channel.get('name')}] 异常: {str(exc)}")
        return False, f"连接异常: {str(exc)}"


def _mask_target(url: str) -> str:
    """脱敏渠道目标地址:保留域名与路径前缀,隐藏 token/key 查询串。"""
    s = str(url or "").strip()
    if not s:
        return ""
    # 截断查询参数(钉钉/企微/飞书的 access_token、key 都在 query 里)
    base = s.split("?", 1)[0]
    if len(base) > 80:
        base = base[:77] + "..."
    return base


def send_alert_and_log(channel: dict[str, Any], alert: dict[str, Any], event_type: str = "firing") -> tuple[bool, str]:
    """发送告警并写入通知发送记录(审计)。失败也照常记录,不影响主流程。

    event_type: firing(触发) / resolved(恢复)。
    """
    success, message = send_alert_to_channel(channel, alert)
    try:
        from app.db.database import SessionLocal
        from app.models.monitoring_notification_log import MonitoringNotificationLog
        log_db = SessionLocal()
        try:
            log_db.add(MonitoringNotificationLog(
                fingerprint=str(alert.get("fingerprint") or "")[:64],
                alert_name=str(alert.get("name") or "")[:100],
                instance=str(alert.get("instance") or "")[:100],
                severity=str(alert.get("severity") or "warning")[:20],
                event_type=str(event_type or "firing")[:20],
                channel_id=str(channel.get("id") or "")[:64],
                channel_name=str(channel.get("name") or "")[:100],
                channel_type=str(channel.get("type") or "")[:30],
                target=_mask_target(channel.get("webhook_url"))[:255],
                success=bool(success),
                message=str(message or "")[:2000],
                created_at=datetime.utcnow(),
            ))
            log_db.commit()
        finally:
            log_db.close()
    except Exception:
        logger.exception("写入通知发送记录失败(不影响告警发送)")
    return success, message



def _build_dingtalk_payload(alert: dict[str, Any]) -> dict[str, Any]:
    """构建钉钉 Markdown 告警载荷。"""
    alertname = alert.get("name") or "未命名告警"
    severity = alert.get("severity") or "warning"
    instance = alert.get("instance") or "-"
    summary = alert.get("summary") or "无摘要"
    desc = alert.get("description") or "无详细描述"
    starts_at = alert.get("startsAt") or alert.get("started_at") or "-"
    group = alert.get("group") or "未分组"

    # 级别颜色与标签
    severity_label = "严重 (Critical)" if severity == "critical" else ("警告 (Warning)" if severity == "warning" else "信息 (Info)")
    status_icon = "🔴" if severity == "critical" else ("🟡" if severity == "warning" else "🔵")

    title = f"{status_icon} 监控告警: {alertname}"
    markdown_text = f"### {status_icon} 监控告警: {alertname}\n\n" \
                    f"**告警级别**: {severity_label}\n\n" \
                    f"**所属分组**: {group}\n\n" \
                    f"**触发实例**: `{instance}`\n\n" \
                    f"**告警摘要**: {summary}\n\n" \
                    f"**详细描述**: {desc}\n\n" \
                    f"**触发时间**: {starts_at}\n\n" \
                    f"*[OpsPilot 智能运维平台]*"

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text
        }
    }


def _build_wechat_payload(alert: dict[str, Any]) -> dict[str, Any]:
    """构建企业微信 Markdown 告警载荷。"""
    alertname = alert.get("name") or "未命名告警"
    severity = alert.get("severity") or "warning"
    instance = alert.get("instance") or "-"
    summary = alert.get("summary") or "无摘要"
    desc = alert.get("description") or "无详细描述"
    starts_at = alert.get("startsAt") or alert.get("started_at") or "-"
    group = alert.get("group") or "未分组"

    color_label = "info"
    if severity == "critical":
        color_label = "warning"
        status_color = "warning"
    elif severity == "warning":
        color_label = "comment"
        status_color = "comment"
    else:
        status_color = "info"

    markdown_text = f"### 监控告警: <font color=\"{status_color}\">{alertname}</font>\n" \
                    f"> **告警级别**: <font color=\"{status_color}\">{severity}</font>\n" \
                    f"> **所属分组**: {group}\n" \
                    f"> **触发实例**: `{instance}`\n" \
                    f"> **告警摘要**: {summary}\n" \
                    f"> **详细描述**: {desc}\n" \
                    f"> **触发时间**: {starts_at}\n\n" \
                    f"*[OpsPilot 智能运维平台]*"

    return {
        "msgtype": "markdown",
        "markdown": {
            "content": markdown_text
        }
    }


def _build_generic_payload(alert: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    """构建通用 Webhook 告警载荷（兼容 Alertmanager 结构）。"""
    alertname = alert.get("name") or "未命名告警"
    severity = alert.get("severity") or "warning"
    instance = alert.get("instance") or "-"
    summary = alert.get("summary") or "无摘要"
    desc = alert.get("description") or "无详细描述"
    starts_at = alert.get("startsAt") or alert.get("started_at") or "-"

    # 包装为 Alertmanager 标准的 grouped alerts 格式，利于上游系统对接
    return {
        "receiver": channel.get("name") or "opspilot-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "severity": severity,
                    "instance": instance,
                    "group": alert.get("group") or "",
                    "opspilot_rule_id": alert.get("opspilot_rule_id", "rule-manual")
                },
                "annotations": {
                    "summary": summary,
                    "description": desc
                },
                "startsAt": starts_at,
                "endsAt": "0001-01-01T00:00:00Z"
            }
        ],
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname, "severity": severity, "instance": instance},
        "commonAnnotations": {"summary": summary, "description": desc}
    }


def _notify_im_users(db, user_ids: list[int], alert_data: dict[str, Any], event_type: str) -> None:
    """按规则配置给飞书通知人发群 @ 消息（best-effort，绝不阻断告警派发）。

    复杂逻辑中文注释：
    - 通知人未绑定飞书 / 已关闭通知时，由 im_feishu.notify_users_alert 内部逐个
      跳过并计数，这里不重复判断；
    - 任何异常只记日志——IM 是增值触达通道，不能因它故障影响告警主链路。
    """
    if not user_ids:
        return
    try:
        from app.services.im_feishu import notify_users_alert

        notify_users_alert(
            db,
            user_ids,
            title=f"告警{event_type}：{alert_data.get('name') or ''}",
            content=(
                f"{alert_data.get('summary') or ''}\n"
                f"实例: {alert_data.get('instance') or '-'}  级别: {alert_data.get('severity') or '-'}"
            ),
            link=settings_console_url("/monitoring/alerts"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"飞书 @ 通知人发送跳过: {exc}")


def finalize_resolved_alert(
    db,
    fingerprint: str,
    channel_ids: list[str],
    channel_map: dict[str, Any],
    alert_settings: dict[str, Any],
    im_user_ids: list[int] | None,
    labels: dict[str, Any] | None,
    resolved_at: datetime | None = None,
) -> bool:
    """正式结束一条 resolving/firing 告警：发恢复通知 + 归档历史 + 从活动表删除。

    被 `handle_resolved_alert_grace` 与兜底扫描 `_scan_stale_resolving` 共用，
    保证恢复通知落库（MonitoringNotificationLog）且活动告警可清理。
    时间一律用 UTC，与 monitoring_active_alert 的 utcnow 默认值对齐。
    """
    from app.models.monitoring_active_alert import MonitoringActiveAlert
    from app.models.monitoring_history_alert import MonitoringHistoryAlert

    now = resolved_at or datetime.utcnow()
    db_alert = db.query(MonitoringActiveAlert).filter(MonitoringActiveAlert.fingerprint == fingerprint).first()
    if not db_alert:
        return False

    _labels = labels if isinstance(labels, dict) else {}
    if not _labels:
        try:
            _labels = json.loads(db_alert.labels) if db_alert.labels else {}
        except Exception:
            _labels = {}

    send_resolved_global = alert_settings.get("send_resolved_global", True)
    if send_resolved_global:
        alert_data = {
            "name": db_alert.name,
            "severity": db_alert.severity,
            "instance": db_alert.instance,
            "summary": f"[已恢复] {db_alert.summary}",
            "description": (
                f"告警已恢复。首发时间: "
                f"{(db_alert.first_seen_at or now).strftime('%Y-%m-%d %H:%M:%S')} UTC"
            ),
            "startsAt": (db_alert.first_seen_at or now).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "resolved",
            "group": _labels.get("group") or "",
        }
        for cid in channel_ids or []:
            channel = channel_map.get(cid)
            if channel and channel.get("enabled", True):
                if not channel.get("send_resolved", True):
                    continue
                logger.info(f"向通道 [{channel.get('name')}] 发送恢复通知: {db_alert.name}")
                send_alert_and_log(channel, alert_data, "resolved")
        if im_user_ids:
            _notify_im_users(db, im_user_ids, alert_data, "已恢复")

    db.add(
        MonitoringHistoryAlert(
            fingerprint=db_alert.fingerprint,
            name=db_alert.name,
            instance=db_alert.instance,
            job=db_alert.job,
            severity=db_alert.severity,
            summary=db_alert.summary,
            description=db_alert.description,
            group=_labels.get("group") or "",
            started_at=db_alert.active_at,
            resolved_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            status="resolved",
        )
    )
    db.delete(db_alert)
    db.commit()
    logger.info(f"告警指纹 [{fingerprint}] 已恢复归档。")
    return True


async def handle_resolved_alert_grace(
    fingerprint: str,
    grace_seconds: int,
    candidate_time: datetime,
    target_channel_ids: list[str],
    channel_map: dict[str, Any],
    alert_settings: dict[str, Any],
    im_user_ids: list[int] | None = None,
):
    """异步恢复防抖计时协程。

    在指定秒数之后，检查该告警指纹是否仍处于待恢复（resolving）状态，且未被重新触发。
    如果是，则正式发送恢复通知并清理数据库。
    """
    await asyncio.sleep(grace_seconds)

    from app.db.database import SessionLocal
    from app.models.monitoring_active_alert import MonitoringActiveAlert

    db = SessionLocal()
    try:
        db_alert = db.query(MonitoringActiveAlert).filter(MonitoringActiveAlert.fingerprint == fingerprint).first()
        if db_alert and db_alert.state == "resolving" and db_alert.recovery_candidate_at:
            db_cand_str = db_alert.recovery_candidate_at.strftime('%Y-%m-%d %H:%M:%S')
            cand_str = candidate_time.strftime('%Y-%m-%d %H:%M:%S')
            if db_cand_str == cand_str:
                # 状态依然是待恢复，并且时间戳完全一致（没有被再次触发）
                finalize_resolved_alert(
                    db,
                    fingerprint=fingerprint,
                    channel_ids=target_channel_ids,
                    channel_map=channel_map,
                    alert_settings=alert_settings,
                    im_user_ids=im_user_ids,
                    labels=None,
                )
    except Exception as exc:
        db.rollback()
        logger.exception(f"异步处理恢复防抖任务发生异常: {str(exc)}")
    finally:
        db.close()


def dispatch_active_alerts(
    alerts: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    silences: list[dict[str, Any]],
    now: datetime,
    alert_settings: dict[str, Any] | None = None
):
    """根据规则、静默配置与全局策略，分发活跃告警并进行降噪限流。"""
    if alert_settings is None:
        alert_settings = {
            "default_channel_ids": [],
            "global_routing_mode": "fallback",
            "send_resolved_global": True,
            "recovery_debounce_seconds": 60,
            "reminder_interval_minutes": 30,
            "max_reminders_cap": 0,
            "reminder_max_age_hours": 168
        }

    from app.db.database import SessionLocal
    from app.models.monitoring_active_alert import MonitoringActiveAlert
    from app.api.monitoring.services.dashboard import _calculate_fingerprint
    import asyncio

    # 过滤出当前的有效静默规则
    active_silence_rules = []
    for s in silences:
        try:
            start_dt = datetime.strptime(s.get("starts_at", ""), "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(s.get("ends_at", ""), "%Y-%m-%d %H:%M:%S")
            if start_dt <= now <= end_dt:
                active_silence_rules.append(s)
        except Exception:
            pass

    # 将 rules 和 channels 转换为映射
    rule_map = {r.get("id"): r for r in rules}
    channel_map = {c.get("id"): c for c in channels}

    db = SessionLocal()
    try:
        for alert in alerts:
            labels = alert.get("labels") or {}
            annotations = alert.get("annotations") or {}
            alert_name = labels.get("alertname")
            instance = labels.get("instance") or ""
            opspilot_rule_id = labels.get("opspilot_rule_id")

            if not alert_name:
                continue

            status = alert.get("status") or "firing"
            is_firing = status == "firing"
            is_resolved = status == "resolved"

            # 仅在 firing/resolved 状态下分发通知
            if not is_firing and not is_resolved:
                logger.info(f"告警 [{alert_name}] ({instance}) 状态为 {status}，跳过通知分发。")
                continue

            # 1. 校验是否被静默
            is_silenced = False
            for rule in active_silence_rules:
                if rule.get("alert_name") == alert_name:
                    rule_inst = rule.get("instance") or ""
                    if not rule_inst or rule_inst == instance:
                        is_silenced = True
                        break

            if is_silenced:
                logger.info(f"告警 [{alert_name}] ({instance}) 已静默，跳过通知分发。")
                continue

            # 2. 匹配绑定的通道 ID 及具体规则
            bound_channel_ids = []
            active_rule = None
            if opspilot_rule_id and opspilot_rule_id in rule_map:
                active_rule = rule_map[opspilot_rule_id]
                bound_channel_ids = active_rule.get("channel_ids") or []
            else:
                for r in rules:
                    if r.get("name") == alert_name:
                        active_rule = r
                        bound_channel_ids = r.get("channel_ids") or []
                        break

            # 飞书 @ 通知人（规则级）：与 webhook 渠道并行的独立触达路径，
            # 规则只配了通知人、没配渠道时也应该能通知到人。
            im_user_ids = list((active_rule or {}).get("im_user_ids") or []) if active_rule else []

            # 运用全局路由策略
            default_channel_ids = alert_settings.get("default_channel_ids") or []
            global_routing_mode = alert_settings.get("global_routing_mode") or "fallback"

            target_channel_ids = []
            if global_routing_mode == "all":
                target_channel_ids = list(set(bound_channel_ids + default_channel_ids))
            else:  # fallback
                if not bound_channel_ids:
                    target_channel_ids = default_channel_ids
                else:
                    target_channel_ids = bound_channel_ids

            if not target_channel_ids and not im_user_ids:
                _log_throttled(
                    f"no-channel:{alert_name}:{instance}",
                    f"告警 [{alert_name}] ({instance}) 未绑定任何通知渠道/通知人且无全局兜底渠道，不发送通知。（同一告警 1 小时内只记一次）",
                )
                continue

            # 3. 构造规范化的时间（统一 UTC，与引擎/活动表一致）
            starts_at_raw = alert.get("startsAt") or ""
            starts_at_local = starts_at_raw
            if starts_at_raw:
                try:
                    dt = datetime.fromisoformat(starts_at_raw.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(tz=timezone.utc).replace(tzinfo=None)
                    starts_at_local = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            # 4. 计算告警特征指纹
            fp = _calculate_fingerprint(alert_name, instance, labels)
            db_alert = db.query(MonitoringActiveAlert).filter(MonitoringActiveAlert.fingerprint == fp).first()

            if is_firing:
                # 处理 firing 告警
                should_notify = False
                if not db_alert:
                    # 首次触发，在数据库中创建记录并触发通知
                    db_alert = MonitoringActiveAlert(
                        fingerprint=fp,
                        name=alert_name,
                        instance=instance,
                        job=labels.get("job") or "",
                        state="firing",
                        severity=labels.get("severity") or "warning",
                        summary=annotations.get("summary") or alert_name,
                        description=annotations.get("description") or "请登录 OpsPilot 告警中心查看详情",
                        active_at=starts_at_local,
                        first_seen_at=now,
                        last_seen_at=now,
                        labels=json.dumps(labels, ensure_ascii=False),
                        last_notified_at=now,
                        notification_count=1,
                        recovery_candidate_at=None
                    )
                    db.add(db_alert)
                    db.commit()
                    should_notify = True
                    logger.info(f"告警 [{alert_name}] ({instance}) 首次触发，写入数据库并发送通知。")
                else:
                    # 已经在数据库中
                    if db_alert.state == "resolving":
                        # 之前在恢复留观防抖中，现在重新触发
                        db_alert.state = "firing"
                        db_alert.recovery_candidate_at = None
                        db_alert.last_seen_at = now
                        db.commit()
                        logger.info(f"告警 [{alert_name}] ({instance}) 在防抖期内重新触发，重置状态为 firing，不发送重复通知。")
                    else:
                        # 正常处于 firing 状态，检查是否需要发送重复提醒
                        reminder_interval_minutes = alert_settings.get("reminder_interval_minutes", 30)
                        max_reminders_cap = alert_settings.get("max_reminders_cap", 0)
                        # 超过该时长仍未恢复的告警停止重复提醒（告警保留在中心，恢复通知不受影响）；0=不限制
                        reminder_max_age_hours = alert_settings.get("reminder_max_age_hours", 168)
                        if active_rule:
                            reminder_interval_minutes = active_rule.get("reminder_interval_minutes", reminder_interval_minutes)
                            max_reminders_cap = active_rule.get("max_reminders_cap", max_reminders_cap)
                            reminder_max_age_hours = active_rule.get("reminder_max_age_hours", reminder_max_age_hours)

                        last_notified = db_alert.last_notified_at or db_alert.first_seen_at
                        elapsed_seconds = (now - last_notified).total_seconds()

                        # 告警年龄 = 距首次触发的小时数，用于升级提醒间隔与停发判断
                        first_seen_ref = db_alert.first_seen_at or last_notified or now
                        firing_age_hours = max(0.0, (now - first_seen_ref).total_seconds()) / 3600.0

                        if reminder_max_age_hours > 0 and firing_age_hours >= reminder_max_age_hours:
                            _log_throttled(
                                f"remind-expired:{fp}",
                                f"告警 [{alert_name}] ({instance}) 已持续 {firing_age_hours:.0f} 小时未恢复，"
                                f"超过最大重复提醒时长 {reminder_max_age_hours} 小时，停止重复通知（恢复通知仍会发送）。",
                            )
                        elif elapsed_seconds >= _effective_reminder_interval_minutes(reminder_interval_minutes, firing_age_hours) * 60:
                            if max_reminders_cap == 0 or db_alert.notification_count < max_reminders_cap:
                                db_alert.last_notified_at = now
                                db_alert.notification_count += 1
                                db_alert.last_seen_at = now
                                db.commit()
                                should_notify = True
                                logger.info(f"告警 [{alert_name}] ({instance}) 触发重复通知，发送第 {db_alert.notification_count} 次。")
                            else:
                                _log_throttled(
                                    f"remind-capped:{fp}",
                                    f"告警 [{alert_name}] ({instance}) 已达最大通知次数 {max_reminders_cap}，跳过发送。",
                                )
                        else:
                            # 常规"未满间隔"每 30s 评估周期都会走到，降为 debug 防止日志刷屏
                            effective_interval = _effective_reminder_interval_minutes(reminder_interval_minutes, firing_age_hours)
                            logger.debug(f"告警 [{alert_name}] ({instance}) 距离上次通知未满 {effective_interval} 分钟，跳过发送。")

                if should_notify:
                    alert_data = {
                        "name": alert_name,
                        "severity": labels.get("severity") or "warning",
                        "instance": instance,
                        "summary": annotations.get("summary") or alert_name,
                        "description": annotations.get("description") or "请登录 OpsPilot 告警中心查看详情",
                        "startsAt": starts_at_local,
                        "opspilot_rule_id": opspilot_rule_id,
                        "group": labels.get("group") or "",
                    }
                    for cid in target_channel_ids:
                        channel = channel_map.get(cid)
                        if channel and channel.get("enabled", True):
                            logger.info(f"开始向通道 [{channel.get('name')}] 发送触发通知: {alert_name}")
                            send_alert_and_log(channel, alert_data, "firing")

                    # 飞书 @ 通知人（触发事件）
                    if im_user_ids:
                        _notify_im_users(db, im_user_ids, alert_data, "触发")

            elif is_resolved:
                # 处理 resolved 告警
                recovery_debounce_seconds = alert_settings.get("recovery_debounce_seconds", 60)
                if active_rule:
                    recovery_debounce_seconds = active_rule.get("recovery_debounce_seconds", recovery_debounce_seconds)
                
                if recovery_debounce_seconds > 0 and db_alert:
                    # 进入恢复防抖阶段
                    db_alert.state = "resolving"
                    db_alert.recovery_candidate_at = now
                    db.commit()
                    logger.info(f"告警 [{alert_name}] ({instance}) 已触发恢复，进入防抖期（{recovery_debounce_seconds}秒）。")
                    # 启动异步协程休眠检查(线程安全:dispatch 可能运行在工作线程)
                    scheduled = schedule_coroutine(
                        handle_resolved_alert_grace(
                            fingerprint=fp,
                            grace_seconds=recovery_debounce_seconds,
                            candidate_time=now,
                            target_channel_ids=target_channel_ids,
                            channel_map=channel_map,
                            alert_settings=alert_settings,
                            im_user_ids=im_user_ids,
                        )
                    )
                    if not scheduled:
                        # 协程被丢弃时由 runner 的 _scan_stale_resolving 兜底补发恢复
                        logger.info(f"告警 [{alert_name}] ({instance}) 恢复防抖协程未调度，等待兜底扫描补发。")
                else:
                    # 无需防抖，或者数据库中无记录，直接发送恢复消息并清理数据库记录
                    if db_alert:
                        try:
                            _arc_labels2 = json.loads(db_alert.labels) if db_alert.labels else {}
                        except Exception:
                            _arc_labels2 = {}
                        finalize_resolved_alert(
                            db,
                            fingerprint=fp,
                            channel_ids=target_channel_ids,
                            channel_map=channel_map,
                            alert_settings=alert_settings,
                            im_user_ids=im_user_ids,
                            labels=_arc_labels2,
                            resolved_at=now if now.tzinfo is None else now.replace(tzinfo=None),
                        )
                    else:
                        send_resolved_global = alert_settings.get("send_resolved_global", True)
                        if send_resolved_global:
                            alert_data = {
                                "name": alert_name,
                                "severity": labels.get("severity") or "warning",
                                "instance": instance,
                                "summary": f"[已恢复] {annotations.get('summary') or alert_name}",
                                "description": f"告警已恢复。触发时间: {starts_at_local}",
                                "startsAt": starts_at_local,
                                "status": "resolved",
                                "group": labels.get("group") or "",
                            }
                            for cid in target_channel_ids:
                                channel = channel_map.get(cid)
                                if channel and channel.get("enabled", True):
                                    if not channel.get("send_resolved", True):
                                        continue
                                    logger.info(f"立即向通道 [{channel.get('name')}] 发送恢复通知: {alert_name}")
                                    send_alert_and_log(channel, alert_data, "resolved")
                            if im_user_ids:
                                _notify_im_users(db, im_user_ids, alert_data, "已恢复")

    except Exception as exc:
        db.rollback()
        logger.exception(f"分发活跃告警与降噪逻辑异常: {str(exc)}")
    finally:
        db.close()


