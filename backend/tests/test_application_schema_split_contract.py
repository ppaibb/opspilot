"""应用中心 schema 拆分契约测试。"""
from __future__ import annotations

import unittest


class ApplicationSchemaSplitContractTest(unittest.TestCase):
    """验证应用中心模型已从路由文件拆出，同时保留旧导入兼容。"""

    def test_application_models_are_importable_from_schema_module(self) -> None:
        from app.schemas.application import ApplicationPayload, ApplicationReleasePreviewResponse

        payload = ApplicationPayload(name="订单系统")
        self.assertEqual("订单系统", payload.name)
        self.assertEqual("external", payload.entry_type)
        self.assertTrue(ApplicationReleasePreviewResponse(release_type="external", allowed=True).allowed)

    def test_old_api_module_still_exports_application_models(self) -> None:
        from app.api.applications import ApplicationPayload

        self.assertEqual("external", ApplicationPayload(name="兼容应用").run_mode)


if __name__ == "__main__":
    unittest.main()
