"""应用中心路由公共依赖、校验与序列化工具。"""
from __future__ import annotations

import base64
import json
import posixpath
import re
import shlex
import uuid
from datetime import datetime, timedelta
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
import yaml

from app.core.rbac import list_role_templates
from app.core.json_fields import dump_json_list, normalize_json_list
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationConfig,
    ApplicationConfigRevision,
    ApplicationHealthSnapshot,
    ApplicationLink,
    ApplicationRelease,
    ApplicationResource,
)
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.user import User
from app.models.vm import VirtualMachine
from app.schemas.application import (
    AnsibleTemplateOption,
    ApplicationComponentBatchDeleteRequest,
    ApplicationComponentPayload,
    ApplicationComponentResponse,
    ApplicationComposePreflightRequest,
    ApplicationComposePreflightResponse,
    ApplicationConfigImportItemResult,
    ApplicationConfigImportPayload,
    ApplicationConfigImportResponse,
    ApplicationConfigPayload,
    ApplicationConfigResponse,
    ApplicationConfigRevisionResponse,
    ApplicationDetailResponse,
    ApplicationHealthSnapshotItem,
    ApplicationHealthTimelineResponse,
    ApplicationLinkPayload,
    ApplicationLinkResponse,
    ApplicationObservabilityContext,
    ApplicationOverviewResponse,
    ApplicationPayload,
    ApplicationReleasePayload,
    ApplicationReleasePreviewCheck,
    ApplicationReleasePreviewResponse,
    ApplicationReleasePreviewTarget,
    ApplicationReleaseResponse,
    ApplicationResourcePayload,
    ApplicationResourceResponse,
    ApplicationResponse,
    ApplicationRuntimeActionPayload,
    ApplicationRuntimeActionResponse,
    ApplicationRuntimeDiscoveryCandidate,
    ApplicationRuntimeDiscoveryRequest,
    ApplicationRuntimeDiscoveryResponse,
    ApplicationRuntimeInstancePayload,
    ApplicationRuntimeInstanceResponse,
    ApplicationTimelineEvent,
    ApplicationTimelineResponse,
    CredentialRefOption,
    ResourceOption,
    RoleOption,
)
from app.services.application_config_import import (
    CONFIG_IMPORT_KEY_PATTERN,
    CONFIG_IMPORT_SENSITIVE_PATTERN,
    MAX_CONFIG_IMPORT_CONTENT_LENGTH,
    MAX_CONFIG_IMPORT_LINES,
    parse_env_import_content as _parse_env_import_content,
)
from app.services.application_health import (
    APPLICATION_HEALTH_SNAPSHOT_INTERVAL_SECONDS,
    APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS,
    active_alert_count_for_hosts as _active_alert_count_for_hosts,
    build_application_health as _build_application_health,
    compute_application_health_snapshot as _compute_application_health_snapshot,
    run_application_health_snapshot_cycle,
    start_application_health_snapshot_runner,
)
from app.services.application_timeline import (
    application_alert_timeline_events as _application_alert_timeline_events,
    build_application_timeline,
    config_revision_timeline_event as _config_revision_timeline_event,
    normalize_alert_time as _normalize_alert_time,
    release_timeline_event as _release_timeline_event,
    runtime_control_timeline_event as _runtime_control_timeline_event,
)
from app.services.application_releases import (
    ApplicationReleasePlannerDeps,
    plan_application_release,
    serialize_release as _serialize_release,
    truncate_release_output as _truncate_release_output,
)
from app.services.application_runtime import (
    build_compose_control_ref as _build_compose_control_ref,
    build_compose_update_module_args as _build_compose_update_module_args,
    build_docker_container_release_module_args as _build_docker_container_release_module_args,
    build_runtime_control_module_args as _build_runtime_control_module_args,
    build_runtime_discovery_command as _build_runtime_discovery_command,
    build_runtime_release_module_args as _build_runtime_release_module_args,
    build_runtime_update_module_args as _runtime_build_update_module_args,
    build_supervisor_artifact_release_module_args as _build_supervisor_artifact_release_module_args,
    build_systemd_artifact_release_module_args as _build_systemd_artifact_release_module_args,
    compose_file_path as _compose_file_path,
    derive_runtime_status as _derive_runtime_status,
    parse_compose_control_ref as _parse_compose_control_ref,
    parse_compose_discovery_output as _parse_compose_discovery_output,
    parse_docker_discovery_output as _parse_docker_discovery_output,
    parse_helm_control_ref as _parse_helm_control_ref,
    parse_kubernetes_control_ref as _parse_kubernetes_control_ref,
    parse_runtime_discovery_output as _parse_runtime_discovery_output,
    is_system_runtime_service as _is_system_runtime_service,
    parse_supervisor_discovery_output as _parse_supervisor_discovery_output,
    parse_systemd_discovery_output as _parse_systemd_discovery_output,
    resolve_runtime_execution_channel as _resolve_runtime_execution_channel,
    runtime_status_from_text as _runtime_status_from_text,
    service_name_from_unit as _service_name_from_unit,
    shell_quote as _shell_quote,
    truncate_runtime_control_output as _truncate_runtime_control_output,
    validate_compose_content as _validate_compose_content,
    validate_compose_deploy_path as _validate_compose_deploy_path,
    validate_compose_file_path as _validate_compose_file_path,
    validate_compose_filename as _validate_compose_filename,
    validate_compose_service_name as _validate_compose_service_name,
    validate_docker_image_ref as _validate_docker_image_ref,
    validate_docker_run_args as _validate_docker_run_args,
    validate_env_content as _validate_env_content,
    validate_env_file_path as _validate_env_file_path,
    validate_env_filename as _validate_env_filename,
    validate_systemd_artifact_content_b64 as _validate_systemd_artifact_content_b64,
    validate_systemd_artifact_filename as _validate_systemd_artifact_filename,
    validate_systemd_artifact_ref as _validate_systemd_artifact_ref,
    validate_supervisor_command as _validate_supervisor_command,
    validate_supervisor_environment as _validate_supervisor_environment,
    validate_supervisor_program_name as _validate_supervisor_program_name,
    validate_supervisor_runtime as _validate_supervisor_runtime,
    validate_supervisor_user as _validate_supervisor_user,
)
from app.services.application_time import format_app_datetime, now_dt, parse_app_datetime
from app.services.credential_access import ensure_credential_access, filter_credentials_for_user
from app.api.assets import decode_asset_password, get_credential_content as get_asset_credential_content, run_ssh_command
from app.api.tools import (
    AnsiblePlaybookRunRequest,
    AnsibleRunRequest,
    AnsibleRunResponse,
    AnsibleTemplateRunRequest,
    _check_ansible_available,
    _execute_ansible_task,
    _asset_identity as _ansible_asset_identity,
    _find_template_index,
    _normalize_template_row,
    _read_ansible_config,
    _read_ansible_templates,
    _template_visible_to_user,
    run_ansible_playbook,
    run_ansible_template,
)
from .runtime_executor import (
    BackgroundRequestContext,
    _append_runtime_control_audit_log,
    _execute_runtime_ssh_task,
)
from .permissions import (
    APP_ACTION_PERMISSION_MAP,
    APP_ACTION_ROLE_FIELD_MAP,
    _application_action_allowed,
    _application_action_role_allowed,
    _application_capabilities,
    _application_visible_to_user,
    _can_bypass_application_scope,
    _can_manage_apps,
    _ensure_application_action,
    _validate_roles,
)
from .validators import (
    _validate_payload,
    _validate_url,
)

