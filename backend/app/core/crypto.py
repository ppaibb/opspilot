"""
敏感字段加密工具（凭证内容）
"""
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

ENC_PREFIX = "enc::"


def _derive_key(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    # 优先使用显式配置，不存在则回退到基于 SECRET_KEY 派生的密钥
    seed = settings.CREDENTIAL_ENCRYPTION_KEY.strip() or settings.SECRET_KEY
    return Fernet(_derive_key(seed))


def encrypt_secret(raw: str) -> str:
    if raw is None:
        return ""
    text = raw.strip()
    if not text:
        return ""
    token = _get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")
    return f"{ENC_PREFIX}{token}"


def decrypt_secret(ciphertext: str) -> str:
    if ciphertext is None:
        return ""
    text = ciphertext.strip()
    if not text:
        return ""
    # 兼容历史明文数据
    if not text.startswith(ENC_PREFIX):
        return text
    payload = text[len(ENC_PREFIX):]
    try:
        return _get_fernet().decrypt(payload.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""
