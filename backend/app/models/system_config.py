"""
系统配置模型（键值存储）
"""
from sqlalchemy import Column, Integer, String

from app.db.database import Base


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String(80), unique=True, index=True, nullable=False)
    config_value = Column(String(8000), default="", nullable=False)
    updated_at = Column(String(30), default="", nullable=False)