APP_STATUS_VALUES = {"active", "maintenance", "offline"}
APP_ENV_VALUES = {"prod", "test", "dev", "mixed"}
APP_ENTRY_TYPES = {"external", "internal", "grafana", "tool"}
RESOURCE_TYPES = {"host", "vm", "domain", "certificate"}
COMPONENT_TYPES = {"frontend", "backend", "gateway", "worker", "other"}
DEPLOYMENT_TYPES = {
    "systemd",
    "supervisor",
    "docker",
    "docker_compose",
    "kubernetes",
    "helm",
    "external",
    # 历史兼容值：旧版本用 binary/kubernetes_yaml/static_site 表达执行方式。
    "binary",
    "kubernetes_yaml",
    "static_site",
}
RUNTIME_INSTANCE_TYPES = {"systemd", "supervisor", "docker", "docker_compose", "kubernetes", "helm", "external"}
RUNTIME_CONTROL_ACTIONS = {"start", "stop", "restart", "status"}
RUNTIME_CONTROL_SUPPORTED_TYPES = {"systemd", "supervisor", "docker", "docker_compose"}
RUNTIME_STATUS_ONLY_TYPES = {"kubernetes", "helm"}
RUNTIME_EXECUTION_CHANNELS = {"auto", "ansible", "ssh_shell"}
RUNTIME_DISCOVERY_TYPES = {"systemd", "supervisor", "docker", "docker_compose"}
CONTROL_METHODS = {"systemd", "supervisor", "docker", "docker_compose", "kubectl_apply", "helm", "ansible", "manual"}
DEPLOYMENT_CONTROL_METHOD_MAP = {
    "binary": "systemd",
    "systemd": "systemd",
    "supervisor": "supervisor",
    "docker": "docker",
    "docker_compose": "docker_compose",
    "kubernetes_yaml": "kubectl_apply",
    "kubernetes": "kubectl_apply",
    "helm": "helm",
    "static_site": "ansible",
    "external": "manual",
}
CONFIG_VALUE_TYPES = {"plain", "credential_ref", "file"}
CONFIG_CHANGE_TYPES = {"create", "update", "delete"}
RELEASE_STATUS_VALUES = {"pending", "running", "success", "failed", "canceled"}
RELEASE_TYPES = {"external", "ansible", "systemd", "supervisor", "docker", "docker_compose", "kubernetes", "helm", "rollback"}
EXTERNAL_RELEASE_STATUS_VALUES = {"success", "failed", "canceled"}
APPLICATION_HEALTH_VALUES = {"healthy", "warning", "degraded", "offline", "unknown"}
MAX_RELEASE_OUTPUT_LENGTH = 4000
MAX_RUNTIME_CONTROL_OUTPUT_LENGTH = 4000
MAX_RUNTIME_DISCOVERY_CANDIDATES = 200
MAX_COMPOSE_CONTENT_LENGTH = 20000
MAX_CONFIG_PLAIN_VALUE_LENGTH = 2000
MAX_CONFIG_FILE_VALUE_LENGTH = 20000
BUILTIN_DOCKER_COMPOSE_TEMPLATE_ID = "builtin/docker-compose-deploy"
BUILTIN_DOCKER_COMPOSE_TEMPLATE_NAME = "系统内置：Docker Compose 标准部署"
def _now_str() -> datetime:
    """返回应用中心统一的 DateTime 值。"""
    return now_dt()


def _load_json_list(raw_value: str | None) -> list[str]:
    """安全解析 JSON 数组，兼容 ORM 已经返回 list 的新字段。"""
    return normalize_json_list(raw_value)


def _dump_json_list(items: list[str] | None) -> str:
    """把字符串列表持久化为规范 JSON 字符串。"""
    return dump_json_list(items)


