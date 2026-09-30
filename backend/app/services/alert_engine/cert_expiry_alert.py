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

from app.db.database import SessionLocal

logger = logging.getLogger(__name__)

# 默认每 6 小时扫一次;到期告警靠 dispatch 内部防抖避免刷屏
SCAN_INTERVAL_SECONDS = 6 * 3600
STARTUP_DELAY_SECONDS = 40

# 合成规则 id/名称:让 dispatch 能按规则匹配通道,并在前端按 opspilot_rule_id 归类
CERT_EXPIRY_RULE_ID = "builtin-cert-expiry"
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


async def start_cert_expiry_alert_runner() -> None:
    """常驻后台任务:周期扫描证书到期并经告警管道派发。随后端进程启停。"""
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
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
                        dispatch_active_alerts,
                        events, rules, channels, silences, datetime.now(), alert_settings,
                    )
            except asyncio.CancelledError:
                logger.info("证书到期告警扫描器收到取消信号,退出。")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"证书到期告警扫描器异常: {exc}", exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
