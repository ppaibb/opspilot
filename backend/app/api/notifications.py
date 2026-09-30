"""用户站内通知 API。

面向登录用户的通知收件箱：列表、未读数、标记已读、删除、清空、SSE 实时推送。
前端铃铛通知中心的数据源，与告警审计日志（monitoring_notification_log）完全独立。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.core.config import settings
from app.core.security import (
    SSE_TICKET_SCOPE,
    _check_token_version,
    get_current_user,
    normalize_user_status,
)
from app.db.database import get_db
from app.models.notification import UserNotification
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


# ── SSE Pub/Sub ───────────────────────────────────────────────────────────────
# 进程内 asyncio.Queue 广播：user_id → set[Queue]
# create_notification() 写库后调用 publish_notification()，SSE 端点消费 Queue 推给前端。

_sse_subscribers: dict[int, set[asyncio.Queue]] = {}


def _subscribe(user_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _sse_subscribers.setdefault(user_id, set()).add(q)
    return q


def _unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    subs = _sse_subscribers.get(user_id)
    if subs:
        subs.discard(q)
        if not subs:
            _sse_subscribers.pop(user_id, None)


def publish_notification(user_id: int, payload: dict) -> None:
    """写库后广播给该用户的所有 SSE 连接（同步调用，内部 fire-and-forget）。"""
    subs = _sse_subscribers.get(user_id)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


# ── Pydantic Schemas ──────────────────────────────────────────────────────────


class NotificationOut(BaseModel):
    id: str
    type: str
    category: str
    title: str
    content: str
    link: Optional[str] = None
    resource_id: Optional[str] = None
    is_read: bool
    created_at: str

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    items: list[NotificationOut]
    total: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    unread_count: int


class NotificationCreate(BaseModel):
    """供后端其他模块调用的创建 schema（不暴露为 API，仅内部使用）。"""
    user_id: int
    type: str = Field(default="info", pattern=r"^(success|error|warning|info)$")
    category: str = Field(default="system", max_length=50)
    title: str = Field(max_length=200)
    content: str = ""
    link: Optional[str] = None
    resource_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_out(n: UserNotification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        type=n.type,
        category=n.category,
        title=n.title,
        content=n.content or "",
        link=n.link,
        resource_id=n.resource_id,
        is_read=n.is_read,
        created_at=n.created_at.strftime("%Y-%m-%d %H:%M:%S") if n.created_at else "",
    )


def create_notification(db: Session, *, data: NotificationCreate) -> UserNotification:
    """后端内部调用：往 user_notifications 写一条记录，并广播给 SSE 连接。

    典型用法（在异步任务结束时调用）::

        from app.api.notifications import create_notification, NotificationCreate
        create_notification(db, data=NotificationCreate(
            user_id=user.id,
            type="success",
            category="certificate",
            title="证书续期完成",
            content="example.com 证书已续期至 2026-10-24",
            link="/assets/certificates",
            resource_id="cert_123",
        ))
    """
    n = UserNotification(
        user_id=data.user_id,
        type=data.type,
        category=data.category,
        title=data.title,
        content=data.content,
        link=data.link,
        resource_id=data.resource_id,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    # 广播给 SSE 连接（fire-and-forget）
    try:
        publish_notification(data.user_id, _to_out(n).model_dump())
    except Exception:
        logger.debug("SSE publish skipped (no subscribers or error)")
    # 飞书群 @ 通知（个人 IM 渠道，best-effort：失败只记日志，绝不影响站内通知）
    try:
        from app.services.im_feishu import fanout_user_notification

        fanout_user_notification(
            db,
            user_id=n.user_id,
            title=n.title,
            content=n.content or "",
            link=n.link or "",
        )
    except Exception:
        logger.debug("feishu fanout skipped (no binding or send error)")
    return n


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    is_read: Optional[bool] = Query(None, description="筛选未读/已读"),
    # 分类/关键词用普通默认值而非 Query()：本仓库测试直接调用路由函数，Query 默认值会漏进来
    category: str = "",
    q: str = "",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取当前用户的通知列表，附带 total 和 unread_count。"""
    q_obj = db.query(UserNotification).filter(UserNotification.user_id == current_user.id)
    if is_read is not None:
        q_obj = q_obj.filter(UserNotification.is_read == is_read)
    if category:
        q_obj = q_obj.filter(UserNotification.category == category)
    keyword = (q or "").strip()
    if keyword:
        # 转义 LIKE 通配符，避免用户输入 %/_ 时匹配到无关通知
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        q_obj = q_obj.filter(
            or_(
                UserNotification.title.like(like, escape="\\"),
                UserNotification.content.like(like, escape="\\"),
            )
        )

    total = q_obj.count()
    unread_count = (
        db.query(func.count())
        .select_from(UserNotification)
        .filter(UserNotification.user_id == current_user.id, UserNotification.is_read.is_(False))
        .scalar()
    )
    items = q_obj.order_by(UserNotification.created_at.desc()).offset(offset).limit(limit).all()
    return NotificationListResponse(
        items=[_to_out(n) for n in items],
        total=total,
        unread_count=unread_count,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """轻量轮询接口：仅返回未读数量，前端 30~60s 调一次刷新角标。"""
    count = (
        db.query(func.count())
        .select_from(UserNotification)
        .filter(UserNotification.user_id == current_user.id, UserNotification.is_read.is_(False))
        .scalar()
    )
    return UnreadCountResponse(unread_count=count)


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记单条通知已读。"""
    n = (
        db.query(UserNotification)
        .filter(
            UserNotification.id == notification_id,
            UserNotification.user_id == current_user.id,
        )
        .first()
    )
    if not n:
        raise HTTPException(status_code=404, detail="通知不存在")
    if not n.is_read:
        n.is_read = True
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """全部标记已读。"""
    updated = (
        db.query(UserNotification)
        .filter(UserNotification.user_id == current_user.id, UserNotification.is_read.is_(False))
        .update({UserNotification.is_read: True})
    )
    db.commit()
    return {"ok": True, "updated": updated}


@router.delete("/{notification_id}")
def delete_notification(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除单条通知。"""
    n = (
        db.query(UserNotification)
        .filter(
            UserNotification.id == notification_id,
            UserNotification.user_id == current_user.id,
        )
        .first()
    )
    if not n:
        raise HTTPException(status_code=404, detail="通知不存在")
    db.delete(n)
    db.commit()
    return {"ok": True}


@router.post("/clear")
def clear_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清空当前用户全部通知。"""
    deleted = (
        db.query(UserNotification)
        .filter(UserNotification.user_id == current_user.id)
        .delete()
    )
    db.commit()
    return {"ok": True, "deleted": deleted}


# ── SSE Stream ────────────────────────────────────────────────────────────────
#
# 鉴权契约（EventSource 不支持自定义 header，长期 JWT 放在 URL 上泄漏面太大）：
# 1. 前端先 `POST /api/notifications/stream/ticket`（Bearer 鉴权）换取一次性票据；
# 2. 再 `GET /api/notifications/stream?ticket=<ticket>` 建立 SSE。
# 票据是 60 秒短时 JWT，scope 固定为 notifications-sse，不能当 access token 用；
# access token 也不能当票据用（没有该 scope），双向隔离。

SSE_TICKET_TTL_SECONDS = 60


@router.post("/stream/ticket")
def issue_stream_ticket(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """签发 60 秒 SSE 一次性票据，返回 {"ticket", "expires_in"}。"""
    # 只允许 active 用户领取（禁用账号即使持有未过期 token 也拿不到新票据）。
    if normalize_user_status(getattr(current_user, "status", "active")) != "active":
        raise HTTPException(status_code=403, detail="当前账号已被禁用，无法领取 SSE 票据")
    expires = datetime.utcnow() + timedelta(seconds=SSE_TICKET_TTL_SECONDS)
    ticket = jwt.encode(
        {
            "sub": current_user.username,
            "scope": SSE_TICKET_SCOPE,
            # 会话版本号：重置密码/禁用后已领取的票据同样失效。
            "ver": int(getattr(current_user, "token_version", 0) or 0),
            "exp": expires,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    return {"ticket": ticket, "expires_in": SSE_TICKET_TTL_SECONDS}


def _verify_sse_ticket(ticket: str, db: Session) -> User:
    """校验一次性 SSE 票据：短时 JWT、scope 匹配、用户存在且 active。

    与长期 access token 完全隔离：缺 scope / scope 不对 / 过期 / 版本号
    不一致（改密、禁用）一律 401。
    """
    credentials_exception = HTTPException(status_code=401, detail="SSE 票据无效或已过期")
    try:
        payload = jwt.decode(ticket, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise credentials_exception
    if str(payload.get("scope") or "") != SSE_TICKET_SCOPE:
        raise credentials_exception
    username = payload.get("sub")
    if not username:
        raise credentials_exception
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise credentials_exception
    try:
        # 复用会话版本校验：票据签发后用户被改密/禁用则立即失效。
        _check_token_version(payload, user)
    except HTTPException:
        raise credentials_exception
    if normalize_user_status(getattr(user, "status", "active")) != "active":
        raise credentials_exception
    return user


@router.get("/stream")
async def notification_stream(
    request: Request,
    ticket: str = Query(..., description="一次性 SSE 票据（POST /stream/ticket 换取）"),
):
    """SSE 实时推送端点。

    前端用 EventSource 连接，断线自动重连；无新通知时每 20s 发心跳保活。
    鉴权只接受 60 秒一次性票据（query param），不再接受长期 access token。
    """
    # 手动鉴权（EventSource 无法带 header），票据解码需要独立短会话。
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        user = _verify_sse_ticket(ticket, db)
    finally:
        db.close()

    user_id = user.id
    queue = _subscribe(user_id)
    logger.info("SSE connected: user_id=%s", user_id)

    async def event_generator():
        # 首帧也包进 try：客户端连上立刻断开时，finally 仍能注销订阅，
        # 避免已断开的连接残留在 _sse_subscribers 里泄漏。
        try:
            # 立即 flush 首帧，避免 EventSource 等 20s 才触发 onopen
            yield ": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                if await request.is_disconnected():
                    break
        finally:
            _unsubscribe(user_id, queue)
            logger.info("SSE disconnected: user_id=%s", user_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 透传
        },
    )
