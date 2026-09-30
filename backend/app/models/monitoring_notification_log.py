"""通知发送记录(审计)持久化模型。

每次通过某个通知渠道推送告警(无论触发/恢复、成功/失败)都写入一行,
用于「通知发送记录/审计」页面查看推送结果与目标渠道。
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean

from app.db.database import Base


class MonitoringNotificationLog(Base):
    __tablename__ = "monitoring_notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    # 告警关联
    fingerprint = Column(String(64), default="", nullable=False, index=True)
    alert_name = Column(String(100), default="", nullable=False, index=True)
    instance = Column(String(100), default="", nullable=False)
    severity = Column(String(20), default="warning", nullable=False)
    # 触发/恢复
    event_type = Column(String(20), default="firing", nullable=False)  # firing / resolved
    # 渠道信息
    channel_id = Column(String(64), default="", nullable=False)
    channel_name = Column(String(100), default="", nullable=False, index=True)
    channel_type = Column(String(30), default="", nullable=False)  # webhook/dingtalk/wechat/feishu/mail
    target = Column(String(255), default="", nullable=False)  # 推送目标(脱敏后的 url/收件人)
    # 结果
    success = Column(Boolean, default=False, nullable=False, index=True)
    message = Column(Text, default="", nullable=False)  # 成功/失败说明
    # 时间
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
