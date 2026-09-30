"""应用发布预检契约测试。

不实际执行远程命令，只验证 `_plan_application_release` 的计划输出：
- 绑定主机与权限校验；
- K8s / Helm 固定命令预览；
- 回滚只指向同应用同实例已成功发布记录；
- 环境错配 / 未绑定凭证会给出警告。
"""
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.applications import (
    ApplicationReleasePayload,
    _plan_application_release,
)
from app.api.application_routes.releases import _ensure_release_plan_allowed
from app.db.database import Base
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationRelease,
    ApplicationResource,
)
from app.models.asset import Asset


def _make_session():
    """构建独立的内存 SQLite 会话。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # app.api.applications 会连带导入 deployment_task 模型，其 JSONB 列是 PostgreSQL 专属类型，
    # 在 SQLite 上编译不了，全量 create_all 会失败；这里只建其余表（本套件用不到该表）。
    Base.metadata.create_all(
        bind=engine,
        tables=[t for name, t in Base.metadata.tables.items() if name != "application_deployment_tasks"],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _make_user(role: str = "管理员") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        username="tester",
        name="Tester",
        role=role,
        permission_overrides="",
    )


def _seed_application(db) -> Application:
    app = Application(
        app_id="APP-1",
        name="订单系统",
        environment="prod",
        status="active",
        run_mode="external",
        entry_type="external",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _seed_host(db, *, instance_id: str, ip: str, credential_ref: str = "cred-1") -> Asset:
    asset = Asset(
        instance_id=instance_id,
        name=f"host-{instance_id}",
        ip=ip,
        provider="manual",
        region="",
        status="online",
        type="healthy",
        ssh_user="root",
        ssh_port=22,
        credential_ref=credential_ref,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _bind_host(db, app_id: str, asset: Asset) -> None:
    db.add(
        ApplicationResource(
            app_id=app_id,
            resource_type="host",
            resource_id=asset.instance_id,
            resource_name=asset.name,
        )
    )
    db.commit()


def _seed_component(db, app_id: str, **kwargs) -> ApplicationComponent:
    defaults = {
        "component_id": "COMP-1",
        "app_id": app_id,
        "name": "api",
        "component_type": "backend",
        "deployment_type": "kubernetes",
        "control_method": "kubectl_apply",
        "control_ref": "default/deployment/api",
        "env": "prod",
        "status": "active",
    }
    defaults.update(kwargs)
    component = ApplicationComponent(**defaults)
    db.add(component)
    db.commit()
    db.refresh(component)
    return component


class ExternalReleasePreviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.app = _seed_application(self.db)
        self.user = _make_user()

    def test_external_only_records_is_allowed_without_targets(self):
        payload = ApplicationReleasePayload(
            version="1.0.0",
            env="prod",
            release_type="external",
            status="success",
            external_url="https://ci.example.com/pipeline/1",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["targets"], [])
        self.assertEqual(plan["command_preview"], [])
        self.assertTrue(any(item["level"] == "info" for item in plan["checks"]))


class KubernetesReleasePreviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.app = _seed_application(self.db)
        self.user = _make_user()
        self.asset = _seed_host(self.db, instance_id="host-1", ip="10.0.0.1")
        _bind_host(self.db, self.app.app_id, self.asset)
        self.component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            resource_refs='["host:host-1"]',
        )

    def test_kubernetes_publish_generates_apply_command(self):
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="/opt/manifests/api.yaml",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertEqual(len(plan["targets"]), 1)
        self.assertEqual(plan["targets"][0]["resource_id"], "host-1")
        self.assertEqual(len(plan["command_preview"]), 1)
        self.assertIn("kubectl", plan["command_preview"][0])
        self.assertIn("apply -f", plan["command_preview"][0])
        self.assertIn("api.yaml", plan["command_preview"][0])

    def test_kubernetes_publish_without_manifest_is_blocked(self):
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertFalse(plan["allowed"])
        self.assertTrue(any(
            item["level"] == "error" and "manifest" in item["message"]
            for item in plan["checks"]
        ))

    def test_create_release_reuses_preview_plan_as_backend_guard(self):
        """真实执行前必须复用预检计划，避免前端绕过预检直接提交。"""
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="",
        )
        with self.assertRaises(Exception) as ctx:
            _ensure_release_plan_allowed(
                app=self.app,
                payload=payload,
                db=self.db,
                current_user=self.user,
            )
        self.assertIn("发布预检未通过", str(ctx.exception))

    def test_environment_mismatch_emits_warning(self):
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="test",  # 应用是 prod
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="/opt/manifests/api.yaml",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertTrue(plan["allowed"])
        self.assertTrue(any(item["level"] == "warning" and "环境" in item["message"] for item in plan["checks"]))

    def test_missing_ssh_credential_emits_warning(self):
        # 解除 SSH 凭证绑定。
        self.asset.credential_ref = ""
        self.db.commit()

        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="/opt/manifests/api.yaml",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertTrue(plan["allowed"])
        self.assertTrue(any(item["level"] == "warning" and "SSH 凭证" in item["message"] for item in plan["checks"]))

    def test_component_not_in_this_app_is_blocked(self):
        """跨应用运行实例必须拒绝。"""
        other_app = Application(
            app_id="APP-2",
            name="其它系统",
            environment="prod",
            status="active",
        )
        self.db.add(other_app)
        self.db.commit()

        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="kubernetes",
            component_id=self.component.component_id,
            artifact_ref="/opt/manifests/api.yaml",
        )
        plan = _plan_application_release(
            app=other_app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertFalse(plan["allowed"])
        self.assertTrue(any("必须属于当前应用" in item["message"] for item in plan["checks"]))


class RollbackPreviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.app = _seed_application(self.db)
        self.user = _make_user()
        self.asset = _seed_host(self.db, instance_id="host-1", ip="10.0.0.1")
        _bind_host(self.db, self.app.app_id, self.asset)
        self.component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            resource_refs='["host:host-1"]',
            deployment_type="kubernetes",
            control_ref="default/deployment/api",
        )
        # 一条可回滚的历史发布记录。
        self.db.add(
            ApplicationRelease(
                release_id="REL-OLD",
                app_id=self.app.app_id,
                component_id=self.component.component_id,
                component_name=self.component.name,
                version="5",
                env="prod",
                release_type="kubernetes",
                status="success",
            )
        )
        self.db.commit()

    def test_rollback_to_existing_release_is_allowed(self):
        payload = ApplicationReleasePayload(
            version="3.0.0",
            env="prod",
            release_type="rollback",
            component_id=self.component.component_id,
            rollback_release_id="REL-OLD",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertEqual(len(plan["command_preview"]), 1)
        self.assertIn("rollout undo", plan["command_preview"][0])
        self.assertIn("--to-revision=5", plan["command_preview"][0])

    def test_rollback_without_release_id_is_blocked(self):
        payload = ApplicationReleasePayload(
            version="3.0.0",
            env="prod",
            release_type="rollback",
            component_id=self.component.component_id,
            rollback_release_id="",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertFalse(plan["allowed"])

    def test_rollback_to_failed_release_is_blocked(self):
        self.db.add(
            ApplicationRelease(
                release_id="REL-FAIL",
                app_id=self.app.app_id,
                component_id=self.component.component_id,
                component_name=self.component.name,
                version="4",
                env="prod",
                release_type="kubernetes",
                status="failed",
            )
        )
        self.db.commit()

        payload = ApplicationReleasePayload(
            version="3.0.0",
            env="prod",
            release_type="rollback",
            component_id=self.component.component_id,
            rollback_release_id="REL-FAIL",
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertFalse(plan["allowed"])


class AnsibleReleasePreviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.app = _seed_application(self.db)
        self.user = _make_user()
        self.asset = _seed_host(self.db, instance_id="host-1", ip="10.0.0.1")
        _bind_host(self.db, self.app.app_id, self.asset)

    def test_ansible_target_outside_bound_hosts_is_blocked(self):
        payload = ApplicationReleasePayload(
            version="1.0.0",
            env="prod",
            release_type="ansible",
            template_id="ANSIBLE-ANY",
            target_resource_ids=["i-not-bound"],
        )
        plan = _plan_application_release(
            app=self.app,
            payload=payload,
            db=self.db,
            current_user=self.user,
        )
        self.assertFalse(plan["allowed"])
        self.assertTrue(any("绑定主机" in item["message"] for item in plan["checks"]))


class SingleRuntimeReleasePreviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self.app = _seed_application(self.db)
        self.user = _make_user()
        self.asset = _seed_host(self.db, instance_id="host-1", ip="10.0.0.1")
        _bind_host(self.db, self.app.app_id, self.asset)

    def test_systemd_release_generates_restart_plan(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="systemd",
            control_ref="mall-api.service",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="systemd",
            component_id=component.component_id,
            deploy_dir="/opt/apps/mall-api",
            artifact_ref="/opt/releases/mall-api.jar",
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertEqual("host-1", plan["targets"][0]["resource_id"])
        self.assertTrue(any("/opt/apps/mall-api/releases/mall-api.jar" in item for item in plan["command_preview"]))
        self.assertTrue(any("systemctl restart mall-api.service" in item for item in plan["command_preview"]))

    def test_docker_release_generates_pull_restart_plan(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="docker",
            control_ref="mall-api",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="docker",
            component_id=component.component_id,
            artifact_ref="registry.example.com/mall-api:2.0.0",
            docker_run_args="-p 8080:80 --restart always",
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertTrue(any("docker run -d --name mall-api -p 8080:80 --restart always registry.example.com/mall-api:2.0.0" in item for item in plan["command_preview"]))

    def test_docker_release_rejects_forbidden_run_args(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="docker",
            control_ref="mall-api",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="docker",
            component_id=component.component_id,
            artifact_ref="registry.example.com/mall-api:2.0.0",
            docker_run_args="--privileged",
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)
        self.assertFalse(plan["allowed"])
        self.assertTrue(any("不允许" in item["message"] for item in plan["checks"]))

    def test_supervisor_release_generates_program_plan(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="supervisor",
            control_ref="mall-api",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="supervisor",
            component_id=component.component_id,
            deploy_dir="/opt/apps/mall-api",
            artifact_ref="/opt/releases/mall-api.jar",
            supervisor_command="java -jar /opt/apps/mall-api/current",
            supervisor_user="www-data",
            supervisor_environment="APP_ENV=prod",
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertTrue(any("/etc/supervisor/conf.d/mall-api.conf" in item for item in plan["command_preview"]))
        self.assertTrue(any("supervisorctl restart mall-api" in item for item in plan["command_preview"]))

    def test_managed_supervisor_release_generates_managed_runtime_plan(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="supervisor",
            control_ref="mall-api",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="supervisor",
            component_id=component.component_id,
            deploy_dir="/opt/apps/mall-api",
            artifact_ref="/opt/releases/mall-api.jar",
            supervisor_runtime="managed",
            supervisor_command="java -jar /opt/apps/mall-api/current",
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)

        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertTrue(any("/opt/opspilot/supervisor/conf.d/mall-api.conf" in item for item in plan["command_preview"]))
        self.assertTrue(any("opspilot-supervisord.service" in item for item in plan["command_preview"]))

    def test_docker_compose_release_requires_one_bound_host_and_generates_plan(self):
        component = _seed_component(
            self.db,
            app_id=self.app.app_id,
            deployment_type="docker_compose",
            control_ref="compose=/opt/apps/mall-api/docker-compose.yml;service=api",
            resource_refs='["host:host-1"]',
        )
        payload = ApplicationReleasePayload(
            version="2.0.0",
            env="prod",
            release_type="docker_compose",
            component_id=component.component_id,
            deploy_dir="/opt/apps/mall-api",
            compose_filename="docker-compose.yml",
            compose_content="services:\n  api:\n    image: registry.example.com/mall-api:latest\n",
            service_name="api",
            pull_image=True,
            target_resource_ids=["host-1"],
        )
        plan = _plan_application_release(app=self.app, payload=payload, db=self.db, current_user=self.user)
        self.assertTrue(plan["allowed"], msg=plan["checks"])
        self.assertEqual("host-1", plan["targets"][0]["resource_id"])
        self.assertTrue(any("docker compose -f /opt/apps/mall-api/docker-compose.yml up -d api" in item for item in plan["command_preview"]))


if __name__ == "__main__":
    unittest.main()
