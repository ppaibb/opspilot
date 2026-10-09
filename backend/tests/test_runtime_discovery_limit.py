import unittest
from unittest.mock import MagicMock, patch

from app.api.application_routes.runtime import discover_application_runtime_instances
from app.schemas.application import ApplicationRuntimeDiscoveryRequest


class TestRuntimeDiscoveryLimit(unittest.TestCase):
    """验证后端在扫描运行对象候选时，每种部署类型独立限制候选数量（最大 200），且不互相影响。"""

    @patch("app.api.application_routes.runtime._get_visible_application")
    @patch("app.api.application_routes.runtime._bound_host_assets")
    @patch("app.api.application_routes.runtime._run_runtime_discovery_command")
    @patch("app.api.application_routes.runtime._parse_runtime_discovery_output")
    def test_per_type_isolated_limit(
        self,
        mock_parse_output,
        mock_run_command,
        mock_bound_assets,
        mock_get_app
    ):
        # 1. 模拟应用和主机绑定关系
        mock_app = MagicMock()
        mock_app.app_id = "test_app_123"
        mock_get_app.return_value = mock_app

        # 绑定了一个主机
        mock_asset = MagicMock()
        mock_bound_assets.return_value = {
            "host_id_1": (mock_asset, "Host-01")
        }

        # 2. 模拟命令执行：全部成功
        mock_run_command.return_value = (True, "mock_stdout", "")

        # 3. 模拟解析输出结果：
        # - systemd 返回 250 个候选 (超过 200 限制)
        # - docker 返回 50 个候选
        # - 其他返回 0 个
        def side_effect_parse(discovery_type, stdout):
            if discovery_type == "systemd":
                return [
                    {
                        "deployment_type": "systemd",
                        "name": f"service-{i}",
                        "control_ref": f"service-{i}.service",
                        "status": "running",
                        "description": "mock systemd service",
                    }
                    for i in range(250)
                ]
            elif discovery_type == "docker":
                return [
                    {
                        "deployment_type": "docker",
                        "name": f"container-{i}",
                        "control_ref": f"container-{i}",
                        "status": "running",
                        "description": "mock docker container",
                    }
                    for i in range(50)
                ]
            return []

        mock_parse_output.side_effect = side_effect_parse

        # 4. 调用接口
        payload = ApplicationRuntimeDiscoveryRequest(
            asset_ids=["host_id_1"],
            types=["systemd", "docker"]
        )
        db_mock = MagicMock()
        user_mock = MagicMock()

        result = discover_application_runtime_instances(
            app_id="test_app_123",
            payload=payload,
            db=db_mock,
            current_user=user_mock
        )

        # 5. 验证结果
        candidates = result["candidates"]
        errors = result["errors"]

        # 分类统计候选数量
        systemd_candidates = [c for c in candidates if c["deployment_type"] == "systemd"]
        docker_candidates = [c for c in candidates if c["deployment_type"] == "docker"]

        # 验证 systemd 限制为 200，docker 全额保留为 50
        self.assertEqual(len(systemd_candidates), 200)
        self.assertEqual(len(docker_candidates), 50)
        
        # 验证总数是 250
        self.assertEqual(len(candidates), 250)

        # 验证产生了截断的错误提示
        self.assertTrue(any("systemd" in err and "截断" in err for err in errors))
        # 确认 docker 没有产生截断提示
        self.assertFalse(any("docker" in err and "截断" in err for err in errors))


if __name__ == "__main__":
    unittest.main()
