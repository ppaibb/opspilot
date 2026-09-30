"""已解决历史告警持久化模型。"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime

from app.db.database import Base


class MonitoringHistoryAlert(Base):
    """持久化保存已解决（Resolved）告警历史记录的数据库模型。

    设计说明：
    1. 当活跃告警被解决（包括防抖期满或无防抖立即恢复）时，将其移出活跃告警表，并在此表归档留痕。
    2. 使用 `fingerprint` 进行告警关联，使用 `started_at` 和 `resolved_at` 记录完整的告警存续时间。
    """

    __tablename__ = "monitoring_history_alerts"

    id = Column(Integer, primary_key=True, index=True)
    # 告警指纹
    fingerprint = Column(String(64), index=True, nullable=False)
    name = Column(String(100), nullable=False, index=True)
    instance = Column(String(100), nullable=False, index=True)
    job = Column(String(100), default="", nullable=False)
    severity = Column(String(20), default="warning", nullable=False)
    summary = Column(Text, default="", nullable=False)
    description = Column(Text, default="", nullable=False)
    # 所属分组（来自告警规则的 group 标签快照，便于历史按分组筛选）
    group = Column(String(60), default="", nullable=False)
    
    # 时间信息（均记录为 "%Y-%m-%d %H:%M:%S" 格式的本地时间字符串，便于检索）
    started_at = Column(String(30), default="", nullable=False)
    resolved_at = Column(String(30), default="", nullable=False)
    
    status = Column(String(20), default="resolved", nullable=False)  # 历史告警默认状态为 resolved
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
