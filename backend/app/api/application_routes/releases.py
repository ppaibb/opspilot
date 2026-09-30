"""应用中心发布记录、发布预检与发布执行路由。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import BackgroundTasks

from app.db.database import SessionLocal
from app.services.leases import acquire_lease, lease_owner, release_lease

from .shared import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

router = APIRouter()

STALE_RUNNING_RELEASE_MINUTES = 30

# 发布执行租约有效期（秒）：覆盖单次发布的正常时长上限；
# 执行器崩溃未释放时，残留记录由启动时的 recover_stale_running_releases 收敛。
RELEASE_LEASE_TTL_SECONDS = 3600


def recover_stale_running_releases(
    stale_minutes: int = STALE_RUNNING_RELEASE_MINUTES,
) -> int:
    """把进程重启后遗留的 running 发布记录标记为失败。

    当前发布任务仍基于 FastAPI `BackgroundTasks`，任务状态没有外部 broker
    持久化。服务进程重启时，已经入库为 running 但尚未完成的记录不会继续执行。
    启动时主动收敛这些陈旧任务，避免页面长期展示“执行中”。
    """
    cutoff = datetime.now() - timedelta(minutes=max(int(stale_minutes or 1), 1))
    changed = 0
    db = SessionLocal()
    try:
        rows = (
            db.query(ApplicationRelease)
            .filter(ApplicationRelease.status == "running")
            .all()
        )
        for row in rows:
            started_at = parse_app_datetime(row.started_at) or datetime.min
            if started_at > cutoff:
                continue
            row.status = "failed"
            row.finished_at = _now_str()
            row.summary = "发布任务因服务重启或超时中断，已自动标记失败"
            row.output = row.output or row.summary
            changed += 1
        if changed:
            db.commit()
        return changed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@router.get(
    "/deployment-options",
    response_model=List[AnsibleTemplateOption],
    dependencies=[Depends(require_permissions("module.apps.deployment"))],
)
def get_application_deployment_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目发布可选模板。

    这里不直接复用 /api/tools 路由权限，避免用户必须进入“运维工具”菜单；
    真正执行时仍复用 Ansible 执行器的模块白名单、凭证授权组、超时和审计。
    """
    return [
        {
            "template_id": item["template_id"],
            "kind": item.get("kind") or "ansible",
            "name": item["name"],
            "description": item.get("description") or "",
            "module": item.get("module") or "",
        }
        for item in _read_ansible_templates(db)
        if _template_visible_to_user(item, current_user)
    ]

