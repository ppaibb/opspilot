"""主机 SSH 绑定凭证解析契约测试。"""
from __future__ import annotations

import unittest

from app.core.crypto import encrypt_secret
from app.models.credential import Credential
from app.services.asset_auth import resolve_credential_ssh_auth


class AssetSshCredentialAuthContractTest(unittest.TestCase):
    """验证主机绑定凭证可区分 SSH 私钥和 SSH 密码。"""

    def test_password_credential_is_used_as_ssh_password(self) -> None:
        credential = Credential(
            cred_id="CRED-PWD",
            name="root-password",
            type="Password",
            content=encrypt_secret("plain-secret"),
        )

        auth = resolve_credential_ssh_auth(credential)

        self.assertEqual("plain-secret", auth.password)
        self.assertEqual("", auth.pkey_content)
        self.assertIn("Password", auth.notice)

    def test_ssh_key_credential_is_used_as_private_key(self) -> None:
        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        credential = Credential(
            cred_id="CRED-KEY",
            name="ssh-key",
            type="SSH Key",
            content=encrypt_secret(private_key),
        )

        auth = resolve_credential_ssh_auth(credential)

        self.assertEqual("", auth.password)
        self.assertEqual(private_key, auth.pkey_content)
        self.assertIn("SSH Key", auth.notice)

    def test_non_ssh_credential_is_ignored_for_host_login(self) -> None:
        credential = Credential(
            cred_id="CRED-TOKEN",
            name="api-token",
            type="Token",
            content=encrypt_secret("token-secret"),
        )

        auth = resolve_credential_ssh_auth(credential)

        self.assertEqual("", auth.password)
        self.assertEqual("", auth.pkey_content)
        self.assertIn("不适用于 SSH", auth.notice)


if __name__ == "__main__":
    unittest.main()
