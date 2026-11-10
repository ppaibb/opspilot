"""证书到期告警扫描器(方案A · 后台扫表)

不依赖 PromQL/VictoriaMetrics。每个周期扫描证书表,对临近到期/已过期的证书
构造 Alertmanager 风格告警事件,复用自建告警引擎的 dispatch_active_alerts 管道
(静默/路由/防抖/active_alerts 落库/飞书等渠道推送)。

随后端进程启停,与证书续期/域名到期同步等 runner 同一模式(asyncio.create_task)。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.api.monitoring.constants import CERT_EXPIRY_RULE_ID
from app.db.database import SessionLocal

logger = logging.getLogger(__name__)

# 默认每 6 小时扫一次;到期告警靠 dispatch 内部防抖避免刷屏
SCAN_INTERVAL_SECONDS = 6 * 3600
STARTUP_DELAY_SECONDS = 40

# 兜底通知去重窗口：证书到期是慢变量，24 小时内同一证书只发一次兜底站内信，
# 避免每 6 小时扫描刷屏；24 小时后仍未处理则再提醒一次。
CERT_EXPIRY_FALLBACK_REMINDER_HOURS = 24

# 合成规则名称:让 dispatch 能按规则匹配通道,并在前端按 opspilot_rule_id 归类
CERT_EXPIRY_RULE_NAME = "证书即将到期"


def _build_cert_events(db) -> list[dict]:
    """扫描证书,产出本轮 firing/resolved 事件列表。"""
    from app.models.certificate import CertificateRecord
    from app.api.certificates import calc_certificate_status
    from app.services.alert_engine.engine import _build_alert_event

    events: list[dict] = []
    try:
        certs = db.query(CertificateRecord).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"证书到期告警:读取证书失败: {exc}")
        return events

    now = datetime.now()
    for cert in certs:
        not_after = (cert.not_after or "").strip()
        renew_days = int(cert.renew_before_days or 30)
        status = calc_certificate_status(not_after, renew_days, cert.status or "issued")
        domain = cert.primary_domain or cert.name or cert.cert_id

        # 计算剩余天数(用于文案)
        days_left = None
        if not_after:
            try:
                expire_at = datetime.strptime(not_after, "%Y-%m-%d %H:%M:%S")
                days_left = (expire_at - now).days
            except Exception:  # noqa: BLE001
                days_left = None

        breaching = status in {"expiring", "expired"}
        severity = "critical" if status == "expired" else "warning"
        labels = {
            "instance": domain,
            "cert_id": cert.cert_id or "",
            "domain": domain,
            "not_after": not_after,
        }
        if days_left is not None:
            labels["days_left"] = str(days_left)

        if status == "expired":
            summary = f"证书已过期:{domain}"
            desc = f"证书 {domain} 已于 {not_after} 过期,请尽快续签或更换。"
        elif status == "expiring":
            day_txt = f"剩余 {days_left} 天" if days_left is not None else "即将到期"
            summary = f"证书即将到期:{domain}（{day_txt}）"
            desc = f"证书 {domain} 将于 {not_after} 到期（{day_txt}），请及时续签。"
        else:
            summary = f"证书正常:{domain}"
            desc = ""

        event = _build_alert_event(
            rule_id=CERT_EXPIRY_RULE_ID,
            name=CERT_EXPIRY_RULE_NAME,
            severity=severity,
            labels=labels,
            value=float(days_left) if days_left is not None else 0.0,
            summary_tpl=summary,
            desc_tpl=desc,
            status="firing" if breaching else "resolved",
            active_at=now,
        )
        # 仅对越界证书发 firing;未越界的统一作为 resolved(dispatch 自行判断是否曾 firing)
        events.append(event)

    # 只保留有意义的事件:firing 全留;resolved 也留(让 dispatch 关闭曾经的告警)
    return events


def _scan_and_collect() -> list[dict]:
    db = SessionLocal()
    try:
        return _build_cert_events(db)
    finally:
        db.close()


def _load_dispatch_context():
    """复用告警引擎的派发上下文加载(规则/渠道/静默/策略)。"""
    from app.api.monitoring import read_monitoring_rules
    from app.api.monitoring.services.notifications import read_monitoring_notifications
    from app.api.alert_center import _read_silences_config, _read_alert_settings

    db = SessionLocal()
    try:
        rules, _ = read_monitoring_rules(db)
        channels, _ = read_monitoring_notifications(db)
        silences = _read_silences_config(db)
        alert_settings = _read_alert_settings(db)
        return rules, channels, silences, alert_settings
    finally:
        db.close()


def _event_has_notification_path(event: dict, rules: list[dict], alert_settings: dict | None) -> bool:
    """本轮 dispatch 是否有路把该事件发出去（规则绑定的渠道 / 规则 @ 人 / 全局兜底渠道）。

    有路可走时兜底不插手，避免同一告警既走渠道又发站内信；只有三者全空
    （规则未命中、规则未绑渠道、也无全局兜底渠道——默认即断的那个状态）
    时，兜底才发全体管理员站内信。
    """
    labels = event.get("labels") or {}
    rule_id = str(labels.get("opspilot_rule_id") or "")
    alert_name = str(labels.get("alertname") or "")
    rule: dict | None = None
    for item in rules or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == rule_id or (alert_name and str(item.get("name") or "") == alert_name):
            rule = item
            break
    bound_channel_ids = list((rule or {}).get("channel_ids") or [])
    im_user_ids = list((rule or {}).get("im_user_ids") or [])
    default_channel_ids = list(((alert_settings or {}).get("default_channel_ids")) or [])
    return bool(bound_channel_ids or im_user_ids or default_channel_ids)


def _notify_cert_expiry_fallback(db, events: list[dict], rules: list[dict], alert_settings: dict | None) -> int:
    """全体管理员站内信兜底：到期/已过期告警无任何通知出口时也要让人看到。

    只覆盖 firing 事件（resolved 不发恢复通知）；复用现有的 create_notification
    站内信链路，不另造通知系统。24 小时内同一证书已发过兜底通知则跳过。
    返回实际发送的通知条数。
    """
    from datetime import timedelta

    from app.api.notifications import NotificationCreate, create_notification
    from app.models.notification import UserNotification
    from app.models.user import User

    sent = 0
    cutoff = datetime.utcnow() - timedelta(hours=CERT_EXPIRY_FALLBACK_REMINDER_HOURS)
    for event in events or []:
        if (event.get("status") or "") != "firing":
            continue
        labels = event.get("labels") or {}
        if str(labels.get("opspilot_rule_id") or "") != CERT_EXPIRY_RULE_ID:
            continue
        if _event_has_notification_path(event, rules, alert_settings):
            continue
        cert_id = str(labels.get("cert_id") or "").strip()
        domain = str(labels.get("instance") or "").strip() or cert_id
        annotations = event.get("annotations") or {}
        summary = str(annotations.get("summary") or "").strip()
        expired = str(labels.get("severity") or "") == "critical" or "已过期" in summary
        title = "证书已过期" if expired else "证书即将到期"
        recent = (
            db.query(UserNotification)
            .filter(
                UserNotification.category == "certificate",
                UserNotification.resource_id == cert_id,
                UserNotification.title == title,
                UserNotification.created_at >= cutoff,
            )
            .first()
        )
        if recent is not None:
            continue
        content = str(annotations.get("description") or summary or "").strip()
        if not content:
            content = f"证书 {domain} 即将到期，请及时续签。"
        admins = db.query(User).filter(User.role == "admin").all()
        for admin in admins:
            try:
                create_notification(
                    db,
                    data=NotificationCreate(
                        user_id=admin.id,
                        type="critical" if expired else "warning",
                        category="certificate",
                        title=title,
                        content=content,
                        link="/assets/certificates",
                        resource_id=cert_id,
                    ),
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"证书到期兜底通知发送失败（管理员 {admin.id}）: {exc}")
        if admins:
            logger.info(f"证书到期告警 [{domain}] 无规则/渠道可配，已发全体管理员站内兜底通知。")
    return sent


def _dispatch_with_fallback(
    events: list[dict],
    rules: list[dict],
    channels: list[dict],
    silences: list[dict],
    now: datetime,
    alert_settings: dict | None,
) -> None:
    """先走正常告警派发，再对无任何出口的证书到期 firing 事件发站内信兜底。"""
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts

    dispatch_active_alerts(events, rules, channels, silences, now, alert_settings)
    db = SessionLocal()
    try:
        _notify_cert_expiry_fallback(db, events, rules, alert_settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"证书到期兜底通知异常: {exc}", exc_info=True)
    finally:
        db.close()


async def start_cert_expiry_alert_runner() -> None:
    """常驻后台任务:周期扫描证书到期并经告警管道派发。随后端进程启停。"""
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "cert_expiry_alert",
        "证书到期告警扫描",
        "每 6 小时扫描证书表，按剩余有效期经告警管道派发到期告警（与手动告警规则同一出口）",
        SCAN_INTERVAL_SECONDS,
    )
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info("证书到期告警扫描器(方案A 后台扫表)已启动。")
    while True:
        with track_run("cert_expiry_alert"):
            try:
                events = await asyncio.to_thread(_scan_and_collect)
                if events:
                    rules, channels, silences, alert_settings = await asyncio.to_thread(_load_dispatch_context)
                    await asyncio.to_thread(
                        _dispatch_with_fallback,
                        events, rules, channels, silences, datetime.now(), alert_settings,
                    )
            except asyncio.CancelledError:
                logger.info("证书到期告警扫描器收到取消信号,退出。")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"证书到期告警扫描器异常: {exc}", exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
