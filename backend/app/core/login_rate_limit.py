"""登录失败限流。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _LoginAttemptState:
    """单个登录来源的失败状态。"""

    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginRateLimiter:
    """基于内存的用户名与来源 IP 登录失败限流器。"""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        lock_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化限流器。

        Args:
            max_attempts: 窗口期内最大失败次数。
            window_seconds: 失败计数窗口。
            lock_seconds: 达到阈值后的锁定时长。
            clock: 测试可注入时间函数。
        """
        self._max_attempts = max(int(max_attempts), 1)
        self._window_seconds = max(int(window_seconds), 1)
        self._lock_seconds = max(int(lock_seconds), 1)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._states: dict[str, _LoginAttemptState] = {}

    def build_key(self, username: str, source_ip: str) -> str:
        """生成限流键，避免不同来源互相影响。"""
        safe_user = str(username or "").strip().lower() or "-"
        safe_ip = str(source_ip or "").strip() or "-"
        return f"{safe_user}@{safe_ip}"

    def check_allowed(self, key: str) -> tuple[bool, str]:
        """检查当前登录尝试是否允许继续。"""
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if not state:
                return True, ""
            if state.locked_until > now:
                remaining = int(state.locked_until - now)
                return False, f"登录失败次数过多，请 {remaining} 秒后再试"
            state.locked_until = 0.0
            state.failures = self._recent_failures(state.failures, now)
            if not state.failures:
                self._states.pop(key, None)
        return True, ""

    def record_failure(self, key: str) -> None:
        """记录一次失败，达到阈值后短期锁定。"""
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(key, _LoginAttemptState())
            state.failures = self._recent_failures(state.failures, now)
            state.failures.append(now)
            if len(state.failures) >= self._max_attempts:
                state.locked_until = now + self._lock_seconds

    def record_success(self, key: str) -> None:
        """登录成功后清理失败记录。"""
        with self._lock:
            self._states.pop(key, None)

    def _recent_failures(self, failures: list[float], now: float) -> list[float]:
        """保留窗口期内失败记录。"""
        lower_bound = now - self._window_seconds
        return [item for item in failures if item >= lower_bound]