def _serialize_application(app: Application, db: Session) -> dict[str, Any]:
    """统一序列化应用列表字段。"""
    resources = db.query(ApplicationResource).filter(ApplicationResource.app_id == app.app_id).all()
    components = db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app.app_id).all()
    link_count = db.query(ApplicationLink.id).filter(ApplicationLink.app_id == app.app_id).count()
    host_lookup = _host_asset_lookup(db)
    host_health_values = []
    for resource in resources:
        if resource.resource_type != "host":
            continue
        asset = host_lookup.get(str(resource.resource_id or "").strip())
        host_health_values.append(str((asset.type if asset else "") or (asset.status if asset else "") or "unknown"))

    domain_ids = [int(row.resource_id) for row in resources if row.resource_type == "domain" and str(row.resource_id).isdigit()]
    domains = db.query(DomainRecord).filter(DomainRecord.id.in_(domain_ids)).all() if domain_ids else []
    cert_ids = [str(row.resource_id) for row in resources if row.resource_type == "certificate" and str(row.resource_id).strip()]
    certs = db.query(CertificateRecord).filter(CertificateRecord.cert_id.in_(cert_ids)).all() if cert_ids else []
    health = _build_application_health(
        app_status=app.status or "active",
        component_statuses=[row.status or "" for row in components],
        host_health_values=host_health_values,
        domain_expiring_count=len([item for item in domains if _is_expiring_soon(item.expires_at)]),
        certificate_expiring_count=len([item for item in certs if _is_expiring_soon(item.not_after)]),
    )
    return {
        "app_id": app.app_id,
        "name": app.name or "",
        "description": app.description or "",
        "category": app.category or "业务应用",
        "owner": app.owner or "",
        "status": app.status or "active",
        "environment": app.environment or "prod",
        "run_mode": app.run_mode or "external",
        "entry_type": app.entry_type or "external",
        "entry_url": app.entry_url or "",
        "tags": _load_json_list(app.tags),
        "allowed_roles": _load_json_list(app.allowed_roles),
        "deploy_roles": _load_json_list(app.deploy_roles),
        "control_roles": _load_json_list(app.control_roles),
        "config_roles": _load_json_list(app.config_roles),
        "remark": app.remark or "",
        "health_status": health["health_status"],
        "health_label": health["health_label"],
        "health_reasons": health["health_reasons"],
        "resource_count": len(resources),
        "component_count": len(components),
        "link_count": link_count,
        "created_at": format_app_datetime(app.created_at),
        "updated_at": format_app_datetime(app.updated_at),
    }


def _serialize_application_from_groups(
    app: Application,
    resources: list[ApplicationResource],
    components: list[ApplicationComponent],
    link_count: int,
    host_lookup: dict[str, Asset],
    domain_lookup: dict[int, DomainRecord],
    cert_lookup: dict[str, CertificateRecord],
) -> dict[str, Any]:
    """用批量预取结果序列化应用列表项，避免列表页 N+1 查询。"""
    host_health_values = []
    for resource in resources:
        if resource.resource_type != "host":
            continue
        asset = host_lookup.get(str(resource.resource_id or "").strip())
        host_health_values.append(str((asset.type if asset else "") or (asset.status if asset else "") or "unknown"))

    domain_ids = [int(row.resource_id) for row in resources if row.resource_type == "domain" and str(row.resource_id).isdigit()]
    cert_ids = [str(row.resource_id) for row in resources if row.resource_type == "certificate" and str(row.resource_id).strip()]
    health = _build_application_health(
        app_status=app.status or "active",
        component_statuses=[row.status or "" for row in components],
        host_health_values=host_health_values,
        domain_expiring_count=len([domain_lookup[item] for item in domain_ids if item in domain_lookup and _is_expiring_soon(domain_lookup[item].expires_at)]),
        certificate_expiring_count=len([cert_lookup[item] for item in cert_ids if item in cert_lookup and _is_expiring_soon(cert_lookup[item].not_after)]),
    )
    return {
        "app_id": app.app_id,
        "name": app.name or "",
        "description": app.description or "",
        "category": app.category or "业务应用",
        "owner": app.owner or "",
        "status": app.status or "active",
        "environment": app.environment or "prod",
        "run_mode": app.run_mode or "external",
        "entry_type": app.entry_type or "external",
        "entry_url": app.entry_url or "",
        "tags": _load_json_list(app.tags),
        "allowed_roles": _load_json_list(app.allowed_roles),
        "deploy_roles": _load_json_list(app.deploy_roles),
        "control_roles": _load_json_list(app.control_roles),
        "config_roles": _load_json_list(app.config_roles),
        "remark": app.remark or "",
        "health_status": health["health_status"],
        "health_label": health["health_label"],
        "health_reasons": health["health_reasons"],
        "resource_count": len(resources),
        "component_count": len(components),
        "link_count": link_count,
        "created_at": format_app_datetime(app.created_at),
        "updated_at": format_app_datetime(app.updated_at),
    }


def _serialize_applications_bulk(apps: list[Application], db: Session) -> list[dict[str, Any]]:
    """批量序列化应用列表，避免每个应用单独查询关联表。"""
    app_ids = [str(app.app_id) for app in apps if str(app.app_id or "").strip()]
    if not app_ids:
        return []

    resources_by_app: dict[str, list[ApplicationResource]] = {app_id: [] for app_id in app_ids}
    components_by_app: dict[str, list[ApplicationComponent]] = {app_id: [] for app_id in app_ids}
    for row in db.query(ApplicationResource).filter(ApplicationResource.app_id.in_(app_ids)).all():
        resources_by_app.setdefault(str(row.app_id), []).append(row)
    for row in db.query(ApplicationComponent).filter(ApplicationComponent.app_id.in_(app_ids)).all():
        components_by_app.setdefault(str(row.app_id), []).append(row)

    link_counts = {
        str(app_id): int(count or 0)
        for app_id, count in (
            db.query(ApplicationLink.app_id, func.count(ApplicationLink.id))
            .filter(ApplicationLink.app_id.in_(app_ids))
            .group_by(ApplicationLink.app_id)
            .all()
        )
    }

    host_lookup = _host_asset_lookup(db)
    domain_ids: set[int] = set()
    cert_ids: set[str] = set()
    for resources in resources_by_app.values():
        for resource in resources:
            if resource.resource_type == "domain" and str(resource.resource_id).isdigit():
                domain_ids.add(int(resource.resource_id))
            elif resource.resource_type == "certificate" and str(resource.resource_id).strip():
                cert_ids.add(str(resource.resource_id))
    domain_lookup = {
        int(row.id): row
        for row in (db.query(DomainRecord).filter(DomainRecord.id.in_(domain_ids)).all() if domain_ids else [])
    }
    cert_lookup = {
        str(row.cert_id): row
        for row in (db.query(CertificateRecord).filter(CertificateRecord.cert_id.in_(cert_ids)).all() if cert_ids else [])
    }

    return [
        _serialize_application_from_groups(
            app=app,
            resources=resources_by_app.get(str(app.app_id), []),
            components=components_by_app.get(str(app.app_id), []),
            link_count=link_counts.get(str(app.app_id), 0),
            host_lookup=host_lookup,
            domain_lookup=domain_lookup,
            cert_lookup=cert_lookup,
        )
        for app in apps
    ]


