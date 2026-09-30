"""应用中心路由聚合入口。

该模块保持历史导入路径 `app.api.applications` 不变，具体业务路由拆分到
`app.api.application_routes` 包内，避免单文件继续膨胀。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.application import *  # noqa: F401,F403
from app.services.application_health import (
    APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS,
    build_application_health as _build_application_health,
    compute_application_health_snapshot as _compute_application_health_snapshot,
    run_application_health_snapshot_cycle,
    start_application_health_snapshot_runner,
)
from app.services.application_timeline import (
    config_revision_timeline_event as _config_revision_timeline_event,
    normalize_alert_time as _normalize_alert_time,
    release_timeline_event as _release_timeline_event,
    runtime_control_timeline_event as _runtime_control_timeline_event,
)
from app.services.application_config_import import (
    CONFIG_IMPORT_SENSITIVE_PATTERN,
    parse_env_import_content as _parse_env_import_content,
)
from app.services.application_runtime import (
    build_docker_container_release_module_args as _build_docker_container_release_module_args,
    build_supervisor_artifact_release_module_args as _build_supervisor_artifact_release_module_args,
    build_systemd_artifact_release_module_args as _build_systemd_artifact_release_module_args,
    parse_kubernetes_control_ref as _parse_kubernetes_control_ref,
)
from app.api.application_routes.catalog import router as catalog_router
from app.api.application_routes.configs import router as configs_router
from app.api.application_routes.releases import (
    _plan_application_release,
    recover_stale_running_releases,
    router as releases_router,
)
from app.api.application_routes.runtime import router as runtime_router
from app.api.application_routes.deployment_tasks import router as deployment_tasks_router
from app.api.application_routes.shared import _build_runtime_update_module_args

router = APIRouter()

# 子路由里保留了应用列表的空路径 `GET ""`，FastAPI 不允许另一个 APIRouter
# 在无 prefix 时 include 这种空路径路由。这里按原路由顺序合并 route 表：
# 既保持 `/api/applications` 入口不变，也避免 `/{app_id}` 抢占更具体的路径。
for child_router in (configs_router, releases_router, runtime_router, deployment_tasks_router, catalog_router):
    router.routes.extend(child_router.routes)
