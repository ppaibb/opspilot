import unittest

from app.api.certificates import router, normalize_domain


class CertificateUploadContractTest(unittest.TestCase):
    """验证前端/API 依赖的证书上传路由被正确注册。"""

    def _registered_methods(self, path: str) -> set[str]:
        """返回指定路径在 FastAPI Router 中注册的方法集合。"""
        methods: set[str] = set()
        for route in router.routes:
            if getattr(route, "path", "") == path:
                methods.update(getattr(route, "methods", set()) or set())
        return methods

    def test_upload_supports_post(self) -> None:
        """证书上传接口应该支持 POST。"""
        methods = self._registered_methods("/upload")
        self.assertIn("POST", methods)

    def test_normalize_domain_wildcard(self) -> None:
        """验证 normalize_domain 能够正确处理和支持通配符域名。"""
        # 测试普通域名
        self.assertEqual(normalize_domain("ppaibb.com"), "ppaibb.com")
        # 测试带 *. 的通配符域名
        self.assertEqual(normalize_domain("*.ppaibb.com"), "*.ppaibb.com")
        # 测试首尾有空格
        self.assertEqual(normalize_domain(" *.ppaibb.com "), "*.ppaibb.com")
        # 测试大写
        self.assertEqual(normalize_domain("*.PPAIBB.COM"), "*.ppaibb.com")

        # 测试非法域名
        with self.assertRaises(Exception):
            normalize_domain("ppaibb.*")
        with self.assertRaises(Exception):
            normalize_domain("ppaibb.*.com")
        with self.assertRaises(Exception):
            normalize_domain("*.ppaibb.*.com")


if __name__ == "__main__":
    unittest.main()
