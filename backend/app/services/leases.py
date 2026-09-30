"""数据库租约服务：多进程部署下防止后台任务重复执行的最小实现。

原理：
1. 首次获取直接 INSERT，依赖 task_leases.lease_key 的唯一约束；
2. 唯一冲突说明已有人持有，此时尝试条件 UPDATE 抢一个**已过期**的租约
   （`WHERE lease_key = ? AND expires_at < now`），以 rowcount==1 判定是否抢到；
3. 释放即删行（校验 owner，防止误删他人租约）；持有者崩溃时靠 TTL 到期
   自动失效，无需心跳也能自愈。

时间统一使用 format_app_datetime 的 `YYYY-MM-DD HH:MM:SS` 格式，字符串
比较与时间比较等价，过期判断直接在 SQL 的 WHERE 里完成。
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.task_lease import TaskLease
from app.services.application_time import format_app_datetime, now_dt

logger = logging.getLogger(__name__)


def lease_owner() -> str:
    """生成实例级租约持有者标识。

    同一进程内每个调用返回不同值（含随机段），调用方应在一次
    「获取-续约-释放」生命周期内复用同一个 owner 字符串。

    Returns:
        形如 `hostname:12345:1a2b3c4d` 的实例标识。
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _now_str() -> str:
    """返回租约体系统一的当前时间字符串。"""
    return format_app_datetime(now_dt())


def _expiry_str(ttl_seconds: int) -> str:
    """按 TTL 计算过期时间字符串。"""
    return format_app_datetime(now_dt() + timedelta(seconds=max(int(ttl_seconds), 1)))


def acquire_lease(db: Session, lease_key: str, owner: str, ttl_seconds: int) -> bool:
    """获取跨进程租约。

    优先 INSERT 新行；lease_key 冲突时说明已有持有者，仅当对方租约已过期
    （expires_at < now）才允许条件 UPDATE 抢占。

    Args:
        db: 数据库会话（调用方负责开关与事务外的异常兜底）。
        lease_key: 全局唯一的任务键，如 `scheduler:bill_sync`。
        owner: 本次持有者标识（同一生命周期内保持不变）。
        ttl_seconds: 租约有效期（秒），到期后其他实例可抢占。

    Returns:
        True 表示成功持有租约；False 表示已被其他持有者占用且未过期。
    """
    now = _now_str()
    expires_at = _expiry_str(ttl_seconds)
    try:
        db.add(
            TaskLease(
                lease_key=lease_key,
                owner=owner,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=expires_at,
            )
        )
        db.commit()
        return True
    except IntegrityError:
        # 唯一键冲突：租约已存在，回滚后进入过期抢占分支。
        db.rollback()
    result = db.execute(
        update(TaskLease)
        .where(TaskLease.lease_key == lease_key, TaskLease.expires_at < now)
        .values(owner=owner, acquired_at=now, heartbeat_at=now, expires_at=expires_at)
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def renew_lease(db: Session, lease_key: str, owner: str, ttl_seconds: int) -> bool:
    """续约租约（仅持有者本人可续，顺带刷新心跳时间）。

    Args:
        db: 数据库会话。
        lease_key: 任务键。
        owner: 持有者标识，不匹配则不续约。
        ttl_seconds: 从当前时刻起的新有效期（秒）。

    Returns:
        True 表示续约成功；False 表示租约不存在、已过期被抢或 owner 不匹配。
    """
    result = db.execute(
        update(TaskLease)
        .where(TaskLease.lease_key == lease_key, TaskLease.owner == owner)
        .values(heartbeat_at=_now_str(), expires_at=_expiry_str(ttl_seconds))
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def release_lease(db: Session, lease_key: str, owner: str) -> None:
    """释放租约（删除该行，校验 owner 防止误删他人租约）。

    owner 不匹配或租约不存在时静默忽略，保证幂等；删除行而非清空
    expires_at，可让下一次获取走最简单的 INSERT 路径。

    Args:
        db: 数据库会话。
        lease_key: 任务键。
        owner: 持有者标识。
    """
    db.execute(delete(TaskLease).where(TaskLease.lease_key == lease_key, TaskLease.owner == owner))
    db.commit()
