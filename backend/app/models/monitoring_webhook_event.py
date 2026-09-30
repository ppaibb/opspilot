"""监控 Webhook 接收事件模型。"""

from sqlalchemy import Column, Integer, String, Text

from app.db.database import Base


class MonitoringWebhookEvent(Base):
    """记录内置 Webhook 接收器收到的告警请求。

    设计说明：
    1. 这里只做“收包留痕”，不对负载做任何命令执行或动态解释。
    2. `payload_preview` 仅保存截断后的文本，避免数据库被异常大报文撑爆。
    3. 该表主要用于联调与验通，不承担长期归档职责。
    """

    __tablename__ = "monitoring_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(64), unique=True, index=True, nullable=False)
    received_at = Column(String(30), index=True, nullable=False, default="")
    remote_addr = Column(String(64), default="-", nullable=False)
    source = Column(String(64), default="alertmanager", nullable=False)
    status = Column(String(20), default="received", nullable=False)
    alert_count = Column(Integer, default=0, nullable=False)
    summary = Column(String(255), default="", nullable=False)
    payload_preview = Column(Text, default="", nullable=False)
