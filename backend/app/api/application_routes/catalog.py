"""应用中心目录、详情、资源绑定与快捷链接路由。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from app.core.security import require_risk_ack
from sqlalchemy import String, cast
from .runtime import _bound_host_assets

router = APIRouter()

@router.get("", response_model=List[ApplicationResponse])
def list_applications(
    q: str = "",
    group_id: Optional[int] = None,
    category: str = "",
    status: str = "",
    environment: str = "",
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回当前用户可见的项目目录。"""
    query = db.query(Application)
    
    # 按分组过滤
    if group_id is not None:
        from app.models.resource_group_member import ResourceGroupMember
        member_app_ids = [
            m.resource_id for m in 
            db.query(ResourceGroupMember).filter(
                ResourceGroupMember.group_id == group_id,
                ResourceGroupMember.resource_type == "application"
            ).all()
        ]
        if member_app_ids:
            query = query.filter(Application.app_id.in_(member_app_ids))
        else:
            # 分组内无应用，返回空列表
            return []
    
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                Application.name.ilike(f"%{keyword}%"),
                Application.app_id.ilike(f"%{keyword}%"),
                Application.description.ilike(f"%{keyword}%"),
                Application.owner.ilike(f"%{keyword}%"),
            )
        )
    if category:
        query = query.filter(Application.category == category.strip())
    if status:
        query = query.filter(Application.status == status.strip())
    if environment:
        query = query.filter(Application.environment == environment.strip())
    if not _can_bypass_application_scope(current_user):
        role = str(current_user.role or "").strip()
        allowed_roles_text = cast(Application.allowed_roles, String)
        query = query.filter(
            or_(
                func.coalesce(allowed_roles_text, "").in_(["", "[]"]),
                Application.allowed_roles.is_(None),
                allowed_roles_text.ilike(f'%"{role}"%'),
            )
        )
    rows = query.order_by(Application.id.desc()).offset(offset).limit(limit).all()
    visible_rows = [row for row in rows if _application_visible_to_user(row, current_user)]
    return _serialize_applications_bulk(visible_rows, db)


@router.get("/role-options", response_model=List[RoleOption])
def get_application_role_options():
    """返回项目可见范围可选权限组。"""
    return [
        {
            "role": item["role"],
            "title": item["title"],
            "description": item.get("description") or "",
        }
        for item in list_role_templates()
    ]


@router.get("/resource-options", response_model=List[ResourceOption])
def get_application_resource_options(db: Session = Depends(get_db)):
    """返回项目可绑定的已有资源。"""
    return list(_resource_options(db).values())

