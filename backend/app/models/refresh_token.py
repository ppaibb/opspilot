"""Refresh token 与登录失败状态模型。"""

from sqlalchemy import Column, ForeignKey, Integer, String

from app.db.database import Base


class RefreshToken(Base):
    """只保存 refresh token 摘要，支持轮换、重放检测和整族撤销。"""

    __tablename__ = "refresh_tokens"

    id = Column(String(36), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id = Column(String(36), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_version = Column(Integer, nullable=False, default=0)
    issued_at = Column(String(40), nullable=False)
    expires_at = Column(String(40), nullable=False, index=True)
    used_at = Column(String(40), nullable=True)
    revoked_at = Column(String(40), nullable=True)


class LoginAttempt(Base):
    """跨 worker 共享的登录失败计数与锁定状态。"""

    __tablename__ = "login_attempts"

    key_hash = Column(String(64), primary_key=True)
    username = Column(String(50), nullable=False, default="")
    source_ip = Column(String(64), nullable=False, default="")
    failure_count = Column(Integer, nullable=False, default=0)
    window_started_at = Column(String(40), nullable=False)
    locked_until = Column(String(40), nullable=True)
    updated_at = Column(String(40), nullable=False)
