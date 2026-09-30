"""
影子告警表（只写不发通知，用于验证告警引擎逻辑）
"""
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.db.database import Base
from datetime import datetime

class MonitoringShadowAlert(Base):
    __tablename__ = "monitoring_shadow_alerts"

    id = Column(Integer, primary_key=True, index=True)
    fingerprint = Column(String(64), unique=True, index=True, comment="由 rule_id + 排序后的 labels 生成的稳定哈希")
    rule_id = Column(String(64), index=True)
    name = Column(String(100))
    instance = Column(String(100), index=True, default="")
    severity = Column(String(20))
    state = Column(String(20), default="pending", comment="pending / firing / resolved")
    value = Column(String(64), comment="最近一次评估的样本值")
    labels = Column(Text, comment="完整 labels 的 JSON 字符串")
    
    first_breach_at = Column(DateTime, default=datetime.utcnow, comment="首次越线时间")
    active_at = Column(DateTime, nullable=True, comment="转入 firing 的时间")
    resolved_at = Column(DateTime, nullable=True, comment="恢复时间")
    
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
