"""
Web 终端会话服务：
负责签发一次性短期会话令牌，并在 WebSocket 握手时校验消费。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid


@dataclass
class TerminalSessionContext:
    """WebSocket 握手后用于建立 SSH 会话的上下文。"""

    session_id: str
    asset_id: str
    asset_name: str
    ip: str
    port: int
    user: str
    password: str
    pkey_content: str
    created_by: str
    cols: int
    rows: int


@dataclass
class _TerminalSessionRecord:
    """内存态会话记录，仅在短时间窗口内有效。"""

    context: TerminalSessionContext
    token_hash: str
    expires_at_ts: float
    consumed: bool = False


_SESSION_TTL_SECONDS = 90
_SESSION_LOCK = threading.Lock()
_SESSION_STORE: dict[str, _TerminalSessionRecord] = {}

# 终端会话运行态治理参数（可通过环境变量覆盖）
_MAX_ACTIVE_SESSIONS_PER_USER = max(int(os.getenv("TERMINAL_MAX_ACTIVE_PER_USER", "5")), 1)
_MAX_SESSION_DURATION_SECONDS = max(int(os.getenv("TERMINAL_MAX_DURATION_SECONDS", "14400")), 300)
_IDLE_TIMEOUT_SECONDS = max(int(os.getenv("TERMINAL_IDLE_TIMEOUT_SECONDS", "900")), 60)


@dataclass
class ActiveTerminalSession:
    """在线终端会话信息，用于并发限制与会话时长审计。"""

    session_id: str
    user: str
    asset_id: str
    ip: str
    port: int
    opened_at_ts: float
    last_active_ts: float


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SESSIONS: dict[str, ActiveTerminalSession] = {}


def terminal_runtime_limits() -> dict[str, int]:
    """返回终端运行时限制，统一供 WebSocket 层读取。"""
    return {
        "max_active_per_user": _MAX_ACTIVE_SESSIONS_PER_USER,
        "max_duration_seconds": _MAX_SESSION_DURATION_SECONDS,
        "idle_timeout_seconds": _IDLE_TIMEOUT_SECONDS,
    }


def register_active_terminal_session(
    *,
    session_id: str,
    user: str,
    asset_id: str,
    ip: str,
    port: int,
) -> tuple[bool, str]:
    """
    注册在线终端会话，执行单用户并发上限检查。

    Returns:
        (True, "ok"): 注册成功
        (False, reason): 注册失败（例如并发超限）
    """
    sid = (session_id or "").strip()
    if not sid:
        return False, "会话ID无效"
    now_ts = time.time()
    owner = (user or "未知用户").strip() or "未知用户"
    with _ACTIVE_LOCK:
        user_count = 0
        expired_session_ids: list[str] = []
        for active_sid, record in _ACTIVE_SESSIONS.items():
            if now_ts - record.opened_at_ts > _MAX_SESSION_DURATION_SECONDS * 2:
                expired_session_ids.append(active_sid)
                continue
            if record.user == owner:
                user_count += 1
        for expired_sid in expired_session_ids:
            _ACTIVE_SESSIONS.pop(expired_sid, None)

        if user_count >= _MAX_ACTIVE_SESSIONS_PER_USER:
            return False, f"并发终端会话超限（最多 {_MAX_ACTIVE_SESSIONS_PER_USER} 个）"

        _ACTIVE_SESSIONS[sid] = ActiveTerminalSession(
            session_id=sid,
            user=owner,
            asset_id=(asset_id or "").strip(),
            ip=(ip or "").strip(),
            port=int(port or 22),
            opened_at_ts=now_ts,
            last_active_ts=now_ts,
        )
        return True, "ok"


def touch_active_terminal_session(session_id: str) -> None:
    """更新会话活跃时间戳，用于空闲超时控制。"""
    sid = (session_id or "").strip()
    if not sid:
        return
    now_ts = time.time()
    with _ACTIVE_LOCK:
        record = _ACTIVE_SESSIONS.get(sid)
        if record:
            record.last_active_ts = now_ts


def pop_active_terminal_session(session_id: str) -> dict[str, float | str]:
    """
    移除在线会话并返回摘要信息。

    返回字段：
    - user / asset_id / ip / port
    - duration_seconds（总时长）
    - idle_seconds（最后一次活动至移除时的空闲时长）
    """
    sid = (session_id or "").strip()
    now_ts = time.time()
    with _ACTIVE_LOCK:
        record = _ACTIVE_SESSIONS.pop(sid, None)
    if not record:
        return {
            "user": "",
            "asset_id": "",
            "ip": "",
            "port": 0,
            "duration_seconds": 0.0,
            "idle_seconds": 0.0,
        }
    return {
        "user": record.user,
        "asset_id": record.asset_id,
        "ip": record.ip,
        "port": record.port,
        "duration_seconds": max(now_ts - record.opened_at_ts, 0.0),
        "idle_seconds": max(now_ts - record.last_active_ts, 0.0),
    }


def _hash_token(token: str) -> str:
    """使用 SHA256 对会话 token 做摘要，避免明文 token 驻留内存。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cleanup_expired_locked(now_ts: float) -> None:
    """清理过期会话，避免内存累积。"""
    expired_keys = [
        sid for sid, record in _SESSION_STORE.items() if record.expires_at_ts <= now_ts
    ]
    for sid in expired_keys:
        _SESSION_STORE.pop(sid, None)


