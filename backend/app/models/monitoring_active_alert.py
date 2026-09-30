"""监控活动告警持久化模型。"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime

from app.db.database import Base


class MonitoringActiveAlert(Base):
    """持久化保存监控活动告警状态的数据库模型。

    设计说明：
    1. 保存当前处于 firing 或 pending 状态的告警，由内置告警引擎维护，进程重启后状态不丢失。
    2. 使用 `fingerprint`（由 alertname + instance + labels 复合哈希/指纹）唯一标识一条活跃告警。
    3. `state` 字段记录当前告警状态（pending / firing），以配合前端做不同状态的视觉呈现。
    """

    __tablename__ = "monitoring_active_alerts"

    id = Column(Integer, primary_key=True, index=True)
    # 告警指纹，用来唯一标识一条告警实例（alertname + instance + labels hash）
    fingerprint = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False, index=True)
    instance = Column(String(100), nullable=False, index=True)
    job = Column(String(100), default="", nullable=False)
    state = Column(String(20), default="pending", nullable=False)  # pending / firing
    severity = Column(String(20), default="warning", nullable=False)
    summary = Column(Text, default="", nullable=False)
    description = Column(Text, default="", nullable=False)
    active_at = Column(String(30), default="", nullable=False)  # 告警触发的 ISO 时间串或时间戳
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    labels = Column(Text, default="{}", nullable=False)  # 存储完整标签的 JSON 字符串
    
    # 消息降噪限流与防抖跟踪字段
    last_notified_at = Column(DateTime, nullable=True)  # 上次通知发送时间
    notification_count = Column(Integer, default=0, nullable=False)  # 累计通知次数
    recovery_candidate_at = Column(DateTime, nullable=True)  # 首次判定为恢复的候选时间戳（用于恢复防抖时长控制）