def _unique_non_empty(items: list[str]) -> list[str]:
    """按出现顺序去重并移除空值。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _row_value(row: Any, key: str, default: str = "") -> str:
    """兼容 SQLAlchemy 行对象和测试中的轻量对象。"""
    return str(getattr(row, key, default) or "").strip()


def _build_application_observability_context(
    app_id: str,
    app_name: str,
    resources: list[Any],
    components: list[Any],
    db: Session | None = None,
    host_lookup: dict[str, Any] | None = None,
    domain_lookup: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从应用绑定资源生成监控/日志跳转上下文。

    安全边界：
    1. 上下文只来自当前应用已绑定资源与运行实例；
    2. 不返回监控组件内网地址，不返回凭据；
    3. 前端只拿这些字段构造过滤条件，真正数据查询仍走 OpsPilot 后端。
    """
    hosts = host_lookup if host_lookup is not None else (_host_asset_lookup(db) if db is not None else {})
    domains = domain_lookup
    if domains is None and db is not None:
        domain_ids = [
            int(_row_value(row, "resource_id"))
            for row in resources
            if _row_value(row, "resource_type") == "domain" and _row_value(row, "resource_id").isdigit()
        ]
        domain_rows = db.query(DomainRecord).filter(DomainRecord.id.in_(domain_ids)).all() if domain_ids else []
        domains = {str(row.id): str(row.domain or "") for row in domain_rows}
    domains = domains or {}

    host_ids: list[str] = []
    host_ips: list[str] = []
    host_names: list[str] = []
    domain_names: list[str] = []
    certificate_ids: list[str] = []

    for row in resources:
        resource_type = _row_value(row, "resource_type")
        resource_id = _row_value(row, "resource_id")
        resource_name = _row_value(row, "resource_name", resource_id)
        if resource_type == "host":
            asset = hosts.get(resource_id)
            host_ids.append(str(getattr(asset, "instance_id", "") or resource_id))
            host_ips.append(str(getattr(asset, "ip", "") or ""))
            host_names.append(str(getattr(asset, "name", "") or resource_name or resource_id))
        elif resource_type == "domain":
            domain_names.append(domains.get(resource_id) or resource_name or resource_id)
        elif resource_type == "certificate":
            certificate_ids.append(resource_id)

    return {
        "app_id": app_id,
        "app_name": app_name,
        "host_ids": _unique_non_empty(host_ids),
        "host_ips": _unique_non_empty(host_ips),
        "host_names": _unique_non_empty(host_names),
        "runtime_instances": _unique_non_empty([_row_value(row, "name") for row in components]),
        "domains": _unique_non_empty(domain_names),
        "certificate_ids": _unique_non_empty(certificate_ids),
    }


def _resource_ref_key(resource_type: str, resource_id: str) -> str:
    """生成服务资源引用键，前后端统一使用该格式。"""
    return f"{str(resource_type or '').strip()}:{str(resource_id or '').strip()}"


def _allowed_resource_refs(db: Session, app_id: str) -> set[str]:
    """返回当前项目已绑定资源的可引用键集合。"""
    rows = db.query(ApplicationResource).filter(ApplicationResource.app_id == app_id).all()
    return {_resource_ref_key(row.resource_type, row.resource_id) for row in rows}


def _validate_component_payload(
    payload: ApplicationComponentPayload,
    db: Session,
    app_id: str,
) -> dict[str, Any]:
    """校验并归一化应用服务。

    资源引用必须先存在于项目资源绑定中，避免用户借服务层绕过资源边界。
    """
    name = (payload.name or "").strip()
    component_type = (payload.component_type or "backend").strip()
    deployment_type = (payload.deployment_type or "binary").strip()
    env = (payload.env or "prod").strip()
    status = (payload.status or "active").strip()
    if not name:
        raise HTTPException(status_code=400, detail="服务名称不能为空")
    if component_type not in COMPONENT_TYPES:
        raise HTTPException(status_code=400, detail="服务角色不支持")
    if deployment_type not in DEPLOYMENT_TYPES:
        raise HTTPException(status_code=400, detail="运行方式不支持")
    # 管控执行器不再由前端选择，避免用户伪造执行方式；后续发布/启停统一按运行方式派生。
    control_method = DEPLOYMENT_CONTROL_METHOD_MAP.get(deployment_type, "manual")
    execution_channel = (payload.execution_channel or "auto").strip()
    if env not in APP_ENV_VALUES:
        raise HTTPException(status_code=400, detail="服务环境仅支持 prod/test/dev/mixed")
    if status not in APP_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="服务状态仅支持 active/maintenance/offline")
    if execution_channel not in RUNTIME_EXECUTION_CHANNELS:
        raise HTTPException(status_code=400, detail="执行通道仅支持 auto/ansible/ssh_shell")

    allowed_refs = _allowed_resource_refs(db, app_id)
    refs = sorted(set(str(item or "").strip() for item in payload.resource_refs if str(item or "").strip()))
    unknown_refs = [item for item in refs if item not in allowed_refs]
    if unknown_refs:
        raise HTTPException(status_code=400, detail=f"服务引用了未绑定资源: {', '.join(unknown_refs[:3])}")
    control_ref = (payload.control_ref or "").strip()
    if deployment_type == "docker_compose":
        compose_file, service_name, env_file = _parse_compose_control_ref(control_ref)
        env_content = _validate_env_content(payload.compose_env_content)
        if env_content and not env_file:
            env_file = _validate_env_file_path(payload.compose_env_filename, compose_file)
        control_ref = _build_compose_control_ref(compose_file, service_name, env_file)
    return {
        "name": name[:120],
        "component_type": component_type,
        "deployment_type": deployment_type,
        "control_method": control_method,
        "control_ref": control_ref[:500],
        "execution_channel": execution_channel,
        "runtime": (payload.runtime or "").strip()[:120],
        "owner": (payload.owner or "").strip()[:80],
        "env": env,
        "status": status,
        "resource_refs": _dump_json_list(refs[:30]),
        "description": (payload.description or "").strip()[:500],
    }


