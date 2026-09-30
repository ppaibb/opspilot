from types import SimpleNamespace

import pytest

from app.api.assets import AssetCreate, resolve_ping_auth, validate_asset_input
from app.services.asset_auth import resolve_credential_ssh_auth


def test_asset_input_rejects_invalid_ip():
    with pytest.raises(Exception) as exc_info:
        validate_asset_input(AssetCreate(name="web", ip="not-an-ip", provider="on-premise"))

    assert "IP 格式非法" in str(exc_info.value)


def test_asset_create_region_defaults_to_local_for_manual_host():
    asset = AssetCreate(name="web", ip="192.168.1.10", provider="on-premise")

    assert asset.region == "local"
    validate_asset_input(asset)


def test_password_credential_is_resolved_for_ssh():
    credential = SimpleNamespace(
        cred_id="cred-1",
        type="Password",
        content="encrypted-password",
    )

    import app.services.asset_auth as asset_auth
    original = asset_auth.decrypt_secret
    asset_auth.decrypt_secret = lambda _: "host-password"
    try:
        auth = resolve_credential_ssh_auth(credential)
    finally:
        asset_auth.decrypt_secret = original

    assert auth.password == "host-password"
    assert auth.pkey_content == ""


def test_ping_prefers_explicit_password_over_ssh_key_credential():
    password, pkey = resolve_ping_auth(
        password="user-typed-password",
        credential_id="cred-key",
        pkey_content="",
        credential_auth=SimpleNamespace(password="", pkey_content="-----BEGIN PRIVATE KEY-----\nbroken\n-----END PRIVATE KEY-----"),
    )

    assert password == "user-typed-password"
    assert pkey == ""


def test_ping_uses_credential_when_no_password_typed():
    password, pkey = resolve_ping_auth(
        password="",
        credential_id="cred-key",
        pkey_content="",
        credential_auth=SimpleNamespace(password="", pkey_content="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----"),
    )

    assert password == ""
    assert pkey.startswith("-----BEGIN")
