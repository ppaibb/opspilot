"""
凭证管理表
"""
from sqlalchemy import Column, Integer, String
from app.db.database import Base

class Credential(Base):
    """凭据保险箱模型"""
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True, index=True)
    cred_id = Column(String(50), unique=True, index=True)
    name = Column(String(100))
    type = Column(String(50)) # 'SSH Key', 'Token', 'Docker Config' 等
    scope = Column(String(200)) # 适用范围
    allowed_roles = Column(String(2000), default="") # JSON 数组；为空表示所有权限组可用
    created_at = Column(String(50))
    expires_at = Column(String(50))
    status = Column(String(50)) # active, expiring, expired
    used_by = Column(Integer, default=0, comment="关联资源数")
    content = Column(String(5000), nullable=True, comment="真实的凭据内容")