def _component_identity(deployment_type: str, control_ref: str, resource_refs: Any) -> tuple[str, str, tuple[str, ...]]:
    """生成运行实例稳定身份，忽略资源引用顺序和重复项。"""
    refs = tuple(sorted(set(_load_json_list(resource_refs))))
    return str(deployment_type or "").strip().lower(), str(control_ref or "").strip(), refs


def _find_existing_component(db: Session, app_id: str, data: dict[str, Any]) -> ApplicationComponent | None:
    """查找当前应用中身份相同的运行实例。"""
    deployment_type, control_ref, refs = _component_identity(
        data.get("deployment_type", ""),
        data.get("control_ref", ""),
        data.get("resource_refs", []),
    )
    rows = (
        db.query(ApplicationComponent)
        .filter(
            ApplicationComponent.app_id == app_id,
            ApplicationComponent.deployment_type == deployment_type,
            ApplicationComponent.control_ref == control_ref,
        )
        .all()
    )
    return next((row for row in rows if _component_identity(row.deployment_type, row.control_ref, row.resource_refs)[2] == refs), None)


def _serialize_component(row: ApplicationComponent, db: Session | None = None) -> dict[str, Any]:
    """序列化应用服务。"""
    compose_content = ""
    compose_env_content = ""
    compose_env_filename = ".env"
    if db is not None and (row.deployment_type or "") == "docker_compose" and row.control_ref:
        try:
            compose_file, _, env_file = _parse_compose_control_ref(row.control_ref)
            compose_env_filename = posixpath.basename(env_file) if env_file else ".env"
            values = _runtime_component_file_values(
                db=db,
                app_id=row.app_id,
                component_id=row.component_id,
                compose_file=compose_file,
                env_file=env_file,
            )
            compose_content = values.get(_runtime_config_key(row.component_id, posixpath.basename(compose_file)), "")
            compose_env_content = values.get(_runtime_config_key(row.component_id, compose_env_filename), "")
        except HTTPException:
            # 历史脏数据不阻断详情加载，编辑时由保存校验提示用户修正。
            pass
    return {
        "component_id": row.component_id,
        "name": row.name or "",
        "component_type": row.component_type or "backend",
        "deployment_type": row.deployment_type or "binary",
        "control_method": row.control_method or "systemd",
        "control_ref": row.control_ref or "",
        "execution_channel": row.execution_channel or "auto",
        "runtime": row.runtime or "",
        "owner": row.owner or "",
        "env": row.env or "prod",
        "status": row.status or "active",
        "resource_refs": _load_json_list(row.resource_refs),
        "description": row.description or "",
        "compose_content": compose_content,
        "compose_env_filename": compose_env_filename,
        "compose_env_content": compose_env_content,
        "pull_image": True,
        "last_control_action": row.last_control_action or "",
        "last_control_result": row.last_control_result or "",
        "last_control_at": format_app_datetime(row.last_control_at),
        "last_control_duration_ms": int(row.last_control_duration_ms or 0),
        "last_control_output": row.last_control_output or "",
        "created_at": format_app_datetime(row.created_at),
        "updated_at": format_app_datetime(row.updated_at),
    }


def _build_runtime_update_module_args(row: ApplicationComponent, db: Session) -> str:
    """兼容旧导入名，Compose 文件内容仍由 API 层从数据库读取。"""
    runtime_type = str(row.deployment_type or "").strip()
    if runtime_type == "docker_compose":
        compose_file, service_name, env_file = _parse_compose_control_ref(row.control_ref or "")
        values = _runtime_component_file_values(
            db=db,
            app_id=row.app_id,
            component_id=row.component_id,
            compose_file=compose_file,
            env_file=env_file,
        )
        compose_content = values.get(_runtime_config_key(row.component_id, posixpath.basename(compose_file)), "")
        env_filename = posixpath.basename(env_file) if env_file else ".env"
        env_content = values.get(_runtime_config_key(row.component_id, env_filename), "")
        if not compose_content:
            raise HTTPException(status_code=400, detail="运行实例缺少 Compose 文件内容，请先编辑实例并保存 compose 文件")
        return _build_compose_update_module_args(compose_file, service_name, compose_content, env_file, env_content, True)
    return _runtime_build_update_module_args(row)


