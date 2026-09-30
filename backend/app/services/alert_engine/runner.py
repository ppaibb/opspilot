"""自建告警引擎 · 嵌入式常驻运行器(runner)

替代 vmalert:作为后端进程内的后台 asyncio 任务,随 uvicorn 启停(与现有
健康检查/证书续期/域名到期同步等 runner 同一模式),不拆成独立服务。
夜莺(n9e)也是单进程内跑评估循环,这里对齐该做法。

每个评估周期:
  1) asyncio.to_thread 跑 evaluate_once(重 IO:VictoriaMetrics 查询 + DB),产出 firing/resolved 事件;
  2) asyncio.to_thread 读取规则/渠道/静默/策略(阻塞 DB);
  3) asyncio.to_thread 跑 dispatch_active_alerts(含同步 HTTP 发送 timeout=8s + DB 提交),
     放工作线程避免阻塞事件循环;其内部恢复防抖协程经 schedule_coroutine 安全调度回主循环。
"""
import asyncio
import json
import logging
from datetime import datetime

from app.db.database import SessionLocal
from app.services.alert_engine.engine import evaluate_once

logger = logging.getLogger(__name__)

EVAL_INTERVAL_SECONDS = 30
# 启动后延迟首跑,避开应用启动期资源争抢(与其它 runner 一致)
STARTUP_DELAY_SECONDS = 20


def _run_evaluate() -> list[dict]:
    """阻塞:跑一轮评估,返回本轮 firing/resolved 事件(供线程池调用)。

    通过数据库租约保证多实例部署下同一评估周期只有一个实例执行,
    避免重复产生告警事件与重复派发。
    """
    from app.services.leases import acquire_lease, lease_owner, release_lease

    db = SessionLocal()
    owner = lease_owner()
    lease_key = "alert_engine:evaluate"
    # TTL 略大于评估周期,持有者崩溃后 90s 内可被其他实例接管。
    ttl_seconds = 90
    try:
        if not acquire_lease(db, lease_key, owner, ttl_seconds=ttl_seconds):
            logger.debug("告警引擎评估租约被其他实例持有,本周期跳过。")
            return []
        try:
            stats = evaluate_once(db)
            events = stats.get("events") or []
            log_stats = {k: v for k, v in stats.items() if k != "events"}
            logger.info(f"告警引擎执行统计: {log_stats} | 派发事件数={len(events)}")
            return events
        finally:
            release_lease(db, lease_key, owner)
    finally:
        db.close()


def _scan_stale_resolving() -> int:
    """扫描超过防抖期仍处于 resolving 的活动告警，补发恢复并归档。

    兜底场景：handle_resolved_alert_grace 协程因无事件循环被丢弃、或进程重启
    导致协程消失。返回处理条数。
    """
    from datetime import datetime

    from app.models.monitoring_active_alert import MonitoringActiveAlert
    from app.api.monitoring.services.alert_dispatcher import finalize_resolved_alert
    from app.api.monitoring.services.notifications import read_monitoring_notifications
    from app.api.alert_center import _read_alert_settings

    now = datetime.utcnow()
    db = SessionLocal()
    handled = 0
    try:
        rules_channels, _ = read_monitoring_notifications(db)
        channel_map = {c.get("id"): c for c in rules_channels}
        alert_settings = _read_alert_settings(db)
        rows = (
            db.query(MonitoringActiveAlert)
            .filter(MonitoringActiveAlert.state == "resolving")
            .all()
        )
        for row in rows:
            candidate = row.recovery_candidate_at
            if not candidate:
                continue
            # 防抖时长：优先全局设置，其次按规则覆盖（通过 labels 里 rule 关联较难，
            # 这里统一用全局 recovery_debounce_seconds，至少保证恢复不丢）。
            debounce = int(alert_settings.get("recovery_debounce_seconds", 60) or 0)
            if (now - candidate).total_seconds() < max(debounce, 0):
                continue
            try:
                labels = json.loads(row.labels) if row.labels else {}
            except Exception:
                labels = {}
            channel_ids = list(alert_settings.get("default_channel_ids") or [])
            finalize_resolved_alert(
                db,
                fingerprint=row.fingerprint,
                channel_ids=channel_ids,
                channel_map=channel_map,
                alert_settings=alert_settings,
                im_user_ids=[],
                labels=labels,
                resolved_at=now,
            )
            handled += 1
        return handled
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"恢复防抖兜底扫描失败: {exc}", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return handled
    finally:
        db.close()


def _load_dispatch_context():
    """阻塞:读取派发所需的规则/渠道/静默/策略(供线程池调用)。"""
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


async def start_alert_engine_runner() -> None:
    """常驻后台任务:周期评估 + 派发。随后端进程启停。"""
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "alert_engine",
        "告警引擎评估",
        "每 30 秒按监控规则评估告警 firing/resolved，并派发到通知渠道（含恢复防抖）",
        EVAL_INTERVAL_SECONDS,
    )
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info("自建告警引擎(嵌入式 runner,替代 vmalert)已启动。")
    while True:
        with track_run("alert_engine"):
            try:
                events = await asyncio.to_thread(_run_evaluate)
                if events:
                    rules, channels, silences, alert_settings = await asyncio.to_thread(_load_dispatch_context)
                    # dispatch 含同步 HTTP 发送(timeout=8s)+ DB 提交,放工作线程跑,
                    # 避免阻塞事件循环;其内部恢复防抖协程经 schedule_coroutine 安全调度回主循环。
                    await asyncio.to_thread(
                        dispatch_active_alerts,
                        events, rules, channels, silences, datetime.utcnow(), alert_settings,
                    )
                # 恢复防抖兜底：调度协程失败/进程重启后遗留的 resolving 告警，
                # 由本轮扫描补发恢复通知并归档，避免恢复通知被静默丢弃。
                await asyncio.to_thread(_scan_stale_resolving)
            except asyncio.CancelledError:
                logger.info("自建告警引擎 runner 收到取消信号,退出。")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"自建告警引擎 runner 异常: {exc}", exc_info=True)
        await asyncio.sleep(EVAL_INTERVAL_SECONDS)
