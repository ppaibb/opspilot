import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.assets import ensure_asset_ping_write_permission


class AssetPermissionTest(unittest.TestCase):
    def test_ping_without_asset_id_only_needs_connect_permission(self):
        """纯 SSH 探测不写库，不额外要求资产管理权限。"""
        user = SimpleNamespace(role="只读", permission_overrides="")

        ensure_asset_ping_write_permission("", user)

    def test_ping_with_asset_id_requires_manage_permission(self):
        """携带 asset_id 会修改资产连接配置，必须要求 manage 权限。"""
        user = SimpleNamespace(role="只读", permission_overrides="")

        with self.assertRaises(HTTPException) as ctx:
            ensure_asset_ping_write_permission("ins-001", user)

        self.assertEqual(ctx.exception.status_code, 403)

    def test_ping_with_asset_id_allows_admin(self):
        user = SimpleNamespace(role="管理员", permission_overrides="")

        ensure_asset_ping_write_permission("ins-001", user)


if __name__ == "__main__":
    unittest.main()
