"""版本化凭证加密服务。

v2 密文为 ``enc::v2::<salt>::<token>``。每条密文使用独立随机盐，通过
HKDF 从专用主密钥派生 Fernet 密钥，避免凭证密钥与 JWT 签名密钥耦合。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

ENC_PREFIX = "enc::"
V2_PREFIX = "enc::v2::"
HKDF_INFO = b"opspilot/credential/v2"


class CredentialCryptoError(RuntimeError):
    """凭证加解密错误基类。"""


class CredentialEncryptionConfigError(CredentialCryptoError):
    """凭证加密主密钥缺失或不安全。"""


class CredentialDecryptionError(CredentialCryptoError):
    """密文损坏、格式非法或无法用当前主密钥解密。"""


def _credential_seed() -> bytes:
    """读取并校验独立凭证主密钥，任何不安全配置均 fail-closed。"""
    seed = settings.CREDENTIAL_ENCRYPTION_KEY.strip()
    if not seed:
        raise CredentialEncryptionConfigError(
            "缺少 CREDENTIAL_ENCRYPTION_KEY，拒绝处理敏感凭证"
        )
    if seed == settings.SECRET_KEY.strip():
        raise CredentialEncryptionConfigError(
            "CREDENTIAL_ENCRYPTION_KEY 必须与 JWT SECRET_KEY 独立"
        )
    return seed.encode("utf-8")


def validate_credential_encryption_settings() -> None:
    """供应用启动阶段调用，尽早暴露不可恢复的密钥配置错误。"""
    _credential_seed()


def _legacy_fernet() -> Fernet:
    """构造 v1 Fernet；仅使用显式凭证密钥，不再回退 JWT 密钥。"""
    digest = hashlib.sha256(_credential_seed()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _v2_fernet(salt: bytes) -> Fernet:
    """使用每条密文的随机盐派生 v2 Fernet 密钥。"""
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
    ).derive(_credential_seed())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(raw: str | None) -> str:
    """把非空敏感文本加密为 v2；空值保持为空。"""
    if raw is None:
        return ""
    text = raw.strip()
    if not text:
        return ""
    salt = os.urandom(16)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    token = _v2_fernet(salt).encrypt(text.encode("utf-8")).decode("ascii")
    return f"{V2_PREFIX}{salt_text}::{token}"


def decrypt_secret(ciphertext: str | None) -> str:
    """解密 v2/v1，兼容历史明文；损坏密文必须显式抛错。"""
    if ciphertext is None:
        return ""
    text = ciphertext.strip()
    if not text:
        return ""
    if not text.startswith(ENC_PREFIX):
        return text

    try:
        if text.startswith(V2_PREFIX):
            parts = text.split("::", 3)
            if len(parts) != 4 or not parts[2] or not parts[3]:
                raise ValueError("v2 密文段数非法")
            salt = base64.b64decode(parts[2], altchars=b"-_", validate=True)
            if len(salt) != 16:
                raise ValueError("v2 salt 长度非法")
            raw = _v2_fernet(salt).decrypt(parts[3].encode("ascii"))
        else:
            payload = text[len(ENC_PREFIX) :]
            if not payload:
                raise ValueError("v1 密文为空")
            raw = _legacy_fernet().decrypt(payload.encode("ascii"))
        return raw.decode("utf-8")
    except CredentialEncryptionConfigError:
        raise
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise CredentialDecryptionError("凭证密文已损坏或无法用当前密钥解密") from exc
