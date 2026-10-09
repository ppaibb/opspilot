"""运行实例 update 动作契约测试。

验证 `_build_runtime_update_module_args` 和 Docker 发布脚本的命令边界：
- systemd：含 daemon-reload、restart、status；
- docker：运行实例 update 被拒绝；发布部署必须显式传镜像地址；
- kubernetes：`kubectl rollout restart`，并兼容 `namespace/deployment/name`；
- helm：拒绝（没有 chart 引用）；
- 未知类型：拒绝。

不实际调用远程主机，只检查命令文本。
"""
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.applications import (
    _build_docker_container_release_module_args,
    _build_runtime_update_module_args,
    _build_supervisor_artifact_release_module_args,
    _build_systemd_artifact_release_module_args,
)
from app.services.application_runtime import build_runtime_control_module_args


def _row(deployment_type: str, control_ref: str, runtime: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        deployment_type=deployment_type,
        control_ref=control_ref,
        runtime=runtime,
        app_id="APP-X",
        component_id="COMP-X",
    )


class RuntimeUpdateCommandTest(unittest.TestCase):
    def test_systemd_update_reloads_and_restarts(self):
        with self.assertRaises(HTTPException) as ctx:
            _build_runtime_update_module_args(_row("systemd", "api.service"), db=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("systemd 发布必须", str(ctx.exception.detail))

    def test_systemd_release_switches_current_link_and_restarts(self):
        cmd = _build_systemd_artifact_release_module_args(
            "api.service",
            "/opt/apps/api",
            "/opt/releases/api.jar",
        )
        self.assertIn("ln -sfn \"$target\" \"$current\"", cmd)
        self.assertIn("systemctl daemon-reload", cmd)
        self.assertIn("systemctl restart \"$service\"", cmd)
        self.assertIn("systemctl status --no-pager \"$service\"", cmd)

    def test_docker_update_requires_release_image(self):
        with self.assertRaises(HTTPException) as ctx:
            _build_runtime_update_module_args(_row("docker", "mall-api"), db=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Docker 发布必须", str(ctx.exception.detail))

    def test_docker_release_rebuilds_container_with_explicit_image(self):
        cmd = _build_docker_container_release_module_args(
            "mall-api",
            "registry.example.com/mall-api:2.0.0",
            "-p 8080:80 --restart always",
        )
        self.assertIn("docker pull \"$image\"", cmd)
        self.assertIn("docker rename \"$container\" \"$backup\"", cmd)
        self.assertIn("docker run -d --name \"$container\" -p 8080:80 --restart always \"$image\"", cmd)
        self.assertIn("rolled back to previous container", cmd)

    def test_supervisor_release_writes_program_config_and_restarts(self):
        cmd = _build_supervisor_artifact_release_module_args(
            "mall-api",
            "/opt/apps/mall-api",
            "java -jar /opt/apps/mall-api/current",
            "/opt/releases/mall-api.jar",
            run_user="www-data",
            environment="APP_ENV=prod",
        )
        self.assertIn("/etc/supervisor/conf.d/mall-api.conf", cmd)
        self.assertIn("supervisorctl reread", cmd)
        self.assertIn("supervisorctl update", cmd)
        self.assertIn("supervisorctl restart \"$program\"", cmd)
        self.assertIn("rolled back current link", cmd)

    def test_managed_supervisor_release_uses_opspilot_runtime_config(self):
        cmd = _build_supervisor_artifact_release_module_args(
            "mall-api",
            "/opt/apps/mall-api",
            "java -jar /opt/apps/mall-api/current",
            "/opt/releases/mall-api.jar",
            runtime_mode="managed",
        )

        self.assertIn("/opt/opspilot/supervisor/conf.d/mall-api.conf", cmd)
        self.assertIn("opspilot-supervisord.service", cmd)
        self.assertIn("supervisorctl -c /opt/opspilot/supervisor/supervisord.conf restart \"$program\"", cmd)

    def test_managed_supervisor_control_uses_opspilot_runtime_config(self):
        cmd = build_runtime_control_module_args(
            "supervisor",
            "mall-api",
            "restart",
            "supervisor:managed",
        )

        self.assertEqual(cmd, "supervisorctl -c /opt/opspilot/supervisor/supervisord.conf restart mall-api")

    def test_kubernetes_update_uses_rollout_restart(self):
        cmd = _build_runtime_update_module_args(_row("kubernetes", "default/deployment/api"), db=None)
        self.assertEqual(cmd, "kubectl -n default rollout restart deployment/api")

    def test_kubernetes_update_with_two_part_ref_defaults_to_deployment(self):
        cmd = _build_runtime_update_module_args(_row("kubernetes", "default/api"), db=None)
        self.assertEqual(cmd, "kubectl -n default rollout restart deployment/api")

    def test_helm_update_rejected_without_chart(self):
        with self.assertRaises(HTTPException) as ctx:
            _build_runtime_update_module_args(_row("helm", "default/api"), db=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("helm upgrade", str(ctx.exception.detail))

    def test_unknown_runtime_type_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _build_runtime_update_module_args(_row("external", "whatever"), db=None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("不支持", str(ctx.exception.detail))

    def test_systemd_release_quotes_control_ref_safely(self):
        """即便历史脏数据里塞了 shell 元字符，命令也应当走 shlex.quote 包裹整个参数。"""
        cmd = _build_systemd_artifact_release_module_args(
            "api.service; rm -rf /",
            "/opt/apps/api",
            "/opt/releases/api.jar",
        )
        # shlex.quote 会在出现元字符的情况下整体加单引号。
        self.assertIn("service='api.service; rm -rf /'", cmd)
        # 并且不会把分号当作命令分隔符出现在裸文本里。
        self.assertNotIn("systemctl restart api.service; rm", cmd)


if __name__ == "__main__":
    unittest.main()
