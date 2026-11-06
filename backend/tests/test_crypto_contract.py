"""凭证密文 v2 契约：独立密钥、版本兼容和显式失败。"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import settings


@pytest.fixture(autouse=True)
def independent_keys(monkeypatch):
    """每例使用确定且相互独立的 JWT/凭证密钥。"""
    monkeypatch.setattr(settings, "SECRET_KEY", "jwt-key-for-tests")
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "credential-key-for-tests")


def _legacy_v1(raw: str, seed: str) -> str:
    """按历史 enc::<fernet> 格式构造兼容样本。"""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    token = Fernet(key).encrypt(raw.encode("utf-8")).decode("utf-8")
    return f"enc::{token}"


def test_missing_credential_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "")
    with pytest.raises(crypto.CredentialEncryptionConfigError, match="CREDENTIAL_ENCRYPTION_KEY"):
        crypto.encrypt_secret("secret")


def test_credential_key_cannot_equal_jwt_key(monkeypatch):
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", settings.SECRET_KEY)
    with pytest.raises(crypto.CredentialEncryptionConfigError, match="独立"):
        crypto.encrypt_secret("secret")


def test_corrupted_ciphertext_never_becomes_empty_secret():
    with pytest.raises(crypto.CredentialDecryptionError, match="损坏"):
        crypto.decrypt_secret("enc::v2::broken::token")


def test_v1_ciphertext_remains_readable_with_explicit_key():
    ciphertext = _legacy_v1("legacy-secret", settings.CREDENTIAL_ENCRYPTION_KEY)
    assert crypto.decrypt_secret(ciphertext) == "legacy-secret"


def test_v2_uses_random_salt_and_roundtrips():
    first = crypto.encrypt_secret("same-secret")
    second = crypto.encrypt_secret("same-secret")
    assert first.startswith("enc::v2::")
    assert second.startswith("enc::v2::")
    assert first != second
    assert crypto.decrypt_secret(first) == "same-secret"
    assert crypto.decrypt_secret(second) == "same-secret"


def test_plaintext_legacy_value_is_still_readable():
    assert crypto.decrypt_secret("legacy-plain") == "legacy-plain"


def test_migration_only_rewrites_nonempty_legacy_values():
    from scripts.migrate_credential_ciphertext import needs_migration

    assert needs_migration("legacy-plain") is True
    assert needs_migration(_legacy_v1("legacy-secret", settings.CREDENTIAL_ENCRYPTION_KEY)) is True
    assert needs_migration(crypto.encrypt_secret("new-secret")) is False
    assert needs_migration("") is False
    assert needs_migration(None) is False
