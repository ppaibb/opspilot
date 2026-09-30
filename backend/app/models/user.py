"""
用户系统数据库模型 (ORM Model)
遵循 Google Python Style Guide
"""
from sqlalchemy import Column, Integer, String
from app.db.database import Base

class User(Base):
    """
    使用数据库持久化的目标用户表
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, comment="用户自增ID")
    username = Column(String(50), unique=True, index=True, nullable=False, comment="登录凭证_用户名")
    name = Column(String(50), comment="用户中文姓名")
    email = Column(String(100), unique=True, index=True, comment="用户邮箱")
    role = Column(String(20), default="user", comment="角色权限(admin/user)")
    department = Column(String(50), comment="所属部门")
    status = Column(String(20), default="active", comment="账号状态(active/disabled)")
    last_login_at = Column(String(30), default="", comment="最近登录时间")
    permission_overrides = Column(
        String(8000),
        default="",
        comment="用户级 RBAC 权限覆盖（JSON 字符串）",
    )
    # 会话版本号：重置密码 / 禁用账号时 +1，签发 JWT 时写入 ver claim，
    # 校验时不一致即拒绝——实现"改密/禁用立刻吊销全部已签发 token"。
    # 开发库由启动时 ensure_model_columns() 自动补列，无需迁移文件。
    token_version = Column(Integer, default=0, comment="会话版本号（重置密码/禁用时递增，旧 token 立即失效）")
    hashed_password = Column(String(255), nullable=False, comment="bcrypt 加盐哈希值")
