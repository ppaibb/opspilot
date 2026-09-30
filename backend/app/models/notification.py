"""用户站内通知持久化模型。

面向登录用户的站内信，用于异步操作结果通知（证书续期/签发、虚机同步、Agent 安装等）。
与 monitoring_notification_log（告警推送审计）完全独立。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.db.database import Base


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id = Column(
        String(36),
        primary_key=True,
        default=lambda: uuid.uuid4().hex,
        index=True,
    )
    user_id = Column(Integer, nullable=False, index=True, comment="接收用户 ID")
    type = Column(
        String(20),
        nullable=False,
        default="info",
        comment="通知类型: success / error / warning / info",
    )
    category = Column(
        String(50),
        nullable=False,
        default="system",
        comment="业务分类: certificate / vm / agent / system 等",
    )
    title = Column(String(200), nullable=False, comment="标题")
    content = Column(Text, nullable=False, default="", comment="详情")
    link = Column(String(500), nullable=True, comment="点击跳转的前端路由")
    resource_id = Column(String(100), nullable=True, comment="关联资源 ID")
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
