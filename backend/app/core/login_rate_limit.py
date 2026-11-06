"""登录失败限流。"""
from __future__ import annotations

import threading
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from app.models.refresh_token import LoginAttempt


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


class DatabaseLoginRateLimiter:
    """数据库持久化限流器，多 worker/多实例共享失败与锁定状态。

    数据库暂时不可用时回退到进程内限流并记录告警，避免认证服务因限流表
    故障完全停摆；数据库恢复后，正常请求会继续使用持久化状态。
    """

    def __init__(self, *, max_attempts: int, window_seconds: int, lock_seconds: int) -> None:
        self._max_attempts = max(int(max_attempts), 1)
        self._window_seconds = max(int(window_seconds), 1)
        self._lock_seconds = max(int(lock_seconds), 1)
        self._fallback = LoginRateLimiter(
            max_attempts=self._max_attempts,
            window_seconds=self._window_seconds,
            lock_seconds=self._lock_seconds,
        )

    def build_key(self, username: str, source_ip: str) -> str:
        return self._fallback.build_key(username, source_ip)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _format(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _identity(key: str) -> tuple[str, str]:
        username, _, source_ip = key.rpartition("@")
        return username, source_ip

    @staticmethod
    def _rollback(db: Session) -> None:
        """测试替身或异常连接可能没有可用 rollback，回退路径不得二次失败。"""
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            rollback()

    def check_allowed(self, key: str, *, db: Session) -> tuple[bool, str]:
        now = self._now()
        try:
            row = db.query(LoginAttempt).filter(LoginAttempt.key_hash == self._key_hash(key)).first()
            if not row:
                return True, ""
            if row.locked_until:
                locked_until = datetime.fromisoformat(row.locked_until)
                if locked_until > now:
                    seconds = max(int((locked_until - now).total_seconds()), 1)
                    return False, f"登录失败次数过多，请 {seconds} 秒后再试"
            started = datetime.fromisoformat(row.window_started_at)
            if started + timedelta(seconds=self._window_seconds) <= now:
                db.delete(row)
                db.commit()
            return True, ""
        except Exception as exc:  # 限流存储故障不能让全部用户无法登录
            self._rollback(db)
            logging.getLogger(__name__).warning("登录限流数据库不可用，回退进程内状态: %s", exc)
            return self._fallback.check_allowed(key)

    def record_failure(self, key: str, *, db: Session) -> None:
        now = self._now()
        try:
            digest = self._key_hash(key)
            row = (
                db.query(LoginAttempt)
                .filter(LoginAttempt.key_hash == digest)
                .with_for_update()
                .first()
            )
            if not row:
                username, source_ip = self._identity(key)
                row = LoginAttempt(
                    key_hash=digest,
                    username=username,
                    source_ip=source_ip,
                    failure_count=0,
                    window_started_at=self._format(now),
                    updated_at=self._format(now),
                )
                db.add(row)
            elif datetime.fromisoformat(row.window_started_at) + timedelta(
                seconds=self._window_seconds
            ) <= now:
                row.failure_count = 0
                row.window_started_at = self._format(now)
                row.locked_until = None
            row.failure_count += 1
            row.updated_at = self._format(now)
            if row.failure_count >= self._max_attempts:
                row.locked_until = self._format(now + timedelta(seconds=self._lock_seconds))
            db.commit()
        except Exception as exc:
            self._rollback(db)
            logging.getLogger(__name__).warning("登录失败状态写库失败，回退进程内状态: %s", exc)
            self._fallback.record_failure(key)

    def record_success(self, key: str, *, db: Session) -> None:
        try:
            db.query(LoginAttempt).filter(
                LoginAttempt.key_hash == self._key_hash(key)
            ).delete(synchronize_session=False)
            db.commit()
        except Exception as exc:
            self._rollback(db)
            logging.getLogger(__name__).warning("登录限流状态清理失败: %s", exc)
        self._fallback.record_success(key)
