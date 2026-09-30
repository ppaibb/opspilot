"""
操作审计日志模型
"""
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.db.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    time = Column(DateTime, default=datetime.utcnow, index=True)
    user = Column(String(100), default="未知用户")
    action = Column(String(100))
    resource = Column(String(255))
    ip = Column(String(64), default="-")
    result = Column(String(20), default="success")
    risk_level = Column(String(10), default="", comment="操作风险等级：low/medium/high")
    risk_reason = Column(String(500), default="", comment="高风险操作原因")