@router.post("", response_model=ApplicationResponse)
def create_application(
    payload: ApplicationPayload,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.apps.manage")),
):
    """创建项目目录项。"""
    data = _validate_payload(payload)
    now = _now_str()
    row = Application(
        app_id=f"APP-{str(uuid.uuid4())[:8].upper()}",
        created_at=now,
        updated_at=now,
        **data,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_application(row, db)


@router.get("/{app_id}/overview", response_model=ApplicationOverviewResponse)
def get_application_overview(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目运维工作台总览数据。"""
    app = _get_visible_application(app_id, db, current_user)
    resources = db.query(ApplicationResource).filter(ApplicationResource.app_id == app.app_id).all()
    resource_stats = {key: 0 for key in RESOURCE_TYPES}
    for item in resources:
        if item.resource_type in resource_stats:
            resource_stats[item.resource_type] += 1

    host_assets = [asset for asset, _ in _bound_host_assets(db, app.app_id).values()]
    host_stats = {
        "total": len(host_assets),
        "online": len([item for item in host_assets if str(item.type or "").lower() == "healthy"]),
        "offline": len([item for item in host_assets if str(item.type or "").lower() != "healthy"]),
    }

    vm_ids = [row.resource_id for row in resources if row.resource_type == "vm"]
    vm_rows = db.query(VirtualMachine).filter(VirtualMachine.vm_id.in_(vm_ids)).all() if vm_ids else []
    vm_stats = {
        "total": len(vm_rows),
        "running": len([item for item in vm_rows if str(item.status or "").lower() == "running"]),
        "stopped": len([item for item in vm_rows if str(item.status or "").lower() != "running"]),
    }

    domain_ids = [int(row.resource_id) for row in resources if row.resource_type == "domain" and str(row.resource_id).isdigit()]
    domains = db.query(DomainRecord).filter(DomainRecord.id.in_(domain_ids)).all() if domain_ids else []
    cert_ids = [row.resource_id for row in resources if row.resource_type == "certificate"]
    certs = db.query(CertificateRecord).filter(CertificateRecord.cert_id.in_(cert_ids)).all() if cert_ids else []
    expiry_stats = {
        "domain_expiring": len([item for item in domains if _is_expiring_soon(item.expires_at)]),
        "certificate_expiring": len([item for item in certs if _is_expiring_soon(item.not_after)]),
        # 当前告警没有项目维度标签，二期先不做误关联。
        "active_alerts": 0,
    }

    release_rows = (
        db.query(ApplicationRelease)
        .filter(ApplicationRelease.app_id == app.app_id)
        .order_by(ApplicationRelease.id.desc())
        .limit(3)
        .all()
    )
    config_rows = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app.app_id)
        .order_by(ApplicationConfig.id.desc())
        .limit(3)
        .all()
    )
    app_names = {app.app_id: app.name}
    return {
        "app": _serialize_application(app, db),
        "resource_stats": resource_stats,
        "host_stats": host_stats,
        "vm_stats": vm_stats,
        "expiry_stats": expiry_stats,
        "recent_releases": [_serialize_release(row, app_names) for row in release_rows],
        "recent_configs": [_serialize_config(row, db, app_names) for row in config_rows],
    }


@router.get(
    "/{app_id}/timeline",
    response_model=ApplicationTimelineResponse,
)
def get_application_timeline(
    app_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回应用最近事件的聚合时间线。

    安全边界：
    1. 仅读取当前应用已有数据（发布记录、配置版本、运行实例最近控制）；
    2. 告警只走已配置 Alertmanager 的只读查询，未配置或失败时静默降级为空；
    3. 不返回凭证明文、VictoriaMetrics / VictoriaLogs 内网地址或监控组件凭据；
    4. 权限继承应用可见性，不引入额外写操作。
    """
    app = _get_visible_application(app_id, db, current_user)
    return _build_application_timeline(app=app, db=db, limit=limit)


@router.get(
    "/{app_id}/health-timeline",
    response_model=ApplicationHealthTimelineResponse,
)
def get_application_health_timeline(
    app_id: str,
    hours: int = Query(default=24, ge=1, le=168),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回应用近 N 小时健康快照，用于健康时间带展示。

    安全边界：
    1. 仅读取巡检器定时生成的快照，不触发任何 SSH、kubectl 或 Alertmanager 请求；
    2. 健康原因最多保留 4 条，和列表页展示口径一致；
    3. 权限继承应用可见性；前端不能写入快照。
    """
    app = _get_visible_application(app_id, db, current_user)
    cutoff = datetime.now() - timedelta(hours=int(hours))
    rows = (
        db.query(ApplicationHealthSnapshot)
        .filter(
            ApplicationHealthSnapshot.app_id == app.app_id,
            ApplicationHealthSnapshot.snapshot_time >= cutoff,
        )
        .order_by(ApplicationHealthSnapshot.id.asc())
        .all()
    )
    snapshots = [
        {
            "snapshot_time": format_app_datetime(row.snapshot_time),
            "health_status": row.health_status or "unknown",
            "health_label": row.health_label or "",
            "health_reasons": _load_json_list(row.health_reasons),
            "component_total": int(row.component_total or 0),
            "component_active": int(row.component_active or 0),
            "host_total": int(row.host_total or 0),
            "host_healthy": int(row.host_healthy or 0),
            "active_alert_count": int(row.active_alert_count or 0),
            "domain_expiring_count": int(row.domain_expiring_count or 0),
            "certificate_expiring_count": int(row.certificate_expiring_count or 0),
        }
        for row in rows
    ]
    return {
        "app_id": app.app_id,
        "app_name": app.name or "",
        "hours": int(hours),
        "now": format_app_datetime(_now_str()),
        "snapshots": snapshots,
    }

@router.get("/{app_id}", response_model=ApplicationDetailResponse)
def get_application_detail(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目详情、资源绑定和快捷链接。"""
    app = _get_visible_application(app_id, db, current_user)
    resources = (
        db.query(ApplicationResource)
        .filter(ApplicationResource.app_id == app.app_id)
        .order_by(ApplicationResource.id.asc())
        .all()
    )
    links = (
        db.query(ApplicationLink)
        .filter(ApplicationLink.app_id == app.app_id)
        .order_by(ApplicationLink.id.asc())
        .all()
    )
    components = (
        db.query(ApplicationComponent)
        .filter(ApplicationComponent.app_id == app.app_id)
        .order_by(ApplicationComponent.id.asc())
        .all()
    )
    return {
        "app": _serialize_application(app, db),
        "resources": [
            {
                "id": row.id,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "resource_name": row.resource_name or row.resource_id,
                "created_at": format_app_datetime(row.created_at),
            }
            for row in resources
        ],
        "components": [_serialize_component(row, db) for row in components],
        "links": [
            {
                "id": row.id,
                "title": row.title,
                "url": row.url,
                "link_type": row.link_type,
                "created_at": format_app_datetime(row.created_at),
            }
            for row in links
        ],
        "observability": _build_application_observability_context(
            app_id=app.app_id,
            app_name=app.name or "",
            resources=resources,
            components=components,
            db=db,
        ),
        "capabilities": _application_capabilities(app, current_user),
    }


@router.get("/{app_id}/dependencies")
def get_application_dependencies(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回应用的资源、运行实例和发布影响范围。"""
    app = _get_visible_application(app_id, db, current_user)
    resources = db.query(ApplicationResource).filter(ApplicationResource.app_id == app.app_id).all()
    components = db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app.app_id).all()
    release_count = db.query(ApplicationRelease).filter(ApplicationRelease.app_id == app.app_id).count()
    return {
        "app_id": app.app_id,
        "app_name": app.name or "",
        "resources": [
            {"resource_type": row.resource_type, "resource_id": row.resource_id, "name": row.resource_name or row.resource_id}
            for row in resources
        ],
        "components": [
            {"component_id": row.component_id, "name": row.name or row.component_id, "deployment_type": row.deployment_type or "", "resource_refs": _load_json_list(row.resource_refs)}
            for row in components
        ],
        "release_count": release_count,
    }


@router.put("/{app_id}", response_model=ApplicationResponse)
def update_application(
    app_id: str,
    payload: ApplicationPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.apps.manage")),
):
    """更新项目目录项。"""
    row = _get_visible_application(app_id, db, current_user)
    data = _validate_payload(payload)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _now_str()
    db.commit()
    db.refresh(row)
    return _serialize_application(row, db)


@router.delete("/{app_id}")
def delete_application(
    app_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.apps.manage")),
    _risk=Depends(require_risk_ack("action.apps.manage", "delete")),
):
    """删除项目目录项及其目录级绑定（apps:delete 二次确认）。"""
    row = _get_visible_application(app_id, db, current_user)
    db.query(ApplicationResource).filter(ApplicationResource.app_id == app_id).delete()
    db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app_id).delete()
    db.query(ApplicationLink).filter(ApplicationLink.app_id == app_id).delete()
    db.query(ApplicationConfig).filter(ApplicationConfig.app_id == app_id).delete()
    db.query(ApplicationRelease).filter(ApplicationRelease.app_id == app_id).delete()
    db.delete(row)
    db.commit()
    return {"success": True}


@router.put("/{app_id}/resources", response_model=List[ApplicationResourceResponse])
def replace_application_resources(
    app_id: str,
    payload: List[ApplicationResourcePayload],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.apps.manage")),
):
    """替换项目资源绑定。"""
    app = _get_visible_application(app_id, db, current_user)
    previous_host_ids = [
        str(row.resource_id or "").strip()
        for row in db.query(ApplicationResource)
        .filter(ApplicationResource.app_id == app_id, ApplicationResource.resource_type == "host")
        .all()
    ]
    options = _resource_options(db)
    normalized: list[ApplicationResourcePayload] = []
    seen: set[tuple[str, str]] = set()
    for item in payload or []:
        resource_type = (item.resource_type or "").strip()
        resource_id = (item.resource_id or "").strip()
        key = (resource_type, resource_id)
        if resource_type not in RESOURCE_TYPES:
            raise HTTPException(status_code=400, detail=f"未知资源类型: {resource_type}")
        if key not in options:
            raise HTTPException(status_code=400, detail=f"资源不存在: {resource_type}/{resource_id}")
        if key in seen:
            continue
        seen.add(key)
        normalized.append(ApplicationResourcePayload(resource_type=resource_type, resource_id=resource_id))

    db.query(ApplicationResource).filter(ApplicationResource.app_id == app_id).delete()
    now = _now_str()
    for item in normalized:
        option = options[(item.resource_type, item.resource_id)]
        db.add(
            ApplicationResource(
                app_id=app_id,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                resource_name=option.name,
                created_at=now,
            )
        )
    app.updated_at = now
    allowed_refs = {_resource_ref_key(item.resource_type, item.resource_id) for item in normalized}
    for component in db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app_id).all():
        # 资源绑定被移除后，服务引用也要同步裁剪，避免留下悬空引用。
        component.resource_refs = _dump_json_list([ref for ref in _load_json_list(component.resource_refs) if ref in allowed_refs])
        component.updated_at = now
    db.commit()

    # 应用绑定变更后，对涉及的主机 best-effort 重新下发 Alloy 策略，
    # 把 app_id label 尽快推到已纳管 Agent；失败会静默忽略，不影响资源绑定本身。
    current_host_ids = [item.resource_id for item in normalized if item.resource_type == "host"]
    affected_host_ids = sorted({*previous_host_ids, *current_host_ids})
    if affected_host_ids:
        try:
            # 延迟导入避免与 monitoring 模块产生循环依赖。
            from app.api.monitoring import resync_alloy_policy_for_asset_ids  # noqa: WPS433

            resync_alloy_policy_for_asset_ids(
                db=db,
                asset_ids=affected_host_ids,
                current_user=current_user,
            )
        except Exception:
            # 监控链路问题不阻塞应用中心的资源绑定操作。
            pass

    return [
        {
            "id": row.id,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "resource_name": row.resource_name or row.resource_id,
            "created_at": format_app_datetime(row.created_at),
        }
        for row in db.query(ApplicationResource).filter(ApplicationResource.app_id == app_id).order_by(ApplicationResource.id.asc()).all()
    ]


@router.put("/{app_id}/links", response_model=List[ApplicationLinkResponse])
def replace_application_links(
    app_id: str,
    payload: List[ApplicationLinkPayload],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.apps.manage")),
):
    """替换应用快捷链接。"""
    app = _get_visible_application(app_id, db, current_user)
    normalized: list[ApplicationLinkPayload] = []
    for item in (payload or [])[:12]:
        title = (item.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="链接标题不能为空")
        link_type = (item.link_type or "external").strip()
        if link_type not in APP_ENTRY_TYPES:
            raise HTTPException(status_code=400, detail="链接类型非法")
        normalized.append(
            ApplicationLinkPayload(
                title=title[:120],
                url=_validate_url(item.url, "链接地址", required=True),
                link_type=link_type,
            )
        )

    db.query(ApplicationLink).filter(ApplicationLink.app_id == app_id).delete()
    now = _now_str()
    for item in normalized:
        db.add(
            ApplicationLink(
                app_id=app_id,
                title=item.title,
                url=item.url,
                link_type=item.link_type,
                created_at=now,
            )
        )
    app.updated_at = now
    db.commit()
    return [
        {
            "id": row.id,
            "title": row.title,
            "url": row.url,
            "link_type": row.link_type,
            "created_at": format_app_datetime(row.created_at),
        }
        for row in db.query(ApplicationLink).filter(ApplicationLink.app_id == app_id).order_by(ApplicationLink.id.asc()).all()
    ]