def _run_runtime_discovery_command(
    asset: Asset,
    command: str,
    db: Session,
    current_user: User,
) -> tuple[bool, str, str]:
    """通过 SSH 直连执行固定只读扫描命令。"""
    pkey_content = ""
    if asset.credential_ref:
        pkey_content = get_asset_credential_content(db, asset.credential_ref, current_user)

    ok, notice, stdout, stderr, code = run_ssh_command(
        asset.ip,
        int(asset.ssh_port or 22),
        asset.ssh_user or "root",
        command,
        45,
        decode_asset_password(asset),
        pkey_content,
    )
    if not ok or int(code) != 0:
        message = str(stderr or stdout or notice or "扫描命令执行失败").strip()
        return False, "", message[-500:]
    return True, str(stdout or ""), ""


def _get_visible_application(app_id: str, db: Session, user: User) -> Application:
    """读取应用并校验当前用户是否可见。"""
    app = db.query(Application).filter(Application.app_id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="应用不存在")
    if not _application_visible_to_user(app, user):
        raise HTTPException(status_code=403, detail="当前权限组无权查看该应用")
    return app


def _visible_app_ids(db: Session, user: User) -> list[str]:
    """返回当前用户可见的项目 ID 列表。"""
    return [
        str(row.app_id)
        for row in db.query(Application).all()
        if _application_visible_to_user(row, user)
    ]


def _app_name_map(db: Session, app_ids: list[str] | None = None) -> dict[str, str]:
    """构建项目 ID 到项目名称的映射。"""
    query = db.query(Application)
    if app_ids:
        query = query.filter(Application.app_id.in_(app_ids))
    return {str(row.app_id): str(row.name or row.app_id) for row in query.all()}


def _parse_date_value(raw_value: str | None) -> datetime | None:
    """解析日期字符串，用于到期统计。"""
    text = (raw_value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    return None


def _is_expiring_soon(raw_value: str | None, days: int = 30) -> bool:
    """判断日期是否在指定天数内到期。"""
    parsed = _parse_date_value(raw_value)
    if not parsed:
        return False
    delta_days = (parsed.date() - datetime.now().date()).days
    return 0 <= delta_days <= days


def _host_asset_lookup(db: Session) -> dict[str, Asset]:
    """构建项目资源 ID 到主机资产的兼容映射。"""
    result: dict[str, Asset] = {}
    for asset in db.query(Asset).all():
        keys = {
            str(asset.id),
            f"asset-{asset.id}",
            _ansible_asset_identity(asset),
            str(asset.instance_id or "").strip(),
        }
        for key in keys:
            if key:
                result[key] = asset
    return result


def _bound_host_assets(db: Session, app_id: str) -> dict[str, tuple[Asset, str]]:
    """返回项目绑定主机。

    返回 key 为项目资源绑定里的 resource_id，value 为主机对象与展示名。
    兼容历史绑定：resource_id 可能是 instance_id、数据库 id 或 asset-{id}。
    """
    lookup = _host_asset_lookup(db)
    result: dict[str, tuple[Asset, str]] = {}
    rows = (
        db.query(ApplicationResource)
        .filter(ApplicationResource.app_id == app_id, ApplicationResource.resource_type == "host")
        .all()
    )
    for row in rows:
        resource_id = str(row.resource_id or "").strip()
        asset = lookup.get(resource_id)
        if not asset:
            continue
        result[resource_id] = (asset, str(row.resource_name or asset.name or resource_id))
    return result


def _build_builtin_docker_compose_playbook(
    deploy_dir: str,
    compose_filename: str,
    compose_content: str,
    service_name: str = "",
    pull_image: bool = True,
) -> str:
    """生成系统内置 Docker Compose 部署 Playbook。

    Playbook 的任务固定为创建目录、写入 compose 文件、可选拉镜像、启动服务、
    查看状态。前端不能注入 shell 命令，只能提供受限参数和 compose 文件内容。
    """
    compose_path = f"{deploy_dir.rstrip('/')}/{compose_filename}"
    service_suffix = f" {service_name}" if service_name else ""
    tasks: list[dict[str, Any]] = [
        {
            "name": "Ensure deploy directory exists",
            "ansible.builtin.file": {"path": deploy_dir, "state": "directory", "mode": "0755"},
        },
        {
            "name": "Write docker compose file",
            "ansible.builtin.copy": {"dest": compose_path, "content": compose_content, "mode": "0644"},
        },
    ]
    if pull_image:
        tasks.append(
            {
                "name": "Pull docker compose images",
                "ansible.builtin.command": {"cmd": f"docker compose -f {compose_path} pull{service_suffix}"},
            }
        )
    tasks.extend(
        [
            {
                "name": "Start docker compose services",
                "ansible.builtin.command": {"cmd": f"docker compose -f {compose_path} up -d{service_suffix}"},
            },
            {
                "name": "Show docker compose status",
                "ansible.builtin.command": {"cmd": f"docker compose -f {compose_path} ps"},
                "register": "opspilot_compose_ps",
                "changed_when": False,
            },
            {
                "name": "Print docker compose status",
                "ansible.builtin.debug": {"var": "opspilot_compose_ps.stdout_lines"},
            },
        ]
    )
    return json.dumps(
        [
            {
                "name": "OpsPilot builtin docker compose deploy",
                "hosts": "all",
                "become": True,
                "tasks": tasks,
            }
        ],
        ensure_ascii=False,
        indent=2,
    )


def _compose_artifact_ref(deploy_dir: str, compose_filename: str, service_name: str) -> str:
    """生成发布记录里的 Compose 文件定位信息。"""
    compose_path = f"{deploy_dir.rstrip('/')}/{compose_filename}"
    return f"{compose_path}:{service_name}" if service_name else compose_path


def _runtime_config_key(component_id: str, filename: str) -> str:
    """生成运行实例配置文件在配置中心里的稳定键名。"""
    safe_component = str(component_id or "").strip()
    safe_filename = str(filename or "").strip()
    if safe_filename.endswith((".yml", ".yaml")):
        safe_filename = _validate_compose_filename(safe_filename)
    else:
        safe_filename = _validate_env_filename(safe_filename)
    return f"runtime/{safe_component}/{safe_filename}"


def _runtime_component_file_values(
    db: Session,
    app_id: str,
    component_id: str,
    compose_file: str,
    env_file: str = "",
) -> dict[str, str]:
    """读取运行实例关联的配置文件内容。"""
    compose_filename = posixpath.basename(_validate_compose_file_path(compose_file))
    env_filename = posixpath.basename(_validate_env_file_path(env_file, compose_file)) if env_file else ".env"
    keys = [
        _runtime_config_key(component_id, compose_filename),
        _runtime_config_key(component_id, env_filename),
    ]
    rows = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app_id, ApplicationConfig.key.in_(keys))
        .all()
    )
    return {str(row.key): str(row.value or "") for row in rows}


