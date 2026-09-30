"""应用发布记录与发布预检服务。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.json_fields import normalize_json_list
from app.models.application import Application, ApplicationComponent, ApplicationRelease
from app.schemas.application import ApplicationReleasePayload
from app.services.application_time import format_app_datetime

APP_ENV_VALUES = {"prod", "test", "dev", "mixed"}
RELEASE_TYPES = {"external", "ansible", "systemd", "supervisor", "docker", "docker_compose", "kubernetes", "helm", "rollback"}
EXTERNAL_RELEASE_STATUS_VALUES = {"success", "failed", "canceled"}
MAX_RELEASE_OUTPUT_LENGTH = 4000


@dataclass(frozen=True)
class ApplicationReleasePlannerDeps:
    """发布预检依赖集合，用于避免 service 反向依赖路由模块。

    这些依赖全部是只读校验或纯校验函数；预检阶段不允许执行 SSH/Ansible。
    """

    bound_host_assets: Callable[[Session, str], dict[str, tuple[Any, str]]]
    build_runtime_release_module_args: Callable[[str, str, str, str], str]
    find_template_index: Callable[[list[dict[str, Any]], str], int]
    normalize_template_row: Callable[[dict[str, Any]], dict[str, Any]]
    read_ansible_templates: Callable[[Session], list[dict[str, Any]]]
    template_visible_to_user: Callable[[dict[str, Any], Any], bool]
    validate_compose_content: Callable[[str], str]
    validate_compose_deploy_path: Callable[[str], str]
    validate_compose_filename: Callable[[str], str]
    validate_compose_service_name: Callable[[str], str]
    validate_docker_image_ref: Callable[[str], str]
    validate_docker_run_args: Callable[[str], list[str]]
    validate_systemd_artifact_content_b64: Callable[[str], str]
    validate_systemd_artifact_filename: Callable[[str], str]
    validate_systemd_artifact_ref: Callable[[str], str]
    validate_supervisor_command: Callable[[str], str]
    validate_supervisor_environment: Callable[[str], str]
    validate_supervisor_program_name: Callable[[str], str]
    validate_supervisor_runtime: Callable[[str], str]
    validate_supervisor_user: Callable[[str], str]


def load_json_list(raw_value: str | None) -> list[str]:
    """安全解析 JSON 数组，兼容 ORM 已经返回 list 的新字段。"""
    return normalize_json_list(raw_value)


def serialize_release(row: ApplicationRelease, app_names: dict[str, str] | None = None) -> dict[str, Any]:
    """序列化发布记录。"""
    return {
        "release_id": row.release_id,
        "app_id": row.app_id,
        "app_name": (app_names or {}).get(str(row.app_id), ""),
        "component_id": row.component_id or "",
        "component_name": row.component_name or "",
        "version": row.version or "",
        "env": row.env or "prod",
        "release_type": row.release_type or ("ansible" if row.template_id else "external"),
        "external_url": row.external_url or "",
        "template_id": row.template_id or "",
        "template_name": row.template_name or "",
        "artifact_ref": row.artifact_ref or "",
        "rollback_release_id": row.rollback_release_id or "",
        "target_resource_ids": load_json_list(row.target_resource_ids),
        "target_names": load_json_list(row.target_names),
        "status": row.status or "pending",
        "operator": row.operator or "",
        "started_at": format_app_datetime(row.started_at),
        "finished_at": format_app_datetime(row.finished_at),
        "duration_ms": int(row.duration_ms or 0),
        "summary": row.summary or "",
        "output": row.output or "",
    }


def truncate_release_output(stdout: str, stderr: str) -> str:
    """合并并截断发布输出，防止数据库保存过大的敏感日志。"""
    text = "\n".join([part for part in [stdout.strip(), stderr.strip()] if part])
    return text[-MAX_RELEASE_OUTPUT_LENGTH:]


def plan_application_release(
    *,
    app: Application,
    payload: ApplicationReleasePayload,
    db: Session,
    current_user: Any,
    deps: ApplicationReleasePlannerDeps,
) -> dict[str, Any]:
    """发布计划器（只读）。

    架构：复用真实发布的前置校验，但只生成命令预览，不执行 Ansible / SSH。
    安全边界：
    - 不触发远程命令；
    - 不访问凭证明文（只查 SSH 凭证是否绑定）；
    - 目标主机限制在当前应用绑定资源；
    - 回滚只允许指向同应用同实例已成功的 K8s/Helm 记录。
    """
    checks: list[dict[str, str]] = []
    targets: list[dict[str, str]] = []
    command_preview: list[str] = []
    allowed = True

    def add_error(message: str) -> None:
        nonlocal allowed
        allowed = False
        checks.append({"level": "error", "message": message})

    def add_warning(message: str) -> None:
        checks.append({"level": "warning", "message": message})

    def add_info(message: str) -> None:
        checks.append({"level": "info", "message": message})

    release_type = (payload.release_type or "external").strip()
    version = (payload.version or "").strip()
    env = (payload.env or "prod").strip()
    component_id = (payload.component_id or "").strip()
    artifact_ref = (payload.artifact_ref or "").strip()
    rollback_release_id = (payload.rollback_release_id or "").strip()
    status_value = (payload.status or "success").strip()

    if release_type not in RELEASE_TYPES:
        add_error("发布方式仅支持 external/ansible/systemd/supervisor/docker/docker_compose/kubernetes/helm/rollback")
        return {
            "release_type": release_type or "external",
            "allowed": False,
            "targets": [],
            "command_preview": [],
            "checks": checks,
        }
    if not version:
        add_error("发布版本不能为空")
    if env not in APP_ENV_VALUES:
        add_error("发布环境仅支持 prod/test/dev/mixed")
    elif env != (app.environment or "prod") and (app.environment or "") not in {"mixed", ""}:
        add_warning(f"发布环境（{env}）与应用环境（{app.environment}）不一致，请确认目标是否正确")

    component: ApplicationComponent | None = None
    if component_id:
        component = (
            db.query(ApplicationComponent)
            .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == component_id)
            .first()
        )
        if not component:
            add_error("发布服务必须属于当前应用")
    if release_type not in {"external", "ansible"} and not component:
        add_error("平台执行发布必须选择运行实例")

    if release_type == "external":
        if status_value not in EXTERNAL_RELEASE_STATUS_VALUES:
            add_error("外部发布结果仅支持 success/failed/canceled")
        else:
            add_info("外部发布只登记结果与链接，OpsPilot 不执行任何远程命令")
    elif release_type == "ansible":
        _plan_ansible_release(payload, db, current_user, app.app_id, deps, add_error, add_warning, add_info, targets, command_preview)
    elif release_type in {"systemd", "supervisor", "docker"}:
        _plan_single_runtime_release(
            release_type=release_type,
            payload=payload,
            app=app,
            component=component,
            db=db,
            deps=deps,
            add_error=add_error,
            add_warning=add_warning,
            targets=targets,
            command_preview=command_preview,
        )
    elif release_type == "docker_compose":
        _plan_compose_release(payload, db, app.app_id, component, deps, add_error, add_warning, targets, command_preview)
    else:
        _plan_runtime_release(
            release_type=release_type,
            artifact_ref=artifact_ref,
            rollback_release_id=rollback_release_id,
            app=app,
            component=component,
            db=db,
            deps=deps,
            add_error=add_error,
            add_warning=add_warning,
            add_info=add_info,
            targets=targets,
            command_preview=command_preview,
            allowed=lambda: allowed,
        )

    return {
        "release_type": release_type,
        "allowed": allowed,
        "targets": targets,
        "command_preview": command_preview,
        "checks": checks,
    }


def _selected_resource_ids(raw_ids: list[str]) -> list[str]:
    """归一化前端提交的目标资源 ID。"""
    return list(dict.fromkeys([str(item or "").strip() for item in raw_ids if str(item or "").strip()]))


def _plan_ansible_release(
    payload: ApplicationReleasePayload,
    db: Session,
    current_user: Any,
    app_id: str,
    deps: ApplicationReleasePlannerDeps,
    add_error: Callable[[str], None],
    add_warning: Callable[[str], None],
    add_info: Callable[[str], None],
    targets: list[dict[str, str]],
    command_preview: list[str],
) -> None:
    """生成 Ansible 发布预检计划。"""
    template_id = (payload.template_id or "").strip()
    if not template_id:
        add_error("请选择批量模板")
    bound_hosts = deps.bound_host_assets(db, app_id)
    selected_resource_ids = _selected_resource_ids(payload.target_resource_ids)
    if not selected_resource_ids:
        add_error("请选择至少一台当前应用绑定主机")
    unknown_targets = [item for item in selected_resource_ids if item not in bound_hosts]
    if unknown_targets:
        add_error(f"发布目标必须来自当前应用绑定主机：{', '.join(unknown_targets[:3])}")
    if template_id:
        templates = deps.read_ansible_templates(db)
        index = deps.find_template_index(templates, template_id)
        if index < 0:
            add_error("批量模板不存在或已删除")
        else:
            template = deps.normalize_template_row(templates[index])
            if not deps.template_visible_to_user(template, current_user):
                add_error("当前权限组无权使用该批量模板")
            else:
                add_info(f"批量模板：{template.get('name') or template_id}")
                command_preview.append(f"ansible-playbook 执行模板 {template.get('name') or template_id}")
    for resource_id in selected_resource_ids:
        if resource_id not in bound_hosts:
            continue
        asset, name = bound_hosts[resource_id]
        targets.append({"resource_id": resource_id, "name": name, "ip": asset.ip or ""})
        if not asset.credential_ref:
            add_warning(f"主机 {name} 未绑定 SSH 凭证，执行时可能失败")


def _plan_compose_release(
    payload: ApplicationReleasePayload,
    db: Session,
    app_id: str,
    component: ApplicationComponent | None,
    deps: ApplicationReleasePlannerDeps,
    add_error: Callable[[str], None],
    add_warning: Callable[[str], None],
    targets: list[dict[str, str]],
    command_preview: list[str],
) -> None:
    """生成 Docker Compose 标准部署预检计划。"""
    if component and component.deployment_type != "docker_compose":
        add_error("Docker Compose 标准部署必须选择 Compose 运行实例")
    try:
        deploy_dir = deps.validate_compose_deploy_path(payload.deploy_dir)
        compose_filename = deps.validate_compose_filename(payload.compose_filename)
        compose_content = deps.validate_compose_content(payload.compose_content)
        service_name = deps.validate_compose_service_name(payload.service_name)
    except HTTPException as exc:
        add_error(str(exc.detail))
        deploy_dir = ""
        compose_filename = ""
        service_name = ""
        compose_content = ""
    bound_hosts = deps.bound_host_assets(db, app_id)
    selected_resource_ids = _selected_resource_ids(payload.target_resource_ids)
    if len(selected_resource_ids) != 1:
        add_error("Docker Compose 标准部署需要且只能选择一台当前应用绑定主机")
    elif selected_resource_ids[0] not in bound_hosts:
        add_error("发布目标必须来自当前应用绑定主机")
    else:
        resource_id = selected_resource_ids[0]
        asset, name = bound_hosts[resource_id]
        targets.append({"resource_id": resource_id, "name": name, "ip": asset.ip or ""})
        if not asset.credential_ref:
            add_warning(f"主机 {name} 未绑定 SSH 凭证，执行时可能失败")
    if deploy_dir and compose_filename:
        compose_path = f"{deploy_dir.rstrip('/')}/{compose_filename}"
        command_preview.append(f"写入 {compose_path}")
        if payload.pull_image:
            command_preview.append(f"docker compose -f {compose_path} pull" + (f" {service_name}" if service_name else ""))
        command_preview.append(f"docker compose -f {compose_path} up -d" + (f" {service_name}" if service_name else ""))


def _plan_single_runtime_release(
    *,
    release_type: str,
    payload: ApplicationReleasePayload,
    app: Application,
    component: ApplicationComponent | None,
    db: Session,
    deps: ApplicationReleasePlannerDeps,
    add_error: Callable[[str], None],
    add_warning: Callable[[str], None],
    targets: list[dict[str, str]],
    command_preview: list[str],
) -> None:
    """生成 systemd / supervisor / docker 单机运行实例发布预检计划。"""
    if not component:
        return
    component_type = component.deployment_type or ""
    expected_types = {"systemd", "binary"} if release_type == "systemd" else {release_type}
    if component_type not in expected_types:
        add_error(f"{release_type} 发布必须选择 {release_type} 运行实例")
    bound_hosts = deps.bound_host_assets(db, app.app_id)
    selected_resource_ids = [
        ref.split(":", 1)[1].strip()
        for ref in load_json_list(component.resource_refs)
        if ref.startswith("host:") and ref.split(":", 1)[1].strip()
    ]
    selected_resource_ids = list(dict.fromkeys(selected_resource_ids))
    if len(selected_resource_ids) != 1:
        add_error("systemd/supervisor/docker 发布需要且只能绑定一台执行主机")
        return
    if selected_resource_ids[0] not in bound_hosts:
        add_error("运行实例引用了未绑定或不可用的主机资源")
        return
    asset, name = bound_hosts[selected_resource_ids[0]]
    targets.append({"resource_id": selected_resource_ids[0], "name": name, "ip": asset.ip or ""})
    if not asset.credential_ref:
        add_warning(f"主机 {name} 未绑定 SSH 凭证，执行时可能失败")
    if release_type == "systemd":
        try:
            deploy_dir = deps.validate_compose_deploy_path(payload.deploy_dir)
            artifact_ref = deps.validate_systemd_artifact_ref(payload.artifact_ref)
            artifact_b64 = deps.validate_systemd_artifact_content_b64(getattr(payload, "artifact_content_b64", ""))
            raw_filename = getattr(payload, "artifact_filename", "")
            if not raw_filename and artifact_ref:
                raw_filename = artifact_ref.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            artifact_filename = deps.validate_systemd_artifact_filename(raw_filename)
            if not artifact_ref and not artifact_b64:
                add_error("systemd 发布必须提供制品路径、URL 或上传文件")
        except HTTPException as exc:
            add_error(str(exc.detail))
            deploy_dir = ""
            artifact_filename = ""
        command_preview.extend([
            f"写入制品到 {deploy_dir.rstrip('/')}/releases/{artifact_filename or '<artifact>'}",
            f"ln -sfn {deploy_dir.rstrip('/')}/releases/{artifact_filename or '<artifact>'} {deploy_dir.rstrip('/')}/current",
            "systemctl daemon-reload",
            f"systemctl restart {component.control_ref}",
            f"systemctl status --no-pager {component.control_ref}",
        ])
    elif release_type == "supervisor":
        try:
            program_name = deps.validate_supervisor_program_name(component.control_ref or "")
            deploy_dir = deps.validate_compose_deploy_path(payload.deploy_dir)
            command = deps.validate_supervisor_command(getattr(payload, "supervisor_command", ""))
            deps.validate_supervisor_user(getattr(payload, "supervisor_user", ""))
            deps.validate_supervisor_environment(getattr(payload, "supervisor_environment", ""))
            supervisor_runtime = deps.validate_supervisor_runtime(getattr(payload, "supervisor_runtime", "system"))
            artifact_ref = deps.validate_systemd_artifact_ref(payload.artifact_ref)
            artifact_b64 = deps.validate_systemd_artifact_content_b64(getattr(payload, "artifact_content_b64", ""))
            raw_filename = getattr(payload, "artifact_filename", "")
            if not raw_filename and artifact_ref:
                raw_filename = artifact_ref.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            artifact_filename = deps.validate_systemd_artifact_filename(raw_filename)
            if not artifact_ref and not artifact_b64:
                add_error("Supervisor 发布必须提供制品路径、URL 或上传文件")
        except HTTPException as exc:
            add_error(str(exc.detail))
            program_name = component.control_ref or "<program>"
            deploy_dir = ""
            artifact_filename = ""
            command = ""
            supervisor_runtime = "system"
        supervisor_conf_path = (
            f"/opt/opspilot/supervisor/conf.d/{program_name}.conf"
            if supervisor_runtime == "managed"
            else f"/etc/supervisor/conf.d/{program_name}.conf"
        )
        supervisorctl = (
            "supervisorctl -c /opt/opspilot/supervisor/supervisord.conf"
            if supervisor_runtime == "managed"
            else "supervisorctl"
        )
        if supervisor_runtime == "managed":
            command_preview.extend([
                "初始化 /opt/opspilot/supervisor 托管运行时",
                "systemctl enable --now opspilot-supervisord.service",
            ])
        command_preview.extend([
            f"写入制品到 {deploy_dir.rstrip('/')}/releases/{artifact_filename or '<artifact>'}",
            f"写入 {supervisor_conf_path}",
            f"{supervisorctl} reread",
            f"{supervisorctl} update",
            f"{supervisorctl} restart {program_name}",
            f"{supervisorctl} status {program_name}",
            f"command={command or '<command>'}",
        ])
    else:
        try:
            image_ref = deps.validate_docker_image_ref(payload.artifact_ref)
            docker_run_args = deps.validate_docker_run_args(getattr(payload, "docker_run_args", ""))
        except HTTPException as exc:
            add_error(str(exc.detail))
            image_ref = ""
            docker_run_args = []
        command_preview.extend([
            f"docker pull {image_ref or '<image>'}",
            f"docker stop {component.control_ref}",
            f"docker rename {component.control_ref} {component.control_ref}.opspilot.bak",
            f"docker run -d --name {component.control_ref}" + (f" {' '.join(docker_run_args)}" if docker_run_args else "") + f" {image_ref or '<image>'}",
        ])


def _plan_runtime_release(
    *,
    release_type: str,
    artifact_ref: str,
    rollback_release_id: str,
    app: Application,
    component: ApplicationComponent | None,
    db: Session,
    deps: ApplicationReleasePlannerDeps,
    add_error: Callable[[str], None],
    add_warning: Callable[[str], None],
    add_info: Callable[[str], None],
    targets: list[dict[str, str]],
    command_preview: list[str],
    allowed: Callable[[], bool],
) -> None:
    """生成 K8s/Helm 发布或回滚预检计划。"""
    if component is None:
        return
    component_type = component.deployment_type or ""
    if release_type == "kubernetes" and component_type not in {"kubernetes", "kubernetes_yaml"}:
        add_error("Kubernetes 发布必须选择 Kubernetes 运行实例")
    if release_type == "helm" and component_type != "helm":
        add_error("Helm 发布必须选择 Helm 运行实例")

    runtime_type = "kubernetes" if component_type == "kubernetes_yaml" else component_type
    publish_ref = artifact_ref
    action_name = "publish"
    if release_type == "rollback":
        if not rollback_release_id:
            add_error("请选择要回滚到的历史发布记录")
        else:
            source_release = (
                db.query(ApplicationRelease)
                .filter(
                    ApplicationRelease.app_id == app.app_id,
                    ApplicationRelease.release_id == rollback_release_id,
                    ApplicationRelease.component_id == component.component_id,
                    ApplicationRelease.status == "success",
                )
                .first()
            )
            if not source_release or (source_release.release_type or "") not in {"kubernetes", "helm"}:
                add_error("只能回滚到当前运行实例已成功的 K8s/Helm 发布记录")
            else:
                runtime_type = source_release.release_type or runtime_type
                publish_ref = str(source_release.version or "").strip()
                action_name = "rollback"
                add_info(f"回滚到 {rollback_release_id} · 版本 {publish_ref}")

    if allowed():
        try:
            command_preview.append(
                deps.build_runtime_release_module_args(runtime_type, component.control_ref or "", publish_ref, action_name)
            )
        except HTTPException as exc:
            add_error(str(exc.detail))

    bound_hosts = deps.bound_host_assets(db, app.app_id)
    instance_host_ids = [
        ref.split(":", 1)[1].strip()
        for ref in load_json_list(component.resource_refs)
        if ref.startswith("host:") and ref.split(":", 1)[1].strip()
    ]
    instance_host_ids = list(dict.fromkeys(instance_host_ids))
    if len(instance_host_ids) != 1:
        add_error("K8s/Helm 发布需要且只能绑定一台执行主机")
    elif instance_host_ids[0] not in bound_hosts:
        add_error("运行实例引用了未绑定或不可用的主机资源")
    else:
        asset, name = bound_hosts[instance_host_ids[0]]
        targets.append({"resource_id": instance_host_ids[0], "name": name, "ip": asset.ip or ""})
        if not asset.credential_ref:
            add_warning(f"主机 {name} 未绑定 SSH 凭证，执行时可能失败")
