"""
Dashboard 每日时序快照数据库模型
用于存储和沉淀系统每日的总量数据，以支撑看板历史演进曲线
"""
from sqlalchemy import Column, Integer, Date, String
from app.db.database import Base

class DashboardSnapshot(Base):
    """
    大盘每日系统总量快照表
    """
    __tablename__ = "dashboard_snapshots"

    id = Column(Integer, primary_key=True, index=True, comment="快照ID")
    date = Column(Date, unique=True, index=True, nullable=False, comment="快照日期")
    
    hosts_total = Column(Integer, default=0, nullable=False, comment="当日主机总数")
    vms_total = Column(Integer, default=0, nullable=False, comment="当日虚机总数")
    applications_total = Column(Integer, default=0, nullable=False, comment="当日应用总数")
    users_total = Column(Integer, default=0, nullable=False, comment="当日用户总数")
    
    updated_at = Column(String(30), default="", nullable=False, comment="最后更新时间")