def _upsert_runtime_file_config(
    db: Session,
    current_user: User,
    app_id: str,
    component_id: str,
    env: str,
    filename: str,
    value: str,
    remark: str,
) -> None:
    """把运行实例文件内容镜像到应用配置中心。

    这里只处理运行实例配置文件，不接收凭证引用；`.env` 中的敏感项后续可再
    拆为凭证引用，避免一期把发布模型做得过重。
    """
    if value == "":
        return
    key = _runtime_config_key(component_id, filename)
    row = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app_id, ApplicationConfig.env == env, ApplicationConfig.key == key)
        .first()
    )
    now = _now_str()
    if row:
        old_value = row.value or ""
        row.value = value
        row.value_type = "file"
        row.credential_ref = ""
        row.remark = remark[:500]
        row.updated_at = now
        _add_config_revision(
            db=db,
            current_user=current_user,
            app_id=app_id,
            config_id=row.config_id,
            env=env,
            key=key,
            change_type="update",
            old_value_type="file",
            old_value=old_value,
            new_value_type="file",
            new_value=value,
            remark=remark,
        )
        return

    config_id = f"CFG-{uuid.uuid4().hex[:8].upper()}"
    db.add(
        ApplicationConfig(
            config_id=config_id,
            app_id=app_id,
            env=env,
            key=key,
            value=value,
            value_type="file",
            credential_ref="",
            remark=remark[:500],
            created_at=now,
            updated_at=now,
        )
    )
    _add_config_revision(
        db=db,
        current_user=current_user,
        app_id=app_id,
        config_id=config_id,
        env=env,
        key=key,
        change_type="create",
        new_value_type="file",
        new_value=value,
        remark=remark,
    )


def _sync_runtime_component_configs(
    db: Session,
    current_user: User,
    app_id: str,
    component_id: str,
    env: str,
    control_ref: str,
    payload: ApplicationComponentPayload,
) -> None:
    """同步运行实例配置文件到配置中心。"""
    if payload.deployment_type != "docker_compose":
        return
    compose_file, _, env_file = _parse_compose_control_ref(control_ref)
    compose_content = str(payload.compose_content or "").strip()
    env_content = _validate_env_content(payload.compose_env_content)
    if compose_content:
        _upsert_runtime_file_config(
            db=db,
            current_user=current_user,
            app_id=app_id,
            component_id=component_id,
            env=env,
            filename=posixpath.basename(compose_file),
            value=_validate_compose_content(compose_content),
            remark="运行实例 Compose 文件",
        )
    if env_content:
        _upsert_runtime_file_config(
            db=db,
            current_user=current_user,
            app_id=app_id,
            component_id=component_id,
            env=env,
            filename=posixpath.basename(env_file or _validate_env_file_path(payload.compose_env_filename, compose_file)),
            value=env_content,
            remark="运行实例 .env 环境变量文件",
        )


def _validate_config_payload(
    payload: ApplicationConfigPayload,
    db: Session,
    current_user: User,
) -> dict[str, str]:
    """校验并归一化项目配置。"""
    env = (payload.env or "prod").strip()
    key = (payload.key or "").strip()
    value_type = (payload.value_type or "plain").strip()
    if env not in APP_ENV_VALUES:
        raise HTTPException(status_code=400, detail="配置环境仅支持 prod/test/dev/mixed")
    if not key:
        raise HTTPException(status_code=400, detail="配置键不能为空")
    if len(key) > 120:
        raise HTTPException(status_code=400, detail="配置键不能超过 120 个字符")
    if value_type not in CONFIG_VALUE_TYPES:
        raise HTTPException(status_code=400, detail="配置类型仅支持 plain/credential_ref")

    value = (payload.value or "").strip()
    credential_ref = (payload.credential_ref or "").strip()
    if value_type == "credential_ref":
        credential = db.query(Credential).filter(Credential.cred_id == credential_ref).first()
        ensure_credential_access(
            credential,
            current_user,
            not_found_message="引用凭证不存在",
            denied_message="当前账号无权引用该凭证",
        )
        value = ""
    elif value_type == "file":
        credential_ref = ""
        if len(value) > MAX_CONFIG_FILE_VALUE_LENGTH:
            raise HTTPException(status_code=400, detail=f"配置文件内容不能超过 {MAX_CONFIG_FILE_VALUE_LENGTH} 个字符")
    else:
        credential_ref = ""
        if len(value) > MAX_CONFIG_PLAIN_VALUE_LENGTH:
            raise HTTPException(status_code=400, detail=f"配置值不能超过 {MAX_CONFIG_PLAIN_VALUE_LENGTH} 个字符")
    return {
        "env": env,
        "key": key,
        "value": value,
        "value_type": value_type,
        "credential_ref": credential_ref,
        "remark": (payload.remark or "").strip()[:500],
    }


