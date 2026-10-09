"""应用发布服务拆分契约测试。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class ApplicationReleaseServiceSplitContractTest(unittest.TestCase):
    """验证发布规划和发布记录工具已从路由文件拆到 service 层。"""

    def test_release_service_exports_helpers(self) -> None:
        from app.services.application_releases import serialize_release, truncate_release_output

        row = SimpleNamespace(
            release_id="REL-1",
            app_id="APP-1",
            component_id="",
            component_name="",
            version="1.0.0",
            env="prod",
            release_type="external",
            external_url="",
            template_id="",
            template_name="",
            artifact_ref="",
            rollback_release_id="",
            target_resource_ids='["host-1"]',
            target_names='["主机1"]',
            status="success",
            operator="alice",
            started_at="",
            finished_at="",
            duration_ms=0,
            summary="",
            output="",
        )

        self.assertEqual(["host-1"], serialize_release(row)["target_resource_ids"])
        self.assertEqual("tail", truncate_release_output("head", "tail")[-4:])

    def test_old_api_module_keeps_release_plan_compatibility(self) -> None:
        from app.api.applications import _plan_application_release

        self.assertTrue(callable(_plan_application_release))


if __name__ == "__main__":
    unittest.main()
