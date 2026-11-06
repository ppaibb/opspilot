"""敏感字段加密兼容入口。

业务代码历史上从本模块导入加解密函数。真正的密钥管理与密文格式放在
``app.services.credential_crypto``，此处只保留稳定导入路径。
"""

from app.services.credential_crypto import (
    CredentialCryptoError,
    CredentialDecryptionError,
    CredentialEncryptionConfigError,
    decrypt_secret,
    encrypt_secret,
    validate_credential_encryption_settings,
)

__all__ = [
    "CredentialCryptoError",
    "CredentialDecryptionError",
    "CredentialEncryptionConfigError",
    "decrypt_secret",
    "encrypt_secret",
    "validate_credential_encryption_settings",
]
