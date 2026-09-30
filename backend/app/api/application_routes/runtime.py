"""应用中心运行实例、扫描与控制路由。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from app.api.notifications import NotificationCreate, create_notification
from app.services.application_runtime import (
    build_compose_project_discovery_command,
    build_compose_preflight_module_args,
    build_compose_update_module_args,
    build_docker_container_release_module_args,
)

router = APIRouter()


def _notify_application_deploy(
    db: Session,
    current_user: User,
    app: Application,
    deployment_type: str,
    ok: bool,
    detail: str,
) -> None:
    """向当前操作人发送应用部署终态通知；通知失败不改变部署结果。"""
    try:
        create_notification(
            db,
            data=NotificationCreate(
                user_id=current_user.id,
                type="success" if ok else "error",
                category="application",
                title=f"{app.name} {'部署完成' if ok else '部署失败'}",
                content=f"{deployment_type}：{detail}"[:1000],
                link=f"/apps/{app.app_id}",
                resource_id=app.app_id,
            ),
        )
    except Exception:
        pass


@router.post(
    "/{app_id}/compose/preflight",
    response_model=ApplicationComposePreflightResponse,
    dependencies=[Depends(require_permissions("action.apps.deploy"))],
)
def preflight_application_compose_project(
    app_id: str,
    payload: ApplicationComposePreflightRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """只读检查 Compose 项目部署条件，不写文件、不拉镜像、不操作容器。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "deploy")
    bound_hosts = _bound_host_assets(db, app.app_id)
    resource_id = str(payload.resource_id or "").strip()
    if resource_id not in bound_hosts:
        raise HTTPException(status_code=400, detail="预检目标不属于当前应用或主机不可用")

    module_args, compose_file = build_compose_preflight_module_args(
        project_name=payload.project_name,
        deploy_dir=payload.deploy_dir,
        compose_filename=payload.compose_filename,
        compose_content=payload.compose_content,
        env_filename=payload.env_filename,
        env_content=payload.env_content,
    )
    result = _execute_runtime_ssh_task(
        asset=bound_hosts[resource_id][0],
        module_args=module_args,
        request=request,
        db=db,
        current_user=current_user,
        audit_action="Compose 部署预检",
        audit_resource=f"compose:preflight:{payload.project_name}",
    )
    output = str(result.stdout or "").strip()
    checks = [
        {"key": "ssh", "label": "SSH 连接", "status": "success", "message": "连接成功"},
        {"key": "docker", "label": "Docker daemon", "status": "success" if result.ok else "failed", "message": "可用" if result.ok else "不可用或无权限"},
        {"key": "compose", "label": "Compose CLI 与配置", "status": "success" if result.ok else "failed", "message": "配置解析通过" if result.ok else str(result.stderr or result.notice or "检查失败")[-300:]},
        {"key": "services", "label": "Compose 服务", "status": "success" if output else "warning", "message": "、".join(output.splitlines()) if output else "未解析到服务"},
        {"key": "directory", "label": "部署目录", "status": "success" if result.ok else "failed", "message": "目录不存在或为空，可安全创建" if result.ok else "目录检查未通过；非空目录不会被覆盖"},
    ]
    return ApplicationComposePreflightResponse(
        ok=result.ok,
        checks=checks,
        compose_file=compose_file,
        cli="auto",
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _deploy_new_runtime_instance(
    app: Application,
    payload: ApplicationComponentPayload,
    data: dict,
    request: Request,
    db: Session,
    current_user: User,
) -> None:
    """全新部署模式：在绑定主机上真正拉起新实例，成功后才允许落库。

    业务取舍：纳管（manage）只登记已存在的实例，而部署（deploy）会远程执行命令拉起
    全新实例，风险更高。因此这里强制 `action.apps.deploy` 权限 + 应用 deploy_roles 范围，
    与发布页保持同一鉴权口径。命令全部由后端白名单模板生成，前端只下发镜像/Compose 内容。
    """
    _ensure_application_action(app, current_user, "deploy")

    # 部署目标必须是当前应用已绑定的主机资源，避免借部署接口越界操作任意主机。
    bound_hosts = _bound_host_assets(db, app.app_id)
    selected_resource_ids = list(dict.fromkeys([
        ref.split(":", 1)[1].strip()
        for ref in payload.resource_refs
        if ref.startswith("host:") and ref.split(":", 1)[1].strip()
    ]))
    if not selected_resource_ids:
        raise HTTPException(status_code=400, detail="全新部署必须选择至少一个主机资源")
    selected_assets = [bound_hosts[item][0] for item in selected_resource_ids if item in bound_hosts]
    if not selected_assets:
        raise HTTPException(status_code=400, detail="引用的主机资源不可用")

    deployment_type = data["deployment_type"]
    control_ref = data["control_ref"]
    cfg = payload.deploy_config
    if deployment_type == "docker":
        if not cfg or not cfg.image_ref:
            raise HTTPException(status_code=400, detail="Docker 部署必须提供镜像地址")
        module_args = build_docker_container_release_module_args(
            container_name=control_ref,
            image_ref=cfg.image_ref,
            docker_run_args=cfg.docker_run_args or "",
        )
    elif deployment_type == "docker_compose":
        # control_ref 已由 _validate_component_payload 归一化为 compose=...;service=...;env=... 形式。
        compose_file, service_name, env_file = _parse_compose_control_ref(control_ref)
        compose_content = str((cfg.compose_content if cfg else "") or payload.compose_content or "").strip()
        if not compose_content:
            raise HTTPException(status_code=400, detail="Docker Compose 部署必须提供 YAML 内容")
        module_args = build_compose_update_module_args(
            compose_file=compose_file,
            service_name=service_name,
            compose_content=compose_content,
            env_file=env_file,
            env_content=payload.compose_env_content or "",
            pull_image=payload.pull_image,
            project_name=(payload.name or "").split(" / ", 1)[0].strip(),
        )
    else:
        raise HTTPException(status_code=400, detail=f"暂不支持该类型（{deployment_type}）的直接部署，请使用扫描纳管")

    # 执行通道复用运行控制链路：单机优先 SSH 直连，多机走 Ansible，凭证与输出截断由下层处理。
    execution_channel = _resolve_runtime_execution_channel("auto", len(selected_assets))
    if execution_channel == "ssh_shell":
        result = _execute_runtime_ssh_task(
            asset=selected_assets[0],
            module_args=module_args,
            request=request,
            db=db,
            current_user=current_user,
            audit_resource=f"runtime:deploy:{control_ref}",
        )
    else:
        ansible_asset_ids = [_ansible_asset_identity(asset) for asset in selected_assets]
        result = _execute_ansible_task(
            payload=AnsibleRunRequest(
                run_all=False,
                asset_ids=ansible_asset_ids,
                module="shell",
                module_args=module_args,
                become=True,
                become_user="root",
            ),
            request=request,
            db=db,
            current_user=current_user,
            run_source=f"application_deploy:{deployment_type}",
        )

    # 同步执行，部署真正成功才落库；失败把脱敏后的错误输出回显给前端。
    if not result.ok:
        stderr = (result.stderr or result.stdout or "未知错误").strip()
        raise HTTPException(status_code=500, detail=f"部署执行失败（退出码 {result.code}）：\n{stderr}")


def _discover_deployed_compose_components(
    app: Application,
    payload: ApplicationComponentPayload,
    resource_id: str,
    asset: Asset,
    request: Request,
    db: Session,
    current_user: User,
) -> list[ApplicationComponent]:
    """部署成功后按 project 标签发现 Compose services，并幂等登记部署组件。"""
    project_name = (payload.name or "").strip()
    result = _execute_runtime_ssh_task(
        asset=asset,
        module_args=build_compose_project_discovery_command(project_name),
        request=request,
        db=db,
        current_user=current_user,
        audit_action="Compose 部署后发现",
        audit_resource=f"compose:discover:{project_name}",
    )
    if not result.ok:
        raise HTTPException(status_code=500, detail=f"Compose 项目已启动，但服务发现失败：{result.stderr or result.notice}")

    rows: list[ApplicationComponent] = []
    now = _now_str()
    for item in _parse_runtime_discovery_output("docker_compose", result.stdout):
        component_payload = ApplicationComponentPayload(
            name=item["name"],
            deployment_type="docker_compose",
            control_ref=item["control_ref"],
            env=payload.env or "prod",
            status=item["status"],
            resource_refs=[f"host:{resource_id}"],
            description=item.get("description", ""),
            create_mode="manage",
        )
        data = _validate_component_payload(component_payload, db, app.app_id)
        existing = _find_existing_component(db, app.app_id, data)
        if existing:
            rows.append(existing)
            continue
        row = ApplicationComponent(
            component_id=f"CMP-{str(uuid.uuid4())[:8].upper()}",
            app_id=app.app_id,
            created_at=now,
            updated_at=now,
            **data,
        )
        db.add(row)
        rows.append(row)
    return rows

@router.get("/{app_id}/components", response_model=List[ApplicationComponentResponse])
def list_application_components(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回应用服务列表。"""
    app = _get_visible_application(app_id, db, current_user)
    rows = (
        db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == app.app_id)
        .order_by(ApplicationComponent.id.asc())
        .all()
    )
    return [_serialize_component(row, db) for row in rows]


@router.post(
    "/{app_id}/components",
    dependencies=[Depends(require_permissions("action.apps.manage"))],
)
def create_application_component(
    app_id: str,
    payload: ApplicationComponentPayload,
    async_deploy: bool = False,  # 新增参数：是否异步部署
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新增运行实例。

    支持两种模式（create_mode）：
    - manage（默认）：纳管主机上已存在的实例，仅登记记录；
    - deploy：在绑定主机上真正部署一个全新实例（docker / docker_compose），
      由 _deploy_new_runtime_instance 走 Ansible/SSH 白名单命令执行，成功后才落库。
    
    异步部署模式（async_deploy=true）：
    - 立即返回任务 ID
    - 后台执行部署
    - 通过 /tasks/{task_id} 查询进度
    - 完成后发送站内通知
    """
    app = _get_visible_application(app_id, db, current_user)
    data = _validate_component_payload(payload, db, app.app_id)
    existing = _find_existing_component(db, app.app_id, data)
    if existing:
        return _serialize_component(existing, db)
    
    is_deploy = str(payload.create_mode or "manage").strip() == "deploy"
    
    # 如果是部署模式且启用异步
    if is_deploy and async_deploy and data["deployment_type"] == "docker_compose":
        from app.services.deployment_task_service import DeploymentTaskService
        
        # 创建异步任务
        deployment_config = {
            "payload": payload.dict(),
            "data": data,
        }
        
        task_id = DeploymentTaskService.start_deployment_task(
            db=db,
            app_id=app.app_id,
            task_type="deploy",
            deployment_config=deployment_config,
            created_by=current_user.username
        )
        
        return {
            "task_id": task_id,
            "status": "pending",
            "message": "部署任务已创建，正在后台执行"
        }
    
    # 原有的同步部署逻辑
    if is_deploy:
        try:
            _deploy_new_runtime_instance(app, payload, data, request, db, current_user)
            if data["deployment_type"] == "docker_compose":
                resource_ids = [ref.split(":", 1)[1] for ref in _load_json_list(data["resource_refs"]) if ref.startswith("host:")]
                bound_hosts = _bound_host_assets(db, app.app_id)
                discovered = _discover_deployed_compose_components(
                    app=app,
                    payload=payload,
                    resource_id=resource_ids[0],
                    asset=bound_hosts[resource_ids[0]][0],
                    request=request,
                    db=db,
                    current_user=current_user,
                )
                if not discovered:
                    raise HTTPException(status_code=500, detail="Compose 项目已启动，但未发现任何服务组件，请检查 project 标签")
                app.updated_at = _now_str()
                db.commit()
                db.refresh(discovered[0])
                _notify_application_deploy(db, current_user, app, "Docker Compose", True, f"项目 {payload.name} 已启动并登记 {len(discovered)} 个组件")
                return _serialize_component(discovered[0], db)
        except HTTPException as exc:
            db.rollback()
            _notify_application_deploy(db, current_user, app, data["deployment_type"], False, str(exc.detail))
            raise
    now = _now_str()
    row = ApplicationComponent(
        component_id=f"CMP-{str(uuid.uuid4())[:8].upper()}",
        app_id=app.app_id,
        created_at=now,
        updated_at=now,
        **data,
    )
    app.updated_at = now
    db.add(row)
    _sync_runtime_component_configs(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        component_id=row.component_id,
        env=row.env or "prod",
        control_ref=row.control_ref or "",
        payload=payload,
    )
    db.commit()
    db.refresh(row)
    if is_deploy:
        _notify_application_deploy(db, current_user, app, data["deployment_type"], True, f"组件 {row.name} 已部署并登记")
    return _serialize_component(row, db)


@router.put(
    "/{app_id}/components/{component_id}",
    response_model=ApplicationComponentResponse,
    dependencies=[Depends(require_permissions("action.apps.manage"))],
)
def update_application_component(
    app_id: str,
    component_id: str,
    payload: ApplicationComponentPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新应用服务。"""
    app = _get_visible_application(app_id, db, current_user)
    row = (
        db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == component_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="服务不存在")
    data = _validate_component_payload(payload, db, app.app_id)
    for key, value in data.items():
        setattr(row, key, value)
    now = _now_str()
    row.updated_at = now
    app.updated_at = now
    _sync_runtime_component_configs(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        component_id=row.component_id,
        env=row.env or "prod",
        control_ref=row.control_ref or "",
        payload=payload,
    )
    db.commit()
    db.refresh(row)
    return _serialize_component(row, db)


@router.post(
    "/{app_id}/components/batch-delete",
    dependencies=[Depends(require_permissions("action.apps.manage"))],
)
def batch_delete_application_components(
    app_id: str,
    payload: ApplicationComponentBatchDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量移除运行实例登记，不停止主机上的实际进程。"""
    app = _get_visible_application(app_id, db, current_user)
    component_ids = list(dict.fromkeys(payload.component_ids))
    rows = (
        db.query(ApplicationComponent)
        .filter(
            ApplicationComponent.app_id == app.app_id,
            ApplicationComponent.component_id.in_(component_ids),
        )
        .all()
    )
    for row in rows:
        db.delete(row)
    app.updated_at = _now_str()
    db.commit()
    return {"success": True, "deleted_count": len(rows)}


@router.delete(
    "/{app_id}/components/{component_id}",
    dependencies=[Depends(require_permissions("action.apps.manage"))],
)
def delete_application_component(
    app_id: str,
    component_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除应用服务。"""
    app = _get_visible_application(app_id, db, current_user)
    row = (
        db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == component_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="服务不存在")
    app.updated_at = _now_str()
    db.delete(row)
    db.commit()
    return {"success": True}


@router.post(
    "/{app_id}/runtime-discovery",
    response_model=ApplicationRuntimeDiscoveryResponse,
    dependencies=[Depends(require_permissions("action.apps.manage"))],
)
def discover_application_runtime_instances(
    app_id: str,
    payload: ApplicationRuntimeDiscoveryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """扫描当前应用已绑定主机上的运行实例候选。

    扫描只做只读探测，不直接创建运行实例；用户确认候选后仍走 components 创建接口。
    """
    app = _get_visible_application(app_id, db, current_user)
    bound_hosts = _bound_host_assets(db, app.app_id)
    if not bound_hosts:
        raise HTTPException(status_code=400, detail="当前应用未绑定主机，无法扫描运行实例")

    requested_ids = list(dict.fromkeys([str(item or "").strip() for item in payload.asset_ids if str(item or "").strip()]))
    selected_ids = requested_ids or list(bound_hosts.keys())
    unknown_ids = [item for item in selected_ids if item not in bound_hosts]
    if unknown_ids:
        raise HTTPException(status_code=400, detail=f"扫描目标不属于当前应用: {', '.join(unknown_ids[:3])}")

    discovery_types = list(dict.fromkeys([str(item or "").strip() for item in payload.types if str(item or "").strip()]))
    if not discovery_types:
        discovery_types = ["supervisor", "docker", "docker_compose"]
    unknown_types = [item for item in discovery_types if item not in RUNTIME_DISCOVERY_TYPES]
    if unknown_types:
        raise HTTPException(status_code=400, detail=f"扫描类型不支持: {', '.join(unknown_types[:3])}")

    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    candidates_count_by_type: dict[str, int] = {}
    truncated_types: set[str] = set()
    filtered_count = 0
    managed_components = db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app.app_id).all()
    managed_by_identity = {
        _component_identity(row.deployment_type, row.control_ref, row.resource_refs): row.component_id
        for row in managed_components
    }

    for resource_id in selected_ids:
        asset, asset_name = bound_hosts[resource_id]
        for discovery_type in discovery_types:
            if candidates_count_by_type.get(discovery_type, 0) >= MAX_RUNTIME_DISCOVERY_CANDIDATES:
                if discovery_type not in truncated_types:
                    truncated_types.add(discovery_type)
                    errors.append(f"候选数量已达到上限 {MAX_RUNTIME_DISCOVERY_CANDIDATES}，类型 {discovery_type} 的后续结果已截断")
                continue

            command = _build_runtime_discovery_command(discovery_type)
            ok, stdout, error = _run_runtime_discovery_command(asset, command, db, current_user)
            if not ok:
                errors.append(f"{asset_name} / {discovery_type}: {error}")
                continue

            for item in _parse_runtime_discovery_output(discovery_type, stdout):
                if discovery_type == "systemd" and not payload.include_system_services and _is_system_runtime_service(item["control_ref"]):
                    filtered_count += 1
                    continue
                current_count = candidates_count_by_type.get(discovery_type, 0)
                if current_count >= MAX_RUNTIME_DISCOVERY_CANDIDATES:
                    if discovery_type not in truncated_types:
                        truncated_types.add(discovery_type)
                        errors.append(f"候选数量已达到上限 {MAX_RUNTIME_DISCOVERY_CANDIDATES}，类型 {discovery_type} 的后续结果已截断")
                    break
                identity = _component_identity(item["deployment_type"], item["control_ref"], [f"host:{resource_id}"])
                managed_component_id = managed_by_identity.get(identity, "")
                candidates.append(
                    {
                        "asset_id": resource_id,
                        "asset_name": asset_name,
                        "deployment_type": item["deployment_type"],
                        "name": item["name"],
                        "control_ref": item["control_ref"],
                        "status": item["status"],
                        "description": item.get("description", ""),
                        "project_name": item.get("project_name", ""),
                        "management_status": "managed" if managed_component_id else "new",
                        "managed_component_id": managed_component_id,
                    }
                )
                candidates_count_by_type[discovery_type] = current_count + 1

    return {
        "scanned_count": len(selected_ids),
        "candidates": candidates,
        "errors": errors[:20],
        "filtered_count": filtered_count,
    }


@router.post(
    "/{app_id}/components/{component_id}/actions",
    response_model=ApplicationRuntimeActionResponse,
    dependencies=[Depends(require_permissions("action.apps.control"))],
)
def control_application_component(
    app_id: str,
    component_id: str,
    payload: ApplicationRuntimeActionPayload,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """控制应用运行实例。

    该接口只接收动作，不接收命令。后端按运行类型生成固定白名单命令，
    并复用 Ansible 执行链路完成 SSH 连接、凭证校验、审计和输出截断。
    """
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "control")
    row = (
        db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == app.app_id, ApplicationComponent.component_id == component_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="运行实例不存在")

    action = str(payload.action or "").strip().lower()
    if action == "update":
        raise HTTPException(status_code=400, detail="运行实例只支持生命周期控制，更新发布请在发布部署页执行")
    module_args = _build_runtime_control_module_args(
        row.deployment_type or "",
        row.control_ref or "",
        action,
        row.runtime or "",
    )
    bound_hosts = _bound_host_assets(db, app.app_id)
    refs = _load_json_list(row.resource_refs)
    selected_resource_ids = [
        ref.split(":", 1)[1].strip()
        for ref in refs
        if ref.startswith("host:") and ref.split(":", 1)[1].strip()
    ]
    selected_resource_ids = list(dict.fromkeys(selected_resource_ids))
    if not selected_resource_ids:
        raise HTTPException(status_code=400, detail="运行实例未绑定主机资源，无法执行启停控制")

    unknown_targets = [item for item in selected_resource_ids if item not in bound_hosts]
    if unknown_targets:
        raise HTTPException(status_code=400, detail="运行实例引用了未绑定或不可用的主机资源")

    selected_assets = [bound_hosts[item][0] for item in selected_resource_ids]
    execution_channel = _resolve_runtime_execution_channel(row.execution_channel or "auto", len(selected_assets))
    if execution_channel == "ssh_shell":
        result = _execute_runtime_ssh_task(
            asset=selected_assets[0],
            module_args=module_args,
            request=request,
            db=db,
            current_user=current_user,
            audit_resource=f"runtime:{row.component_id}:{action}",
        )
    else:
        ansible_asset_ids = [_ansible_asset_identity(asset) for asset in selected_assets]
        result = _execute_ansible_task(
            payload=AnsibleRunRequest(
                run_all=False,
                asset_ids=ansible_asset_ids,
                module="shell",
                module_args=module_args,
                become=True,
                become_user="root",
            ),
            request=request,
            db=db,
            current_user=current_user,
            run_source=f"application_control:{action}",
        )

    next_status = _derive_runtime_status(
        action=action,
        deployment_type=row.deployment_type or "",
        ok=result.ok,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if next_status:
        row.status = next_status

    now = _now_str()
    row.last_control_action = action
    row.last_control_result = "success" if result.ok else "failed"
    row.last_control_at = now
    row.last_control_duration_ms = result.duration_ms
    row.last_control_output = _truncate_runtime_control_output(result.stdout, result.stderr)
    row.updated_at = now
    app.updated_at = now
    db.commit()

    return ApplicationRuntimeActionResponse(
        ok=result.ok,
        action=action,
        runtime_type=row.deployment_type or "",
        target_count=result.target_count,
        duration_ms=result.duration_ms,
        code=result.code,
        execution_channel=execution_channel,
        stdout=result.stdout,
        stderr=result.stderr,
        command_preview=result.command_preview,
        notice=result.notice or "运行实例控制完成",
    )
