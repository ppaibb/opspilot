"""K8s Pod logs/exec one-shot WebSocket session store."""

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

@dataclass
class K8sWebSocketSessionContext:
    """K8s Pod logs/exec WebSocket 一次性会话上下文。"""

    session_id: str
    cluster_id: str
    namespace: str
    pod_name: str
    action: str
    created_by: str
    container: str = ""
    tail: int = 100
    command: str = "/bin/sh"
    previous: bool = False

@dataclass
class _K8sWebSocketSessionRecord:
    """K8s WebSocket 会话内存记录，仅短期有效。"""

    context: K8sWebSocketSessionContext
    token_hash: str
    expires_at_ts: float
    consumed: bool = False

_K8S_WS_SESSION_TTL_SECONDS = 90

_K8S_WS_SESSION_LOCK = threading.Lock()

_K8S_WS_SESSION_STORE: dict[str, _K8sWebSocketSessionRecord] = {}

def _hash_k8s_ws_token(raw_token: str) -> str:
    """对 K8s WebSocket 会话 token 做摘要，避免在内存记录中保留明文。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def _cleanup_expired_k8s_ws_sessions_locked(now_ts: float) -> None:
    """清理过期 K8s WebSocket 会话。"""
    expired_ids = [
        session_id
        for session_id, record in _K8S_WS_SESSION_STORE.items()
        if record.expires_at_ts <= now_ts
    ]
    for session_id in expired_ids:
        _K8S_WS_SESSION_STORE.pop(session_id, None)

def _create_k8s_ws_session(
    *,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    action: str,
    created_by: str,
    container: str = "",
    tail: int = 100,
    command: str = "/bin/sh",
    previous: bool = False,
) -> tuple[str, str, int]:
    """创建 K8s Pod logs/exec WebSocket 一次性会话。"""
    safe_action = str(action or "").strip().lower()
    if safe_action not in {"logs", "exec"}:
        raise HTTPException(status_code=400, detail="不支持的 K8s WebSocket 会话类型")
    safe_tail = min(max(int(tail or 100), 1), 1000)
    session_id = f"k8s-ws-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    now_ts = time.time()
    expire_ts = now_ts + _K8S_WS_SESSION_TTL_SECONDS
    record = _K8sWebSocketSessionRecord(
        context=K8sWebSocketSessionContext(
            session_id=session_id,
            cluster_id=str(cluster_id or "").strip(),
            namespace=str(namespace or "").strip(),
            pod_name=str(pod_name or "").strip(),
            action=safe_action,
            created_by=str(created_by or "未知用户").strip() or "未知用户",
            container=str(container or "").strip(),
            tail=safe_tail,
            command=str(command or "/bin/sh").strip() or "/bin/sh",
            previous=bool(previous),
        ),
        token_hash=_hash_k8s_ws_token(token),
        expires_at_ts=expire_ts,
    )
    with _K8S_WS_SESSION_LOCK:
        _cleanup_expired_k8s_ws_sessions_locked(now_ts)
        _K8S_WS_SESSION_STORE[session_id] = record
    return session_id, token, int(expire_ts * 1000)

def _consume_k8s_ws_session(
    session_id: str,
    token: str,
    *,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    action: str,
) -> tuple[bool, str, K8sWebSocketSessionContext | None]:
    """消费一次性 K8s WebSocket 会话，并校验其绑定目标。"""
    sid = str(session_id or "").strip()
    raw_token = str(token or "").strip()
    safe_action = str(action or "").strip().lower()
    if not sid or not raw_token:
        return False, "会话参数不完整", None

    now_ts = time.time()
    with _K8S_WS_SESSION_LOCK:
        _cleanup_expired_k8s_ws_sessions_locked(now_ts)
        record = _K8S_WS_SESSION_STORE.get(sid)
        if not record:
            return False, "会话不存在或已过期", None
        if record.consumed:
            _K8S_WS_SESSION_STORE.pop(sid, None)
            return False, "会话已被使用", None
        if record.expires_at_ts <= now_ts:
            _K8S_WS_SESSION_STORE.pop(sid, None)
            return False, "会话已过期", None
        if not hmac.compare_digest(record.token_hash, _hash_k8s_ws_token(raw_token)):
            return False, "会话验证失败", None

        context = record.context
        bound_target_matches = (
            context.cluster_id == str(cluster_id or "").strip()
            and context.namespace == str(namespace or "").strip()
            and context.pod_name == str(pod_name or "").strip()
            and context.action == safe_action
        )
        if not bound_target_matches:
            return False, "会话目标不匹配", None

        record.consumed = True
        _K8S_WS_SESSION_STORE.pop(sid, None)
        return True, "ok", context

