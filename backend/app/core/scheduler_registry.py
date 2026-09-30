"""后台定时任务定义、配置持久化适配和单进程运行时管理器。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterator

from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

SCHEDULER_CONFIG_KEY = "scheduled_task_config"

# 单轮执行租约 TTL 的下限（秒）：即使任务周期很短，也保证崩溃实例的
# 残留租约最多让任务停摆两分钟，避免 alert_engine 这类 30 秒级任务被卡死。
MIN_LEASE_TTL_SECONDS = 120

# 租约设施连续 fail-open（获取异常放行）达到该次数时升级为 error 日志：
# 偶发抖动 warning 即可，连续放行说明租约存储坏了，多 worker 会重复执行任务。
FAIL_OPEN_ERROR_THRESHOLD = 3


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    name: str
    description: str
    default_interval_seconds: int
    min_interval_seconds: int
    max_interval_seconds: int
    startup_delay_seconds: int = 0
    default_enabled: bool = True


TASK_DEFINITIONS: tuple[TaskDefinition, ...] = (
    TaskDefinition("assets_health_check", "主机健康巡检", "探测主机健康状态并刷新资产状态标签", 300, 60, 86400),
    TaskDefinition("application_health_snapshot", "应用健康快照", "聚合应用组件、主机和告警健康度", 300, 60, 86400),
    TaskDefinition("certificate_auto_renew", "证书自动续签", "扫描临近到期的自动续签证书并执行 ACME 重签", 43200, 3600, 7 * 86400),
    TaskDefinition("domain_expiry_sync", "域名到期同步", "刷新域名到期时间缓存", 86400, 3600, 7 * 86400, 60),
    TaskDefinition("alert_engine", "告警引擎评估", "评估规则并派发 firing/resolved 事件", 30, 10, 3600, 20),
    TaskDefinition("cert_expiry_alert", "证书到期告警", "扫描证书剩余有效期并派发告警", 21600, 600, 7 * 86400, 40),
    TaskDefinition("bill_sync", "云账单同步", "同步启用云账号的当月与上月账单", 21600, 3600, 7 * 86400),
    TaskDefinition("budget_alert", "预算告警扫描", "检查费用预算并派发超支/逼近告警", 3600, 60, 7 * 86400, 90),
    TaskDefinition("db_ticket_scheduler", "SQL 定时工单", "执行已批准且到期的定时工单", 30, 10, 3600, 45),
)
_DEFINITIONS = {item.key: item for item in TASK_DEFINITIONS}

_LOCK = threading.Lock()
_TASKS: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []


def _definition(key: str) -> TaskDefinition:
    try:
        return _DEFINITIONS[key]
    except KeyError as exc:
        raise ValueError(f"未知定时任务: {key}") from exc


def default_configs() -> dict[str, dict[str, Any]]:
    return {
        item.key: {"enabled": item.default_enabled, "interval_seconds": item.default_interval_seconds}
        for item in TASK_DEFINITIONS
    }


def normalize_configs(raw: Any) -> dict[str, dict[str, Any]]:
    configs = default_configs()
    if not isinstance(raw, dict):
        return configs
    for key, value in raw.items():
        if key not in _DEFINITIONS or not isinstance(value, dict):
            continue
        item = _DEFINITIONS[key]
        enabled = value.get("enabled", configs[key]["enabled"])
        interval = value.get("interval_seconds", configs[key]["interval_seconds"])
        if isinstance(enabled, bool) and isinstance(interval, int) and not isinstance(interval, bool):
            if item.min_interval_seconds <= interval <= item.max_interval_seconds:
                configs[key] = {"enabled": enabled, "interval_seconds": interval}
    return configs


def read_configs(db) -> tuple[dict[str, dict[str, Any]], str]:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == SCHEDULER_CONFIG_KEY).first()
    if not row or not row.config_value:
        return default_configs(), ""
    try:
        raw = json.loads(row.config_value)
    except (TypeError, ValueError):
        raw = {}
    return normalize_configs(raw), str(row.updated_at or "")


def validate_configs(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("定时任务配置必须是对象")
    unknown = [key for key in raw if key not in _DEFINITIONS]
    if unknown:
        raise ValueError(f"未知定时任务: {', '.join(unknown)}")
    configs = default_configs()
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"任务 {key} 配置必须是对象")
        enabled = value.get("enabled")
        interval = value.get("interval_seconds")
        if not isinstance(enabled, bool):
            raise ValueError(f"任务 {key} 的 enabled 必须是布尔值")
        if not isinstance(interval, int) or isinstance(interval, bool):
            raise ValueError(f"任务 {key} 的 interval_seconds 必须是整数")
        item = _DEFINITIONS[key]
        if not item.min_interval_seconds <= interval <= item.max_interval_seconds:
            raise ValueError(
                f"任务 {key} 周期必须在 {item.min_interval_seconds} 至 {item.max_interval_seconds} 秒之间"
            )
        configs[key] = {"enabled": enabled, "interval_seconds": interval}
    return configs


def persist_configs(db, configs: dict[str, dict[str, Any]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = json.dumps(normalize_configs(configs), ensure_ascii=False)
    row = db.query(SystemConfig).filter(SystemConfig.config_key == SCHEDULER_CONFIG_KEY).first()
    if row is None:
        db.add(SystemConfig(config_key=SCHEDULER_CONFIG_KEY, config_value=content, updated_at=now))
    else:
        row.config_value = content
        row.updated_at = now
    db.commit()
    return now


def register_task(key: str, name: str, description: str, interval_seconds: int) -> None:
    """兼容旧 runner 的注册入口，同时补齐可配置任务定义。"""
    with _LOCK:
        if key in _TASKS:
            _TASKS[key]["interval_seconds"] = int(interval_seconds)
            return
        _TASKS[key] = {
            "key": key, "name": name, "description": description,
            "interval_seconds": int(interval_seconds), "enabled": True,
            "started_at": "", "last_run_at": "", "last_status": "pending",
            "last_error": "", "last_duration_ms": None, "run_count": 0, "error_count": 0,
        }
        _ORDER.append(key)


def _ensure_state(item: TaskDefinition, config: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        state = _TASKS.get(item.key)
        if state is None:
            state = {
                "key": item.key, "name": item.name, "description": item.description,
                "interval_seconds": config["interval_seconds"], "enabled": config["enabled"],
                "default_interval_seconds": item.default_interval_seconds,
                "min_interval_seconds": item.min_interval_seconds,
                "max_interval_seconds": item.max_interval_seconds,
                "startup_delay_seconds": item.startup_delay_seconds,
                "started_at": "", "last_run_at": "", "last_status": "pending",
                "last_error": "", "last_duration_ms": None, "run_count": 0, "error_count": 0,
            }
            _TASKS[item.key] = state
            _ORDER.append(item.key)
        else:
            state["interval_seconds"] = config["interval_seconds"]
            state["enabled"] = config["enabled"]
        return state


def mark_run_start(key: str) -> None:
    with _LOCK:
        task = _TASKS.get(key)
        if task is not None and not task["started_at"]:
            task["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mark_run_end(key: str, ok: bool, error: str = "", duration_ms: int | None = None) -> None:
    with _LOCK:
        task = _TASKS.get(key)
        if task is None:
            return
        task["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task["last_status"] = "ok" if ok else "error"
        task["last_error"] = "" if ok else (error or "未知错误")[:500]
        task["last_duration_ms"] = duration_ms
        task["run_count"] = int(task["run_count"]) + 1
        if not ok:
            task["error_count"] = int(task["error_count"]) + 1


@contextmanager
def track_run(key: str) -> Iterator[None]:
    mark_run_start(key)
    started = time.monotonic()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mark_run_end(key, False, str(exc), int((time.monotonic() - started) * 1000))
        raise
    else:
        mark_run_end(key, True, duration_ms=int((time.monotonic() - started) * 1000))


class SchedulerManager:
    def __init__(self) -> None:
        self._handles: dict[str, asyncio.Task] = {}
        self._cycles: dict[str, Callable[[], Awaitable[None]]] = {}
        self._configs = default_configs()
        self._lock: asyncio.Lock | None = None
        self._wake = asyncio.Event()
        # 本进程的租约持有者标识，懒加载；多 worker 部署时各进程各不相同。
        self._lease_owner: str | None = None
        # 各任务连续 fail-open 计数：租约获取成功即清零，连续超阈值升 error。
        self._fail_open_counts: dict[str, int] = {}

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start_all(self, db) -> None:
        from app.core.scheduler_tasks import build_scheduler_cycles
        self._cycles = build_scheduler_cycles()
        self._configs, _ = read_configs(db)
        for item in TASK_DEFINITIONS:
            _ensure_state(item, self._configs[item.key])
        await self.reconcile(self._configs)

    async def reconcile(self, configs: dict[str, dict[str, Any]]) -> None:
        configs = validate_configs(configs)
        async with self._get_lock():
            self._configs = configs
            for item in TASK_DEFINITIONS:
                state = _ensure_state(item, configs[item.key])
                current = self._handles.get(item.key)
                should_run = configs[item.key]["enabled"]
                if should_run and (current is None or current.done()):
                    self._handles[item.key] = asyncio.create_task(self._run(item), name=f"opspilot-{item.key}")
                elif not should_run and current is not None:
                    current.cancel()
                    self._handles.pop(item.key, None)
                    state["runtime_state"] = "disabled"
            self._wake.set()
            self._wake = asyncio.Event()

    def _cycle_lease_owner(self) -> str:
        """返回本进程的租约持有者标识（懒加载，进程生命周期内不变）。"""
        if self._lease_owner is None:
            from app.services.leases import lease_owner

            self._lease_owner = lease_owner()
        return self._lease_owner

    def _acquire_cycle_lease(self, key: str, interval_seconds: int) -> bool:
        """每轮执行前获取跨进程租约，防止多 worker 同时跑同一任务。

        Args:
            key: 任务键。
            interval_seconds: 当前配置的执行周期，用于推导 TTL（下限 120 秒）。

        Returns:
            是否持有租约。租约设施自身异常（如表未建、库暂不可用）时放行
            （fail-open），避免租约故障放大成任务停摆；此时行为与接入前一致。
            连续 fail-open 达到 FAIL_OPEN_ERROR_THRESHOLD 次升级为 error 日志
            （多 worker 下会重复执行任务，必须足够显眼），恢复正常后计数清零。
        """
        from app.db.database import SessionLocal
        from app.services.leases import acquire_lease

        ttl_seconds = max(MIN_LEASE_TTL_SECONDS, 2 * int(interval_seconds))
        try:
            db = SessionLocal()
            try:
                acquired = acquire_lease(db, f"scheduler:{key}", self._cycle_lease_owner(), ttl_seconds)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            count = int(self._fail_open_counts.get(key, 0)) + 1
            self._fail_open_counts[key] = count
            message = "定时任务 %s 租约获取异常，本轮放行本进程执行（连续第 %s 次）: %s"
            if count >= FAIL_OPEN_ERROR_THRESHOLD:
                logger.error(message, key, count, exc)
            else:
                logger.warning(message, key, count, exc)
            return True
        # 租约设施恢复正常：清零连续 fail-open 计数。
        if self._fail_open_counts.pop(key, None) is not None:
            logger.info("定时任务 %s 租约恢复正常，fail-open 计数已清零", key)
        return acquired

    def _release_cycle_lease(self, key: str) -> None:
        """本轮执行结束后释放租约，下一轮（或接管实例）可立即获取。"""
        from app.db.database import SessionLocal
        from app.services.leases import release_lease

        try:
            db = SessionLocal()
            try:
                release_lease(db, f"scheduler:{key}", self._cycle_lease_owner())
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            # 释放失败只影响其他实例的接管时间（等 TTL 过期自动失效），不影响本轮结果。
            logger.warning("定时任务 %s 租约释放失败（将等待过期自动失效）: %s", key, exc)

    async def _run(self, item: TaskDefinition) -> None:
        key = item.key
        try:
            if item.startup_delay_seconds:
                await asyncio.sleep(item.startup_delay_seconds)
            while True:
                config = self._configs[key]
                if not config["enabled"]:
                    return
                try:
                    if self._acquire_cycle_lease(key, int(config["interval_seconds"])):
                        try:
                            with track_run(key):
                                await self._cycles[key]()
                        finally:
                            self._release_cycle_lease(key)
                    else:
                        # 其他进程正持有租约执行同一任务，本轮静默跳过（不计入 run_count）。
                        logger.debug("定时任务 %s 租约被其他实例持有，本进程跳过本轮", key)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # track_run 已记录本轮错误；runner 保持存活，等待下一轮重试。
                    pass
                try:
                    wake = self._wake
                    await asyncio.wait_for(wake.wait(), timeout=self._configs[key]["interval_seconds"])
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            with _LOCK:
                if key in _TASKS:
                    _TASKS[key]["runtime_state"] = "disabled" if not self._configs[key]["enabled"] else "stopped"
            raise

    async def apply(self, configs: dict[str, dict[str, Any]]) -> None:
        await self.reconcile(configs)

    async def shutdown(self) -> None:
        async with self._get_lock():
            handles = list(self._handles.values())
            self._handles.clear()
            for handle in handles:
                handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

    def snapshot(self) -> list[dict[str, Any]]:
        result = snapshot_tasks()
        live = self._handles
        for item in result:
            handle = live.get(item["key"])
            item["runtime_state"] = "running" if handle is not None and not handle.done() else (
                "disabled" if not item.get("enabled", True) else "stopped"
            )
        return result


scheduler_manager = SchedulerManager()


def snapshot_tasks() -> list[dict[str, Any]]:
    with _LOCK:
        result = []
        for key in _ORDER:
            task = dict(_TASKS[key])
            last_run = task.get("last_run_at") or ""
            if last_run and task.get("enabled", True):
                try:
                    timestamp = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S").timestamp() + int(task["interval_seconds"])
                    task["next_run_at"] = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    task["next_run_at"] = ""
            else:
                task["next_run_at"] = ""
            result.append(task)
        return result
