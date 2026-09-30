"""
K8s Watch SSE 端点 — 实时推送资源变更事件

认证方式：复用现有 WebSocket session token 模式（EventSource 不支持自定义 Header）。
安全边界：
1. 签发入口（POST /watch/session）要求登录 + 集群模块权限；
2. session token 与签发用户、资源 kinds、namespace 绑定；
3. 消费时（GET /watch）校验 TTL、集群一致、kinds ⊆ 签发范围、namespace 一致，
   token 一次性（原子弹出），防重放与越权扩大订阅范围。
"""
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse

from app.services.k8s_informer import InformerManager, WATCHED_KINDS
from app.core.security import require_permissions
from app.models.user import User

router = APIRouter()

# 短期 session token 存储（复用 WebSocket 的模式）
_watch_sessions: dict[str, dict] = {}
_WATCH_SESSION_TTL = 90


@router.post("/{cluster_id}/watch/session")
def create_watch_session(
    cluster_id: str,
    kinds: str = Query("", description="Comma-separated kinds to limit, empty = all watched kinds"),
    namespace: str = Query("", description="Limit to one namespace, empty = all namespaces"),
    current_user: User = Depends(require_permissions("module.assets.clusters")),
):
    """创建 SSE watch 的一次性 session token。

    安全边界：token 本身随 URL 传输（SSE 无法带 header），所以签发入口必须
    先过登录与集群模块权限校验；token 一次性且 90 秒过期，并与签发用户、
    kinds、namespace 绑定，消费端会复核订阅范围不得超出签发范围。
    """
    token = str(uuid.uuid4())
    requested_kinds = [k.strip() for k in (kinds or "").split(",") if k.strip()]
    _watch_sessions[token] = {
        "cluster_id": cluster_id,
        "created_at": time.time(),
        "user_id": current_user.id,
        "kinds": requested_kinds,
        "namespace": (namespace or "").strip(),
    }
    # 清理过期 token
    now = time.time()
    expired = [k for k, v in _watch_sessions.items() if now - v["created_at"] > _WATCH_SESSION_TTL]
    for k in expired:
        _watch_sessions.pop(k, None)
    return {"token": token, "expires_at": int(now + _WATCH_SESSION_TTL)}


def _consume_watch_session(token: str, cluster_id: str, kinds: str, namespace: str) -> str:
    """原子消费 watch session token，并复核订阅范围。

    Returns:
        "ok" 表示通过；其余返回失败原因：
        missing / expired / cluster_mismatch → 应映射 401（票据本身无效）；
        kinds_out_of_scope / namespace_mismatch → 应映射 403（越权扩大范围）。
    """
    session = _watch_sessions.pop(token, None)
    if not session:
        return "missing"
    if time.time() - session["created_at"] > _WATCH_SESSION_TTL:
        return "expired"
    if session["cluster_id"] != cluster_id:
        return "cluster_mismatch"

    issued_kinds = {str(k) for k in (session.get("kinds") or [])}
    requested_kinds = {k.strip() for k in (kinds or "").split(",") if k.strip()}
    # 签发为空 = 不限 kinds；否则请求的 kinds 必须是签发范围的子集。
    if issued_kinds and not requested_kinds.issubset(issued_kinds):
        return "kinds_out_of_scope"

    issued_namespace = str(session.get("namespace") or "").strip()
    requested_namespace = str(namespace or "").strip()
    # 签发为空 = 不限 namespace；否则必须与签发一致。
    if issued_namespace and requested_namespace != issued_namespace:
        return "namespace_mismatch"
    return "ok"


@router.get("/{cluster_id}/watch")
async def watch_resources(
    cluster_id: str,
    token: str = Query(..., description="Session token from POST /watch/session"),
    kinds: str = Query(..., description="Comma-separated: Pod,Deployment"),
    namespace: str = Query("", description="Empty = all namespaces"),
):
    """SSE 端点：实时推送 K8s 资源变更事件。"""
    reason = _consume_watch_session(token, cluster_id, kinds, namespace)
    if reason in ("missing", "expired", "cluster_mismatch"):
        raise HTTPException(status_code=401, detail="Invalid or expired watch session token")
    if reason == "kinds_out_of_scope":
        raise HTTPException(status_code=403, detail="Requested kinds exceed the granted watch scope")
    if reason == "namespace_mismatch":
        raise HTTPException(status_code=403, detail="Requested namespace does not match the granted watch scope")

    requested_kinds = {k.strip() for k in kinds.split(",") if k.strip()}
    invalid = requested_kinds - set(WATCHED_KINDS.keys())
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unsupported kinds: {invalid}")
    if not requested_kinds:
        raise HTTPException(status_code=400, detail="At least one kind required")

    manager = InformerManager.get_instance()
    subscriber_id, queue, snapshot = await manager.subscribe(cluster_id, requested_kinds, namespace)

    async def event_stream():
        try:
            yield f"event: snapshot\ndata: {json.dumps(snapshot, default=str)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    payload = {
                        "type": event.event_type,
                        "kind": event.kind,
                        "namespace": event.namespace,
                        "name": event.name,
                        "resourceVersion": event.resource_version,
                        "object": event.object_summary,
                    }
                    yield f"event: watch\ndata: {json.dumps(payload, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await manager.unsubscribe(cluster_id, subscriber_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
