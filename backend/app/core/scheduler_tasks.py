"""定时任务单轮执行适配器。

这里仅组合现有业务函数，不复制业务逻辑；SchedulerManager 统一负责周期、启停与错误心跳。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable


async def _assets_health() -> None:
    from app.api.assets import run_assets_health_check_cycle
    await asyncio.to_thread(run_assets_health_check_cycle)


async def _application_health() -> None:
    from app.services.application_health import run_application_health_snapshot_cycle
    await asyncio.to_thread(run_application_health_snapshot_cycle)


async def _certificate_renew() -> None:
    from app.api.certificates import run_certificates_auto_renew_cycle
    await asyncio.to_thread(run_certificates_auto_renew_cycle)


async def _domain_expiry() -> None:
    from app.api.domains import run_domain_expiry_sync_cycle
    await asyncio.to_thread(run_domain_expiry_sync_cycle)


async def _alert_engine() -> None:
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
    from app.services.alert_engine.runner import _load_dispatch_context, _run_evaluate

    events = await asyncio.to_thread(_run_evaluate)
    if events:
        rules, channels, silences, settings = await asyncio.to_thread(_load_dispatch_context)
        await asyncio.to_thread(dispatch_active_alerts, events, rules, channels, silences, datetime.now(), settings)


async def _certificate_expiry_alert() -> None:
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
    from app.services.alert_engine.cert_expiry_alert import _load_dispatch_context, _scan_and_collect

    events = await asyncio.to_thread(_scan_and_collect)
    if events:
        rules, channels, silences, settings = await asyncio.to_thread(_load_dispatch_context)
        await asyncio.to_thread(dispatch_active_alerts, events, rules, channels, silences, datetime.now(), settings)


async def _bill_sync() -> None:
    from app.services.bill_sync import _sync_all_enabled_accounts
    await asyncio.to_thread(_sync_all_enabled_accounts)


async def _budget_alert() -> None:
    from app.api.monitoring.services.alert_dispatcher import dispatch_active_alerts
    from app.services.budget_alert import _build_budget_event, _load_dispatch_context

    event = await asyncio.to_thread(_build_budget_event)
    if event is not None:
        rules, channels, silences, settings = await asyncio.to_thread(_load_dispatch_context)
        await asyncio.to_thread(dispatch_active_alerts, [event], rules, channels, silences, datetime.now(), settings)


async def _db_tickets() -> None:
    from app.api.database import _run_due_tickets
    await asyncio.to_thread(_run_due_tickets)


def build_scheduler_cycles() -> dict[str, Callable[[], Awaitable[None]]]:
    return {
        "assets_health_check": _assets_health,
        "application_health_snapshot": _application_health,
        "certificate_auto_renew": _certificate_renew,
        "domain_expiry_sync": _domain_expiry,
        "alert_engine": _alert_engine,
        "cert_expiry_alert": _certificate_expiry_alert,
        "bill_sync": _bill_sync,
        "budget_alert": _budget_alert,
        "db_ticket_scheduler": _db_tickets,
    }
