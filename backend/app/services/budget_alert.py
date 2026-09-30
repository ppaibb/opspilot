"""预算告警扫描器（方案A · 后台扫表，与证书到期告警同一模式）。

每个周期聚合当月云账单总额，与预算配置（system_configs.finance_budget_config）
对比：
- 达到 critical 阈值 → severity=critical 的 firing 事件
- 达到 warn 阈值 → severity=warning 的 firing 事件
- 回落阈值以下 → resolved 事件（由 dispatch 关闭历史 firing）

复用自建告警引擎 dispatch_active_alerts 管道（静默/路由/防抖/active_alerts
落库/飞书等渠道推送），不重复造通知轮子。随后端进程启停。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import func

from app.db.database import SessionLocal

logger = logging.getLogger(__name__)

# 预算检查每 1 小时一次；账单本身 6 小时同步一轮，粒度足够。
SCAN_INTERVAL_SECONDS = 3600
STARTUP_DELAY_SECONDS = 90

# 合成规则 id/名称：让 dispatch 能按规则匹配通道，并在前端按 opspilot_rule_id 归类。
BUDGET_RULE_ID = "builtin-finance-budget"
BUDGET_RULE_NAME = "云费用预算超支"


def _collect_budget_state() -> dict | None:
    """读取预算配置 + 当月已发生费用；未启用或无预算时返回 None。"""
    import json

    from app.models.finance import CloudBill
    from app.models.system_config import SystemConfig
    from app.services.bill_sync import month_cycle

    db = SessionLocal()
    try:
        row = (
            db.query(SystemConfig)
            .filter(SystemConfig.config_key == "finance_budget_config")
            .first()
        )
        if not row or not row.config_value:
            return None
        try:
            config = json.loads(row.config_value)
        except Exception:  # noqa: BLE001
            return None
        if not config.get("enabled"):
            return None
        budget = float(config.get("monthly_budget") or 0.0)
        if budget <= 0:
            return None

        month = month_cycle()
        spent = round(
            float(
                db.query(func.sum(CloudBill.pretax_amount))
                .filter(CloudBill.billing_cycle == month)
                .scalar()
                or 0.0
            ),
            2,
        )
        return {
            "month": month,
            "budget": budget,
            "spent": spent,
            "ratio": spent / budget,
            "warn_ratio": float(config.get("warn_ratio") or 0.8),
            "critical_ratio": float(config.get("critical_ratio") or 1.0),
        }
    finally:
        db.close()


def _build_budget_event() -> dict | None:
    """把预算状态转换为 firing/resolved 告警事件（状态回落时发 resolved 关闭历史告警）。"""
    from app.services.alert_engine.engine import _build_alert_event

    state = _collect_budget_state()
    if state is None:
        return None

    ratio = state["ratio"]
    if ratio >= state["critical_ratio"]:
        severity, firing = "critical", True
    elif ratio >= state["warn_ratio"]:
        severity, firing = "warning", True
    else:
        severity, firing = "warning", False

    percent = round(ratio * 100, 1)
    if ratio >= state["critical_ratio"]:
        summary = f"云费用预算已超支：本月 ¥{state['spent']}（预算 ¥{state['budget']}，{percent}%）"
        desc = (
            f"账期 {state['month']} 累计消费 ¥{state['spent']}，已达预算 ¥{state['budget']} 的 {percent}%。"
            "请前往费用管理查看明细并评估是否调整预算或清理资源。"
        )
    elif ratio >= state["warn_ratio"]:
        summary = f"云费用接近预算：本月 ¥{state['spent']}（{percent}%）"
        desc = (
            f"账期 {state['month']} 累计消费 ¥{state['spent']}，已达预算 ¥{state['budget']} 的 {percent}%"
            f"（告警阈值 {round(state['warn_ratio'] * 100)}%）。"
        )
    else:
        summary = f"云费用预算正常：本月 ¥{state['spent']}（{percent}%）"
        desc = ""

    labels = {
        "instance": "finance-budget",
        "month": state["month"],
        "budget": f"{state['budget']:.2f}",
        "spent": f"{state['spent']:.2f}",
    }
    return _build_alert_event(
        rule_id=BUDGET_RULE_ID,
        name=BUDGET_RULE_NAME,
        severity=severity,
        labels=labels,
        value=round(ratio * 100, 2),
        summary_tpl=summary,
        desc_tpl=desc,
        status="firing" if firing else "resolved",
        active_at=datetime.now(),
    )


def _load_dispatch_context():
    """复用告警引擎的派发上下文加载（规则/渠道/静默/策略）。"""
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


async def start_budget_alert_runner() -> None:
    """常驻后台任务：周期检查预算使用率并经告警管道派发。随后端进程启停。"""
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "budget_alert",
        "预算告警扫描",
        "每 1 小时对比当月费用与预算，超支/逼近阈值时经告警管道派发，回落自动 resolved",
        SCAN_INTERVAL_SECONDS,
    )
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info("预算告警扫描器已启动（每 %s 秒检查一次）。", SCAN_INTERVAL_SECONDS)
    while True:
        with track_run("budget_alert"):
            try:
                event = await asyncio.to_thread(_build_budget_event)
                if event is not None:
                    rules, channels, silences, alert_settings = await asyncio.to_thread(_load_dispatch_context)
                    await asyncio.to_thread(
                        dispatch_active_alerts,
                        [event], rules, channels, silences, datetime.now(), alert_settings,
                    )
            except asyncio.CancelledError:
                logger.info("预算告警扫描器收到取消信号，退出。")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("预算告警扫描器异常: %s", exc, exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
