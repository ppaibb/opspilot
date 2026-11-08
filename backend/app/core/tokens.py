"""JWT 唯一编解码入口。

所有 JWT 都固定使用配置算法并在解码时显式给出算法白名单，避免调用点各自
遗漏校验。Access token 另外带 ``type=access``，不能与内部短票据混用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings


class TokenValidationError(ValueError):
    """JWT 无效、过期或用途不匹配。"""


def encode_jwt(payload: dict[str, Any]) -> str:
    """使用系统唯一允许的算法签发 JWT。"""
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_jwt(token: str, *, audience: str | None = None) -> dict[str, Any]:
    """按严格算法白名单验证 JWT。"""
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=audience,
            options={"require": ["exp"], "verify_aud": audience is not None},
        )
    except jwt.PyJWTError as exc:
        raise TokenValidationError("Token 无效或已过期") from exc


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """签发带用途、签发时间和过期时间的短期 access token。"""
    now = datetime.now(timezone.utc)
    expires = now + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = dict(data)
    payload.update({"type": "access", "iat": now, "exp": expires})
    return encode_jwt(payload)


def decode_access_token(token: str) -> dict[str, Any]:
    """验证并返回 access token，拒绝其他用途 JWT。"""
    payload = decode_jwt(token)
    if payload.get("type") != "access":
        raise TokenValidationError("Token 用途不正确")
    return payload