def _serialize_config(
    row: ApplicationConfig,
    db: Session,
    app_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """序列化应用配置，凭证型配置只返回凭证引用和名称。"""
    credential_name = ""
    if row.credential_ref:
        credential = db.query(Credential).filter(Credential.cred_id == row.credential_ref).first()
        credential_name = str(credential.name or "") if credential else ""
    names = app_names or _app_name_map(db, [str(row.app_id)])
    return {
        "config_id": row.config_id,
        "app_id": row.app_id,
        "app_name": names.get(str(row.app_id), ""),
        "env": row.env or "prod",
        "key": row.key or "",
        "value": row.value if row.value_type == "plain" else "",
        "value_type": row.value_type or "plain",
        "credential_ref": row.credential_ref or "",
        "credential_name": credential_name,
        "remark": row.remark or "",
        "created_at": format_app_datetime(row.created_at),
        "updated_at": format_app_datetime(row.updated_at),
    }


def _config_revision_snapshot(value_type: str, value: str, credential_ref: str) -> dict[str, str]:
    """生成配置历史快照。

    凭证引用配置的 value 可能来自前端误传或历史脏数据，这里统一丢弃，
    只保留凭证 ID，避免版本历史成为第二个密钥仓库。
    """
    normalized_type = (value_type or "plain").strip()
    if normalized_type == "credential_ref":
        return {
            "value_snapshot": "",
            "credential_ref_snapshot": (credential_ref or "").strip()[:80],
        }
    return {
        "value_snapshot": (value or "")[:2000],
        "credential_ref_snapshot": "",
    }


def _serialize_config_revision(row: ApplicationConfigRevision) -> dict[str, Any]:
    """序列化配置版本历史。"""
    return {
        "revision_id": row.revision_id or "",
        "app_id": row.app_id or "",
        "config_id": row.config_id or "",
        "env": row.env or "prod",
        "key": row.key or "",
        "change_type": row.change_type or "update",
        "value_type": row.value_type or "plain",
        "old_value_snapshot": row.old_value_snapshot or "",
        "new_value_snapshot": row.new_value_snapshot or "",
        "old_credential_ref": row.old_credential_ref or "",
        "new_credential_ref": row.new_credential_ref or "",
        "operator": row.operator or "",
        "remark": row.remark or "",
        "created_at": format_app_datetime(row.created_at),
    }


def _add_config_revision(
    db: Session,
    current_user: User,
    app_id: str,
    config_id: str,
    env: str,
    key: str,
    change_type: str,
    old_value_type: str = "plain",
    old_value: str = "",
    old_credential_ref: str = "",
    new_value_type: str = "plain",
    new_value: str = "",
    new_credential_ref: str = "",
    remark: str = "",
) -> None:
    """把配置变更写入历史表。

    不在函数内提交事务，由调用方和配置 CRUD 一起 commit，保证审计记录
    与业务变更原子落库。
    """
    if change_type not in CONFIG_CHANGE_TYPES:
        raise HTTPException(status_code=400, detail="配置变更类型非法")
    old_snapshot = _config_revision_snapshot(old_value_type, old_value, old_credential_ref)
    new_snapshot = _config_revision_snapshot(new_value_type, new_value, new_credential_ref)
    operator = str(current_user.name or current_user.username or "").strip()[:80]
    value_type = new_value_type if change_type != "delete" else old_value_type
    db.add(
        ApplicationConfigRevision(
            revision_id=f"CFGREV-{uuid.uuid4().hex[:8].upper()}",
            app_id=app_id,
            config_id=config_id,
            env=env,
            key=key,
            change_type=change_type,
            value_type=value_type,
            old_value_snapshot=old_snapshot["value_snapshot"],
            new_value_snapshot=new_snapshot["value_snapshot"],
            old_credential_ref=old_snapshot["credential_ref_snapshot"],
            new_credential_ref=new_snapshot["credential_ref_snapshot"],
            operator=operator,
            remark=(remark or "").strip()[:500],
            created_at=_now_str(),
        )
    )


def _build_application_timeline(
    app: Application,
    db: Session,
    limit: int,
) -> dict[str, Any]:
    """兼容旧导入名，实际聚合逻辑在 service 层。"""
    return build_application_timeline(
        app=app,
        db=db,
        limit=limit,
        observability_builder=_build_application_observability_context,
    )


def _resource_options(db: Session) -> dict[tuple[str, str], ResourceOption]:
    """聚合可被项目绑定的已有资源。"""
    result: dict[tuple[str, str], ResourceOption] = {}
    for row in db.query(Asset).order_by(Asset.id.desc()).all():
        rid = row.instance_id or str(row.id)
        result[("host", rid)] = ResourceOption(
            resource_type="host",
            resource_id=rid,
            name=row.name or rid,
            meta=f"{row.ip or '-'} · {row.provider or '-'}",
        )
    for row in db.query(VirtualMachine).order_by(VirtualMachine.id.desc()).all():
        result[("vm", row.vm_id)] = ResourceOption(
            resource_type="vm",
            resource_id=row.vm_id,
            name=row.name or row.vm_id,
            meta=f"{row.node or '-'} · {row.status or '-'}",
        )
    for row in db.query(DomainRecord).order_by(DomainRecord.id.desc()).all():
        rid = str(row.id)
        result[("domain", rid)] = ResourceOption(
            resource_type="domain",
            resource_id=rid,
            name=row.domain or rid,
            meta=f"{row.provider or '-'} · {row.expires_at or '-'}",
        )
    for row in db.query(CertificateRecord).order_by(CertificateRecord.id.desc()).all():
        result[("certificate", row.cert_id)] = ResourceOption(
            resource_type="certificate",
            resource_id=row.cert_id,
            name=row.name or row.primary_domain or row.cert_id,
            meta=f"{row.status or '-'} · {row.not_after or '-'}",
        )
    return result


# 子路由需要复用大量历史 helper。显式声明 __all__，让 `from .shared import *`
# 也能导入下划线开头的内部函数，同时避免每个子路由维护一长串重复 import。
__all__ = [name for name in globals() if not name.startswith("__")]
