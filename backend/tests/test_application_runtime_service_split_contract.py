"""应用运行实例服务拆分契约测试。"""
from __future__ import annotations

import unittest


class ApplicationRuntimeServiceSplitContractTest(unittest.TestCase):
    """验证运行实例纯逻辑已从路由文件拆到 service 层。"""

    def test_runtime_service_exports_safe_command_builders(self) -> None:
        from app.services.application_runtime import (
            build_runtime_control_module_args,
            build_compose_preflight_module_args,
            build_compose_project_discovery_command,
            build_compose_update_module_args,
            derive_runtime_status,
            is_system_runtime_service,
            parse_compose_discovery_output,
            parse_compose_control_ref,
            parse_kubernetes_control_ref,
            validate_compose_deploy_path,
        )

        self.assertEqual(("default", "deployment", "api"), parse_kubernetes_control_ref("default/api"))
        self.assertEqual("/opt/order", validate_compose_deploy_path("/opt/order"))
        self.assertIn(
            "systemctl status",
            build_runtime_control_module_args("systemd", "api.service", "status"),
        )
        self.assertTrue(is_system_runtime_service("systemd-journald.service"))
        self.assertTrue(is_system_runtime_service("sshd.service"))
        self.assertFalse(is_system_runtime_service("order-api.service"))
        self.assertIn("docker-compose", build_runtime_control_module_args("docker_compose", "compose=/opt/harbor/docker-compose.yml;service=proxy", "status"))
        self.assertEqual("unknown", derive_runtime_status("status", "docker_compose", False, "", "Docker Compose CLI 不可用"))
        preflight_command, compose_file = build_compose_preflight_module_args(
            "opspilot-compose-demo",
            "/opt/opspilot/test/demo",
            "docker-compose.yml",
            "services:\n  web:\n    image: nginx:alpine",
        )
        self.assertEqual("/opt/opspilot/test/demo/docker-compose.yml", compose_file)
        self.assertIn("mktemp -d", preflight_command)
        self.assertIn("config --services", preflight_command)
        self.assertNotIn("up -d", preflight_command)
        self.assertIn("com.docker.compose.project=opspilot-compose-demo", build_compose_project_discovery_command("opspilot-compose-demo"))
        self.assertEqual(
            ("/data/deploy-test/docker-compose.yml", "", ""),
            parse_compose_control_ref("compose=/data/deploy-test/docker-compose.yml"),
        )
        deploy_command = build_compose_update_module_args(
            "/data/deploy-test/docker-compose.yml",
            "",
            "services:\n  web:\n    image: nginx:alpine",
            env_content="TZ=Asia/Shanghai",
            project_name="deploy-test",
        )
        self.assertIn("目标 Compose 项目已存在", deploy_command)
        self.assertIn("rollback_deploy", deploy_command)
        self.assertIn("down --remove-orphans", deploy_command)
        self.assertIn("Compose 服务未全部运行", deploy_command)
        self.assertIn("已清理本次创建的 Compose 项目和文件", deploy_command)
        self.assertIn("部署目录已存在且非空", deploy_command)
        compose_items = parse_compose_discovery_output(
            "order-prod\t/opt/order\tdocker-compose.yml\tapi\torder-prod-api-1\tUp 2 hours"
        )
        self.assertEqual("order-prod", compose_items[0]["project_name"])
        self.assertEqual("order-prod / api", compose_items[0]["name"])

    def test_old_api_module_keeps_runtime_helper_compatibility(self) -> None:
        from app.api.applications import _parse_kubernetes_control_ref

        self.assertEqual(("prod", "statefulset", "redis"), _parse_kubernetes_control_ref("prod/statefulset/redis"))


if __name__ == "__main__":
    unittest.main()
