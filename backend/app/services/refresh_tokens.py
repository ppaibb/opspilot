"""不透明 refresh token 的签发、轮换和撤销。"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import normalize_user_status
from app.models.refresh_token import RefreshToken
from app.models.user import User


class RefreshTokenRejected(ValueError):
    """Refresh token 无效、过期、重放或会话已失效。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(value: datetime) -> str:
    return value.isoformat()


def refresh_token_digest(raw: str) -> str:
    """生成数据库检索用摘要；原始 token 不落库。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_record(user: User, *, family_id: str | None = None) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(48)
    now = _now()
    record = RefreshToken(
        id=str(uuid.uuid4()),
        user_id=user.id,
        family_id=family_id or str(uuid.uuid4()),
        token_hash=refresh_token_digest(raw),
        token_version=int(getattr(user, "token_version", 0) or 0),
        issued_at=_format(now),
        expires_at=_format(now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)),
    )
    return raw, record


def issue_refresh_token(db: Session, user: User) -> str:
    """签发新 refresh family；数据库永不保存原始 token。"""
    raw, record = _new_record(user)
    db.add(record)
    db.commit()
    return raw


def _revoke_family(db: Session, family_id: str, *, at: str) -> None:
    db.query(RefreshToken).filter(
        RefreshToken.family_id == family_id,
        RefreshToken.revoked_at.is_(None),
    ).update({"revoked_at": at}, synchronize_session=False)


def rotate_refresh_token(db: Session, raw: str, user: User) -> str:
    """原子消费旧 token 并签发同族新 token；重放时撤销整族。"""
    now = _now()
    now_text = _format(now)
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == refresh_token_digest(raw))
        .with_for_update()
        .first()
    )
    if not record or record.user_id != user.id:
        raise RefreshTokenRejected("Refresh token 无效")
    if record.used_at:
        _revoke_family(db, record.family_id, at=now_text)
        db.commit()
        raise RefreshTokenRejected("检测到 Refresh token 重放，已撤销整个会话族")
    if record.revoked_at:
        raise RefreshTokenRejected("Refresh token 已撤销")
    if datetime.fromisoformat(record.expires_at) <= now:
        raise RefreshTokenRejected("Refresh token 已过期")
    if normalize_user_status(user.status) != "active":
        _revoke_family(db, record.family_id, at=now_text)
        db.commit()
        raise RefreshTokenRejected("账号已禁用")
    if record.token_version != int(getattr(user, "token_version", 0) or 0):
        _revoke_family(db, record.family_id, at=now_text)
        db.commit()
        raise RefreshTokenRejected("登录状态已失效")

    raw_next, next_record = _new_record(user, family_id=record.family_id)
    record.used_at = now_text
    db.add(next_record)
    db.commit()
    return raw_next


def revoke_refresh_token(db: Session, raw: str) -> None:
    """注销时撤销 token 所属会话族；未知 token 也保持幂等。"""
    record = db.query(RefreshToken).filter(
        RefreshToken.token_hash == refresh_token_digest(raw)
    ).first()
    if not record:
        return
    _revoke_family(db, record.family_id, at=_format(_now()))
    db.commit()