@router.get(
    "/releases",
    response_model=List[ApplicationReleaseResponse],
    dependencies=[Depends(require_permissions("module.apps.deployment"))],
)
def list_application_releases(
    q: str = "",
    app_id: str = "",
    env: str = "",
    status: str = "",
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回当前用户可见项目的发布记录。"""
    visible_ids = _visible_app_ids(db, current_user)
    if not visible_ids:
        return []
    query = db.query(ApplicationRelease).filter(ApplicationRelease.app_id.in_(visible_ids))
    if app_id:
        if app_id not in visible_ids:
            raise HTTPException(status_code=403, detail="当前权限组无权查看该应用")
        query = query.filter(ApplicationRelease.app_id == app_id)
    if env:
        query = query.filter(ApplicationRelease.env == env.strip())
    if status:
        query = query.filter(ApplicationRelease.status == status.strip())
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                ApplicationRelease.release_id.ilike(f"%{keyword}%"),
                ApplicationRelease.version.ilike(f"%{keyword}%"),
                ApplicationRelease.template_name.ilike(f"%{keyword}%"),
                ApplicationRelease.operator.ilike(f"%{keyword}%"),
            )
        )
    rows = query.order_by(ApplicationRelease.id.desc()).offset(offset).limit(limit).all()
    names = _app_name_map(db, list({str(row.app_id) for row in rows}))
    return [_serialize_release(row, names) for row in rows]

@router.get(
    "/{app_id}/releases",
    response_model=List[ApplicationReleaseResponse],
    dependencies=[Depends(require_permissions("module.apps.deployment"))],
)
def list_application_releases_by_app(
    app_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回单个项目的发布记录。"""
    app = _get_visible_application(app_id, db, current_user)
    rows = (
        db.query(ApplicationRelease)
        .filter(ApplicationRelease.app_id == app.app_id)
        .order_by(ApplicationRelease.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_serialize_release(row, {app.app_id: app.name}) for row in rows]


@router.get(
    "/{app_id}/releases/{release_id}",
    response_model=ApplicationReleaseResponse,
    dependencies=[Depends(require_permissions("module.apps.deployment"))],
)
def get_application_release(
    app_id: str,
    release_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目发布详情。"""
    app = _get_visible_application(app_id, db, current_user)
    row = (
        db.query(ApplicationRelease)
        .filter(ApplicationRelease.app_id == app.app_id, ApplicationRelease.release_id == release_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return _serialize_release(row, {app.app_id: app.name})


@router.post(
    "/{app_id}/releases/{release_id}/retry",
    response_model=ApplicationReleaseResponse,
    dependencies=[Depends(require_permissions("action.apps.deploy"))],
)
def retry_application_release(
    app_id: str,
    release_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """基于失败发布记录创建一次新的异步重试任务。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "deploy")
    source = (
        db.query(ApplicationRelease)
        .filter(ApplicationRelease.app_id == app.app_id, ApplicationRelease.release_id == release_id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if source.release_type == "external":
        raise HTTPException(status_code=400, detail="外部发布记录不需要重试")
    if source.release_type == "docker_compose":
        raise HTTPException(status_code=400, detail="Docker Compose 发布包含文件内容，请重新发起发布")
    if source.release_type == "docker":
        raise HTTPException(status_code=400, detail="Docker 发布包含运行参数，请重新发起发布")
    if source.release_type == "supervisor":
        raise HTTPException(status_code=400, detail="Supervisor 发布包含启动命令或制品内容，请重新发起发布")
    if source.status not in {"failed", "canceled"}:
        raise HTTPException(status_code=400, detail="只有失败或取消的发布记录可以重试")

    payload = ApplicationReleasePayload(
        version=source.version or "",
        env=source.env or "prod",
        component_id=source.component_id or "",
        release_type=source.release_type or "external",
        template_id=source.template_id or "",
        artifact_ref=source.artifact_ref or "",
        rollback_release_id=source.rollback_release_id or "",
        target_resource_ids=_load_json_list(source.target_resource_ids),
        summary=f"重试 {source.release_id}",
    )
    plan = _ensure_release_plan_allowed(app=app, payload=payload, db=db, current_user=current_user)
    targets = list(plan.get("targets") or [])
    retry_row = ApplicationRelease(
        release_id=f"REL-{uuid.uuid4().hex[:8].upper()}",
        app_id=app.app_id,
        component_id=source.component_id or "",
        component_name=source.component_name or "",
        version=source.version or "",
        env=source.env or "prod",
        release_type=source.release_type or "external",
        external_url="",
        template_id=source.template_id or "",
        template_name=source.template_name or "",
        artifact_ref=source.artifact_ref or "",
        rollback_release_id=source.rollback_release_id or "",
        target_resource_ids=_dump_json_list([str(item.get("resource_id") or "") for item in targets]),
        target_names=_dump_json_list([str(item.get("name") or "") for item in targets]),
        status="running",
        operator=str(current_user.name or current_user.username or "未知用户")[:80],
        started_at=_now_str(),
        finished_at=None,
        duration_ms=0,
        summary=f"重试任务已提交：{source.release_id}",
        output="",
    )
    db.add(retry_row)
    db.commit()
    db.refresh(retry_row)
    client_ip = request.client.host if request.client else "-"
    background_tasks.add_task(
        _run_release_job,
        retry_row.release_id,
        _release_payload_to_dict(payload),
        str(current_user.username or ""),
        str(client_ip or "-"),
    )
    return _serialize_release(retry_row, {app.app_id: app.name})


def _plan_application_release(
    *,
    app: Application,
    payload: ApplicationReleasePayload,
    db: Session,
    current_user: User,
) -> dict[str, Any]:
    """兼容旧导入名，实际发布预检逻辑在 service 层。"""
    return plan_application_release(
        app=app,
        payload=payload,
        db=db,
        current_user=current_user,
        deps=ApplicationReleasePlannerDeps(
            bound_host_assets=_bound_host_assets,
            build_runtime_release_module_args=_build_runtime_release_module_args,
            find_template_index=_find_template_index,
            normalize_template_row=_normalize_template_row,
            read_ansible_templates=_read_ansible_templates,
            template_visible_to_user=_template_visible_to_user,
            validate_compose_content=_validate_compose_content,
            validate_compose_deploy_path=_validate_compose_deploy_path,
            validate_compose_filename=_validate_compose_filename,
            validate_compose_service_name=_validate_compose_service_name,
            validate_docker_image_ref=_validate_docker_image_ref,
            validate_docker_run_args=_validate_docker_run_args,
            validate_systemd_artifact_content_b64=_validate_systemd_artifact_content_b64,
            validate_systemd_artifact_filename=_validate_systemd_artifact_filename,
            validate_systemd_artifact_ref=_validate_systemd_artifact_ref,
            validate_supervisor_command=_validate_supervisor_command,
            validate_supervisor_environment=_validate_supervisor_environment,
            validate_supervisor_program_name=_validate_supervisor_program_name,
            validate_supervisor_runtime=_validate_supervisor_runtime,
            validate_supervisor_user=_validate_supervisor_user,
        ),
    )


def _ensure_release_plan_allowed(
    *,
    app: Application,
    payload: ApplicationReleasePayload,
    db: Session,
    current_user: User,
) -> dict[str, Any]:
    """真实发布执行前复用预检计划做后端阻断。

    前端会要求用户先运行预检，但安全边界不能依赖浏览器状态。这里在执行
    Ansible/SSH 前再次用同一套只读计划器校验目标、运行实例和命令参数。
    """
    plan = _plan_application_release(
        app=app,
        payload=payload,
        db=db,
        current_user=current_user,
    )
    if plan.get("allowed", False):
        return plan
    errors = [
        str(item.get("message") or "").strip()
        for item in plan.get("checks", [])
        if item.get("level") == "error" and str(item.get("message") or "").strip()
    ]
    detail = "；".join(errors[:5]) or "发布计划存在阻断问题"
    raise HTTPException(status_code=400, detail=f"发布预检未通过：{detail}")


@router.get(
    "/deployment-health",
    dependencies=[Depends(require_permissions("module.apps.deployment"))],
)
def get_application_deployment_health(db: Session = Depends(get_db)):
    """返回应用发布执行器健康状态。

    这里只暴露 Ansible 可用性和配置状态，不返回任何主机凭证或模板内容。
    """
    config, updated_at = _read_ansible_config(db)
    available, version, last_error = _check_ansible_available(str(config["executable"]))
    return {
        "ansible": {
            "enabled": bool(config["enabled"]),
            "available": bool(available),
            "executable": str(config["executable"]),
            "version": version,
            "last_error": last_error,
            "updated_at": updated_at,
        }
    }


def _release_payload_from_dict(raw_payload: dict[str, Any]) -> ApplicationReleasePayload:
    """从后台任务载荷恢复发布请求。"""
    return ApplicationReleasePayload(**dict(raw_payload or {}))


def _release_payload_to_dict(payload: ApplicationReleasePayload) -> dict[str, Any]:
    """把发布请求转成后台任务可安全持有的普通字典。"""
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()


@dataclass
class _ReleaseExecutionContext:
    """发布执行处理器上下文。"""

    app: Application
    row: ApplicationRelease
    payload: ApplicationReleasePayload
    request: Request | BackgroundRequestContext
    db: Session
    current_user: User


class _BaseReleaseHandler:
    """发布执行处理器基类。"""

    def execute(self, ctx: _ReleaseExecutionContext) -> Any:
        raise NotImplementedError


class _AnsibleReleaseHandler(_BaseReleaseHandler):
    """执行 Ansible 模板发布。"""

    def execute(self, ctx: _ReleaseExecutionContext) -> Any:
        template_id = str(ctx.row.template_id or ctx.payload.template_id or "").strip()
        selected_resource_ids = _load_json_list(ctx.row.target_resource_ids) or [
            str(item or "").strip() for item in ctx.payload.target_resource_ids if str(item or "").strip()
        ]
        bound_hosts = _bound_host_assets(ctx.db, ctx.app.app_id)
        selected_assets = [bound_hosts[item][0] for item in selected_resource_ids if item in bound_hosts]
        return run_ansible_template(
            payload=AnsibleTemplateRunRequest(
                template_id=template_id,
                run_all_override=False,
                asset_ids_override=[_ansible_asset_identity(asset) for asset in selected_assets],
            ),
            request=ctx.request,
            db=ctx.db,
            current_user=ctx.current_user,
        )


class _SingleRuntimeReleaseHandler(_BaseReleaseHandler):
    """执行 systemd、Supervisor 或 Docker 单运行对象发布。"""

    def execute(self, ctx: _ReleaseExecutionContext) -> Any:
        release_type = str(ctx.row.release_type or ctx.payload.release_type or "").strip()
        component = _release_component(ctx)
        if release_type == "systemd":
            module_args = _build_systemd_artifact_release_module_args(
                component.control_ref or "",
                ctx.payload.deploy_dir,
                ctx.row.artifact_ref or ctx.payload.artifact_ref,
                getattr(ctx.payload, "artifact_filename", ""),
                getattr(ctx.payload, "artifact_content_b64", ""),
            )
        elif release_type == "supervisor":
            module_args = _build_supervisor_artifact_release_module_args(
                component.control_ref or "",
                ctx.payload.deploy_dir,
                ctx.payload.supervisor_command,
                ctx.row.artifact_ref or ctx.payload.artifact_ref,
                getattr(ctx.payload, "artifact_filename", ""),
                getattr(ctx.payload, "artifact_content_b64", ""),
                getattr(ctx.payload, "supervisor_user", ""),
                getattr(ctx.payload, "supervisor_environment", ""),
                getattr(ctx.payload, "supervisor_runtime", "system"),
            )
        else:
            module_args = _build_docker_container_release_module_args(
                component.control_ref or "",
                ctx.row.artifact_ref or ctx.payload.artifact_ref,
                getattr(ctx.payload, "docker_run_args", ""),
            )
        asset = _single_component_host_asset(ctx, component)
        return _execute_runtime_ssh_task(
            asset=asset,
            module_args=module_args,
            request=ctx.request,
            db=ctx.db,
            current_user=ctx.current_user,
            audit_action="应用发布执行",
            audit_resource=f"release:{ctx.row.release_id}:{release_type}",
        )


class _DockerComposeReleaseHandler(_BaseReleaseHandler):
    """执行 Docker Compose 发布。"""

    def execute(self, ctx: _ReleaseExecutionContext) -> Any:
        playbook = _build_builtin_docker_compose_playbook(
            deploy_dir=_validate_compose_deploy_path(ctx.payload.deploy_dir),
            compose_filename=_validate_compose_filename(ctx.payload.compose_filename),
            compose_content=_validate_compose_content(ctx.payload.compose_content),
            service_name=_validate_compose_service_name(ctx.payload.service_name),
            pull_image=bool(ctx.payload.pull_image),
        )
        selected_resource_ids = _load_json_list(ctx.row.target_resource_ids)
        bound_hosts = _bound_host_assets(ctx.db, ctx.app.app_id)
        selected_assets = [bound_hosts[selected_resource_ids[0]][0]]
        return run_ansible_playbook(
            payload=AnsiblePlaybookRunRequest(
                run_all=False,
                asset_ids=[_ansible_asset_identity(selected_assets[0])],
                playbook=playbook,
            ),
            request=ctx.request,
            db=ctx.db,
            current_user=ctx.current_user,
        )


class _RuntimeReleaseHandler(_BaseReleaseHandler):
    """执行 Kubernetes、Helm 发布或回滚。"""

    def execute(self, ctx: _ReleaseExecutionContext) -> Any:
        release_type = str(ctx.row.release_type or ctx.payload.release_type or "").strip()
        component = _release_component(ctx)
        runtime_type = "kubernetes" if component.deployment_type == "kubernetes_yaml" else component.deployment_type
        action_name = "publish"
        artifact_ref = str(ctx.row.artifact_ref or ctx.payload.artifact_ref or "").strip()
        if release_type == "rollback":
            source_release = (
                ctx.db.query(ApplicationRelease)
                .filter(
                    ApplicationRelease.app_id == ctx.app.app_id,
                    ApplicationRelease.release_id == ctx.row.rollback_release_id,
                    ApplicationRelease.component_id == component.component_id,
                    ApplicationRelease.status == "success",
                )
                .first()
            )
            if not source_release:
                raise HTTPException(status_code=400, detail="回滚来源发布记录不存在")
            runtime_type = source_release.release_type or runtime_type
            artifact_ref = str(source_release.version or "").strip()
            action_name = "rollback"
        module_args = _build_runtime_release_module_args(
            runtime_type,
            component.control_ref or "",
            artifact_ref,
            action_name,
        )
        asset = _single_component_host_asset(ctx, component)
        return _execute_runtime_ssh_task(
            asset=asset,
            module_args=module_args,
            request=ctx.request,
            db=ctx.db,
            current_user=ctx.current_user,
            audit_action="应用发布执行",
            audit_resource=f"release:{ctx.row.release_id}:{release_type}",
        )


_RELEASE_HANDLERS: dict[str, _BaseReleaseHandler] = {
    "ansible": _AnsibleReleaseHandler(),
    "systemd": _SingleRuntimeReleaseHandler(),
    "supervisor": _SingleRuntimeReleaseHandler(),
    "docker": _SingleRuntimeReleaseHandler(),
    "docker_compose": _DockerComposeReleaseHandler(),
    "kubernetes": _RuntimeReleaseHandler(),
    "helm": _RuntimeReleaseHandler(),
    "rollback": _RuntimeReleaseHandler(),
}


def _release_component(ctx: _ReleaseExecutionContext) -> ApplicationComponent:
    """读取发布记录绑定的运行对象。"""
    component = (
        ctx.db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == ctx.app.app_id, ApplicationComponent.component_id == ctx.row.component_id)
        .first()
    )
    if not component:
        raise HTTPException(status_code=400, detail="运行实例不存在")
    return component


def _single_component_host_asset(ctx: _ReleaseExecutionContext, component: ApplicationComponent) -> Asset:
    """返回运行对象绑定的单台执行主机。"""
    selected_resource_ids = _load_json_list(ctx.row.target_resource_ids)
    if not selected_resource_ids:
        selected_resource_ids = [
            ref.split(":", 1)[1].strip()
            for ref in _load_json_list(component.resource_refs)
            if ref.startswith("host:") and ref.split(":", 1)[1].strip()
        ]
    bound_hosts = _bound_host_assets(ctx.db, ctx.app.app_id)
    return bound_hosts[selected_resource_ids[0]][0]


def _execute_release_job_with_session(
    *,
    release_id: str,
    payload: ApplicationReleasePayload,
    request: Request | BackgroundRequestContext,
    db: Session,
    current_user: User,
) -> None:
    """在指定数据库会话中执行发布任务并更新发布记录。

    该函数供后台任务和契约测试复用。每次执行前都会重新读取发布记录、
    应用和运行实例，并再次调用预检守卫，避免任务排队期间资源关系被修改。
    """
    row = db.query(ApplicationRelease).filter(ApplicationRelease.release_id == release_id).first()
    if not row:
        return
    app = db.query(Application).filter(Application.app_id == row.app_id).first()
    if not app:
        row.status = "failed"
        row.summary = "应用不存在，发布任务终止"
        row.finished_at = _now_str()
        db.commit()
        return

    started_at = parse_app_datetime(row.started_at) or _now_str()
    row.status = "running"
    row.started_at = started_at
    row.finished_at = None
    db.commit()

    status = "failed"
    summary = ""
    output = ""
    duration_ms = 0

    try:
        _ensure_release_plan_allowed(app=app, payload=payload, db=db, current_user=current_user)
        release_type = str(row.release_type or payload.release_type or "").strip()
        handler = _RELEASE_HANDLERS.get(release_type)
        if not handler:
            raise HTTPException(status_code=400, detail="发布方式不支持后台执行")
        result = handler.execute(_ReleaseExecutionContext(app, row, payload, request, db, current_user))

        status = "success" if result.ok else "failed"
        summary = getattr(result, "notice", "") or ("发布执行成功" if result.ok else "发布执行失败")
        output = _truncate_release_output(result.stdout or "", result.stderr or "")
        duration_ms = int(result.duration_ms or 0)
        if result.ok and release_type == "supervisor" and row.component_id:
            component = (
                db.query(ApplicationComponent)
                .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == row.component_id)
                .first()
            )
            if component:
                component.runtime = f"supervisor:{_validate_supervisor_runtime(payload.supervisor_runtime)}"
                component.updated_at = _now_str()
                app.updated_at = component.updated_at
    except HTTPException as exc:
        db.rollback()
        status = "failed"
        summary = str(exc.detail or "发布执行失败")[:500]
        output = summary
    except Exception as exc:
        db.rollback()
        status = "failed"
        summary = f"发布执行失败：{str(exc)}"[:500]
        output = summary

    row = db.query(ApplicationRelease).filter(ApplicationRelease.release_id == release_id).first()
    if not row:
        return
    row.status = status
    row.summary = summary[:500]
    row.output = output
    row.duration_ms = duration_ms
    row.finished_at = _now_str()
    db.commit()


def _run_release_job(release_id: str, raw_payload: dict[str, Any], username: str, client_ip: str) -> None:
    """后台发布任务入口。

    通过数据库租约（release:{release_id}，TTL 3600 秒）保证同一条发布
    记录同一时刻只被一个进程执行，避免 BackgroundTasks、重试入口与启动
    恢复逻辑之间双跑；拿不到租约时不执行、不改动发布记录。
    """
    db = SessionLocal()
    lease_key = f"release:{release_id}"
    owner = lease_owner()
    try:
        if not acquire_lease(db, lease_key, owner, ttl_seconds=RELEASE_LEASE_TTL_SECONDS):
            # 其他实例正在执行同一发布（或租约残留未过期）：本实例直接跳过，
            # 发布记录交给持有者更新，异常残留由 recover_stale_running_releases 收敛。
            logger.warning("发布任务 %s 租约被其他实例持有，跳过本次执行", release_id)
            return
        current_user = db.query(User).filter(User.username == username).first()
        if not current_user:
            row = db.query(ApplicationRelease).filter(ApplicationRelease.release_id == release_id).first()
            if row:
                row.status = "failed"
                row.summary = "发布用户不存在或已被删除"
                row.finished_at = _now_str()
                db.commit()
            return
        request = BackgroundRequestContext(client_ip=client_ip or "-")
        _execute_release_job_with_session(
            release_id=release_id,
            payload=_release_payload_from_dict(raw_payload),
            request=request,
            db=db,
            current_user=current_user,
        )
    finally:
        # 执行结束（无论成败）立即释放，失败后可立刻重试；owner 不匹配
        # （已被抢占）时为幂等空操作。释放失败则等 TTL 过期自动失效。
        try:
            release_lease(db, lease_key, owner)
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("发布任务 %s 租约释放失败（将等待过期自动失效）: %s", release_id, exc)
        db.close()


@router.post(
    "/{app_id}/releases/preview",
    response_model=ApplicationReleasePreviewResponse,
    dependencies=[Depends(require_permissions("action.apps.deploy"))],
)
def preview_application_release(
    app_id: str,
    payload: ApplicationReleasePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发布预检接口。

    只校验发布计划、罗列目标主机和命令预览，不执行任何远程命令。
    安全边界：
    - 沿用发布接口的可见性、应用级 deploy 动作、模板可见性和凭证授权检查；
    - 不调用 Ansible / SSH / kubectl / helm；
    - 不写库、不落审计；
    - 凭证明文不参与响应，只判断 SSH 凭证是否已绑定。
    """
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "deploy")
    return _plan_application_release(
        app=app,
        payload=payload,
        db=db,
        current_user=current_user,
    )


@router.post(
    "/{app_id}/releases",
    response_model=ApplicationReleaseResponse,
    dependencies=[Depends(require_permissions("action.apps.deploy"))],
)
def create_application_release(
    app_id: str,
    payload: ApplicationReleasePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建项目发布。

    external 只登记发布结果和外部链接；ansible/K8s/Helm 才调用后端固定模板执行。
    """
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "deploy")
    version = (payload.version or "").strip()
    env = (payload.env or "prod").strip()
    component_id = (payload.component_id or "").strip()
    release_type = (payload.release_type or "external").strip()
    release_status = (payload.status or "success").strip()
    external_url = _validate_url(payload.external_url, "外部发布链接", required=False)
    template_id = (payload.template_id or "").strip()
    artifact_ref = (payload.artifact_ref or "").strip()
    rollback_release_id = (payload.rollback_release_id or "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="发布版本不能为空")
    if env not in APP_ENV_VALUES:
        raise HTTPException(status_code=400, detail="发布环境仅支持 prod/test/dev/mixed")
    if release_type not in RELEASE_TYPES:
        raise HTTPException(status_code=400, detail="发布方式仅支持 external/ansible/systemd/supervisor/docker/docker_compose/kubernetes/helm/rollback")
    component_name = ""
    component: ApplicationComponent | None = None
    if component_id:
        component = (
            db.query(ApplicationComponent)
            .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == component_id)
            .first()
        )
        if not component:
            raise HTTPException(status_code=400, detail="发布服务必须属于当前应用")
        component_name = component.name or component_id
    if release_type not in {"external", "ansible"} and not component:
        raise HTTPException(status_code=400, detail="平台执行发布必须选择运行实例")
    if release_type == "systemd":
        deploy_dir = _validate_compose_deploy_path(payload.deploy_dir)
        artifact_ref = _validate_systemd_artifact_ref(artifact_ref)
        artifact_b64 = _validate_systemd_artifact_content_b64(payload.artifact_content_b64)
        artifact_filename = _validate_systemd_artifact_filename(payload.artifact_filename)
        if not artifact_ref and not artifact_b64:
            raise HTTPException(status_code=400, detail="systemd 发布必须提供制品路径、URL 或上传文件")
        artifact_ref = artifact_ref or f"upload:{artifact_filename}"
    if release_type == "supervisor":
        deploy_dir = _validate_compose_deploy_path(payload.deploy_dir)
        artifact_ref = _validate_systemd_artifact_ref(artifact_ref)
        artifact_b64 = _validate_systemd_artifact_content_b64(payload.artifact_content_b64)
        artifact_filename = _validate_systemd_artifact_filename(payload.artifact_filename)
        _validate_supervisor_program_name(component.control_ref if component else "")
        _validate_supervisor_command(payload.supervisor_command)
        _validate_supervisor_user(payload.supervisor_user)
        _validate_supervisor_environment(payload.supervisor_environment)
        _validate_supervisor_runtime(payload.supervisor_runtime)
        if not artifact_ref and not artifact_b64:
            raise HTTPException(status_code=400, detail="Supervisor 发布必须提供制品路径、URL 或上传文件")
        artifact_ref = artifact_ref or f"upload:{artifact_filename}"
    if release_type == "docker":
        artifact_ref = _validate_docker_image_ref(artifact_ref)
        _validate_docker_run_args(payload.docker_run_args)
    if release_type == "docker_compose":
        deploy_dir = _validate_compose_deploy_path(payload.deploy_dir)
        compose_filename = _validate_compose_filename(payload.compose_filename)
        service_name = _validate_compose_service_name(payload.service_name)
        compose_path = f"{deploy_dir.rstrip('/')}/{compose_filename}"
        artifact_ref = f"{compose_path}:{service_name}" if service_name else compose_path

    started_at = _now_str()
    release_id = f"REL-{uuid.uuid4().hex[:8].upper()}"
    operator = str(current_user.name or current_user.username or "未知用户")

    if release_type != "external":
        plan = _ensure_release_plan_allowed(
            app=app,
            payload=payload,
            db=db,
            current_user=current_user,
        )
        plan_targets = list(plan.get("targets") or [])
        selected_resource_ids = [str(item.get("resource_id") or "").strip() for item in plan_targets if str(item.get("resource_id") or "").strip()]
        selected_names = [str(item.get("name") or "").strip() for item in plan_targets if str(item.get("name") or "").strip()]
        row = ApplicationRelease(
            release_id=release_id,
            app_id=app.app_id,
            component_id=component_id,
            component_name=component_name[:120],
            version=version[:120],
            env=env,
            release_type=release_type,
            external_url=external_url,
            template_id=template_id,
            template_name="",
            artifact_ref=artifact_ref[:500],
            rollback_release_id=rollback_release_id[:50],
            target_resource_ids=_dump_json_list(selected_resource_ids),
            target_names=_dump_json_list(selected_names),
            status="running",
            operator=operator[:80],
            started_at=started_at,
            finished_at=None,
            duration_ms=0,
            summary=(payload.summary or "").strip()[:500] or "发布任务已提交",
            output="",
        )
        if release_type == "ansible" and template_id:
            templates = _read_ansible_templates(db)
            index = _find_template_index(templates, template_id)
            if index >= 0:
                row.template_name = str(_normalize_template_row(templates[index]).get("name") or template_id)
        db.add(row)
        db.commit()
        db.refresh(row)
        client_ip = request.client.host if request.client else "-"
        background_tasks.add_task(
            _run_release_job,
            release_id,
            _release_payload_to_dict(payload),
            str(current_user.username or ""),
            str(client_ip or "-"),
        )
        return _serialize_release(row, {app.app_id: app.name})

    if release_status not in EXTERNAL_RELEASE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="外部发布结果仅支持 success/failed/canceled")
    row = ApplicationRelease(
        release_id=release_id,
        app_id=app.app_id,
        component_id=component_id,
        component_name=component_name[:120],
        version=version[:120],
        env=env,
        release_type=release_type,
        external_url=external_url,
        template_id=template_id,
        template_name="",
        artifact_ref=artifact_ref[:500],
        rollback_release_id=rollback_release_id[:50],
        target_resource_ids="",
        target_names="",
        status=release_status,
        operator=operator[:80],
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0,
        summary=((payload.summary or "").strip()[:500] or "外部发布记录"),
        output="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_release(row, {app.app_id: app.name})