def create_terminal_session(
    *,
    asset_id: str,
    asset_name: str,
    ip: str,
    port: int,
    user: str,
    password: str,
    pkey_content: str,
    created_by: str,
    cols: int = 120,
    rows: int = 36,
) -> tuple[str, str, int]:
    """
    创建一次性终端会话。

    Returns:
        session_id: 会话ID
        token: 会话明文 token（仅返回给调用方，不入库存储）
        expire_epoch_ms: 过期时间戳（毫秒）
    """
    safe_cols = min(max(int(cols or 120), 60), 300)
    safe_rows = min(max(int(rows or 36), 20), 120)
    session_id = f"term-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    now_ts = time.time()
    expire_ts = now_ts + _SESSION_TTL_SECONDS
    record = _TerminalSessionRecord(
        context=TerminalSessionContext(
            session_id=session_id,
            asset_id=asset_id,
            asset_name=asset_name,
            ip=ip,
            port=int(port),
            user=user,
            password=password,
            pkey_content=pkey_content,
            created_by=created_by or "未知用户",
            cols=safe_cols,
            rows=safe_rows,
        ),
        token_hash=_hash_token(token),
        expires_at_ts=expire_ts,
    )

    with _SESSION_LOCK:
        _cleanup_expired_locked(now_ts)
        _SESSION_STORE[session_id] = record

    return session_id, token, int(expire_ts * 1000)


def consume_terminal_session(session_id: str, token: str) -> tuple[bool, str, TerminalSessionContext | None]:
    """
    消费一次性会话。

    注意：
    1. 会话仅允许消费一次，防止 token 重放；
    2. 校验失败时不给出过多细节，避免泄露内部状态。
    """
    sid = (session_id or "").strip()
    raw_token = (token or "").strip()
    if not sid or not raw_token:
        return False, "会话参数不完整", None

    now_ts = time.time()
    with _SESSION_LOCK:
        _cleanup_expired_locked(now_ts)
        record = _SESSION_STORE.get(sid)
        if not record:
            return False, "会话不存在或已过期", None
        if record.consumed:
            _SESSION_STORE.pop(sid, None)
            return False, "会话已被使用", None
        if record.expires_at_ts <= now_ts:
            _SESSION_STORE.pop(sid, None)
            return False, "会话已过期", None

        if not hmac.compare_digest(record.token_hash, _hash_token(raw_token)):
            return False, "会话验证失败", None

        # 一次性会话：验证通过后立即从内存移除，阻断复用。
        record.consumed = True
        context = record.context
        _SESSION_STORE.pop(sid, None)
        return True, "ok", context
