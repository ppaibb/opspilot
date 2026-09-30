"""应用中心配置项与配置版本路由。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from app.core.security import require_risk_ack

router = APIRouter()

@router.get(
    "/config-credential-options",
    response_model=List[CredentialRefOption],
    dependencies=[Depends(require_permissions("module.apps.config"))],
)
def get_application_config_credential_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目配置可引用凭证。

    只返回凭证元信息，不返回密文内容。
    """
    rows = filter_credentials_for_user(db.query(Credential).order_by(Credential.id.desc()).all(), current_user)
    return [
        {
            "cred_id": str(row.cred_id or ""),
            "name": str(row.name or row.cred_id or ""),
            "type": str(row.type or ""),
        }
        for row in rows
    ]

@router.get(
    "/{app_id}/configs",
    response_model=List[ApplicationConfigResponse],
    dependencies=[Depends(require_permissions("module.apps.config"))],
)
def list_application_configs(
    app_id: str,
    env: str = "",
    q: str = "",
    value_type: str = "",
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回项目配置项。"""
    app = _get_visible_application(app_id, db, current_user)
    query = db.query(ApplicationConfig).filter(ApplicationConfig.app_id == app.app_id)
    if env:
        query = query.filter(ApplicationConfig.env == env.strip())
    if value_type:
        query = query.filter(ApplicationConfig.value_type == value_type.strip())
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                ApplicationConfig.key.ilike(f"%{keyword}%"),
                ApplicationConfig.value.ilike(f"%{keyword}%"),
                ApplicationConfig.credential_ref.ilike(f"%{keyword}%"),
                ApplicationConfig.remark.ilike(f"%{keyword}%"),
            )
        )
    rows = query.order_by(ApplicationConfig.id.desc()).offset(offset).limit(limit).all()
    return [_serialize_config(row, db, {app.app_id: app.name}) for row in rows]


@router.get(
    "/{app_id}/configs/{config_id}/revisions",
    response_model=List[ApplicationConfigRevisionResponse],
    dependencies=[Depends(require_permissions("module.apps.config"))],
)
def list_application_config_revisions(
    app_id: str,
    config_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回单个配置项最近版本历史。"""
    app = _get_visible_application(app_id, db, current_user)
    rows = (
        db.query(ApplicationConfigRevision)
        .filter(ApplicationConfigRevision.app_id == app.app_id, ApplicationConfigRevision.config_id == config_id)
        .order_by(ApplicationConfigRevision.id.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_config_revision(row) for row in rows]


@router.post(
    "/{app_id}/configs",
    response_model=ApplicationConfigResponse,
    dependencies=[Depends(require_permissions("action.apps.config.manage"))],
)
def create_application_config(
    app_id: str,
    payload: ApplicationConfigPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建项目配置项。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "config")
    data = _validate_config_payload(payload, db, current_user)
    exists = (
        db.query(ApplicationConfig)
        .filter(
            ApplicationConfig.app_id == app.app_id,
            ApplicationConfig.env == data["env"],
            ApplicationConfig.key == data["key"],
        )
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="同一应用、环境下配置键已存在")
    now = _now_str()
    row = ApplicationConfig(
        config_id=f"CFG-{uuid.uuid4().hex[:8].upper()}",
        app_id=app.app_id,
        created_at=now,
        updated_at=now,
        **data,
    )
    db.add(row)
    _add_config_revision(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        config_id=row.config_id,
        env=row.env,
        key=row.key,
        change_type="create",
        new_value_type=row.value_type,
        new_value=row.value,
        new_credential_ref=row.credential_ref,
        remark=row.remark,
    )
    db.commit()
    db.refresh(row)
    return _serialize_config(row, db, {app.app_id: app.name})


@router.post(
    "/{app_id}/configs/import",
    response_model=ApplicationConfigImportResponse,
    dependencies=[Depends(require_permissions("action.apps.config.manage"))],
)
def import_application_configs(
    app_id: str,
    payload: ApplicationConfigImportPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量导入 `.env` 文本为应用普通配置。

    行为：
    - 解析 `KEY=VALUE` 行，忽略注释和空行；
    - 疑似敏感键在 `allow_sensitive=False` 时跳过，引导用户走凭证引用；
    - 已存在同 env+key 时，`overwrite=False` 跳过，`overwrite=True` 走 CRUD 同事务写入版本历史；
    - 任何单行失败都不会吞掉同批次的其他行；`imported/updated/skipped` 清单在响应里返回。

    安全边界：
    - 不导入为凭证引用，也不生成“待绑定凭证”这种半成品类型；
    - 内容大小、行数、单值长度全部沿用现有配置上限；
    - 审计走现有 `ApplicationConfigRevision` 链路，每条变更保留 create/update 快照；
    - 凭证明文不会进入版本历史。
    """
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "config")

    env = (payload.env or "prod").strip()
    if env not in APP_ENV_VALUES:
        raise HTTPException(status_code=400, detail="配置环境仅支持 prod/test/dev/mixed")

    entries, parse_errors = _parse_env_import_content(payload.content)
    remark = (payload.remark or "").strip()[:500]
    results: list[ApplicationConfigImportItemResult] = []

    # 先把解析期错误转成 skipped，方便前端直接展示。
    for line_no, reason in parse_errors:
        results.append(ApplicationConfigImportItemResult(
            key=f"line-{line_no}",
            status="skipped",
            reason=reason,
        ))

    # 同批次重复 key 只处理最后一条，前面的都标记为 skipped，避免把后续变更误落成 update。
    seen_keys: dict[str, int] = {}
    for idx, (_, key, _value) in enumerate(entries):
        seen_keys[key] = idx

    applied_count = 0
    for idx, (_, key, value) in enumerate(entries):
        if seen_keys.get(key) != idx:
            results.append(ApplicationConfigImportItemResult(
                key=key,
                status="skipped",
                reason="同批次出现重复键，仅保留最后一条",
            ))
            continue

        if len(value) > MAX_CONFIG_PLAIN_VALUE_LENGTH:
            results.append(ApplicationConfigImportItemResult(
                key=key,
                status="skipped",
                reason=f"值长度超过 {MAX_CONFIG_PLAIN_VALUE_LENGTH}，请拆分或改用凭证引用",
            ))
            continue

        is_sensitive = bool(CONFIG_IMPORT_SENSITIVE_PATTERN.search(key))
        if is_sensitive and not payload.allow_sensitive:
            results.append(ApplicationConfigImportItemResult(
                key=key,
                status="skipped",
                reason="疑似敏感键，请在凭证管理新建凭证后改用凭证引用",
            ))
            continue

        existing = (
            db.query(ApplicationConfig)
            .filter(
                ApplicationConfig.app_id == app.app_id,
                ApplicationConfig.env == env,
                ApplicationConfig.key == key,
            )
            .first()
        )

        if existing is not None:
            if not payload.overwrite:
                results.append(ApplicationConfigImportItemResult(
                    key=key,
                    status="skipped",
                    reason="同键已存在，未开启覆盖导入",
                ))
                continue
            # 覆盖只允许覆盖 plain 类型；凭证引用和文件类型避免被意外降级。
            if existing.value_type != "plain":
                results.append(ApplicationConfigImportItemResult(
                    key=key,
                    status="skipped",
                    reason="同键已是凭证引用或文件类型，拒绝被 .env 明文覆盖",
                ))
                continue
            old_value = existing.value or ""
            existing.value = value
            existing.remark = remark or existing.remark
            existing.updated_at = _now_str()
            _add_config_revision(
                db=db,
                current_user=current_user,
                app_id=app.app_id,
                config_id=existing.config_id,
                env=existing.env,
                key=existing.key,
                change_type="update",
                old_value_type="plain",
                old_value=old_value,
                new_value_type="plain",
                new_value=value,
                remark=remark,
            )
            results.append(ApplicationConfigImportItemResult(key=key, status="updated"))
            applied_count += 1
            continue

        now = _now_str()
        row = ApplicationConfig(
            config_id=f"CFG-{uuid.uuid4().hex[:8].upper()}",
            app_id=app.app_id,
            env=env,
            key=key,
            value=value,
            value_type="plain",
            credential_ref="",
            remark=remark,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        _add_config_revision(
            db=db,
            current_user=current_user,
            app_id=app.app_id,
            config_id=row.config_id,
            env=row.env,
            key=row.key,
            change_type="create",
            new_value_type="plain",
            new_value=value,
            remark=remark,
        )
        results.append(ApplicationConfigImportItemResult(key=key, status="created"))
        applied_count += 1

    if applied_count:
        db.commit()
    else:
        # 没有真正写入时回滚仍然安全，避免半途创建的 session 意外残留。
        db.rollback()

    total_lines = len(str(payload.content or "").splitlines())
    return ApplicationConfigImportResponse(
        total_lines=total_lines,
        parsed=len(entries),
        imported=results,
    )


@router.put(
    "/{app_id}/configs/{config_id}",
    response_model=ApplicationConfigResponse,
    dependencies=[Depends(require_permissions("action.apps.config.manage"))],
)
def update_application_config(
    app_id: str,
    config_id: str,
    payload: ApplicationConfigPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新项目配置项。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "config")
    row = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app.app_id, ApplicationConfig.config_id == config_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="配置项不存在")
    data = _validate_config_payload(payload, db, current_user)
    duplicate = (
        db.query(ApplicationConfig)
        .filter(
            ApplicationConfig.app_id == app.app_id,
            ApplicationConfig.env == data["env"],
            ApplicationConfig.key == data["key"],
            ApplicationConfig.config_id != config_id,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=400, detail="同一应用、环境下配置键已存在")
    old_data = {
        "env": row.env or "prod",
        "key": row.key or "",
        "value": row.value or "",
        "value_type": row.value_type or "plain",
        "credential_ref": row.credential_ref or "",
        "remark": row.remark or "",
    }
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _now_str()
    _add_config_revision(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        config_id=row.config_id,
        env=row.env,
        key=row.key,
        change_type="update",
        old_value_type=old_data["value_type"],
        old_value=old_data["value"],
        old_credential_ref=old_data["credential_ref"],
        new_value_type=row.value_type,
        new_value=row.value,
        new_credential_ref=row.credential_ref,
        remark=row.remark or old_data["remark"],
    )
    db.commit()
    db.refresh(row)
    return _serialize_config(row, db, {app.app_id: app.name})


@router.delete(
    "/{app_id}/configs/{config_id}",
    dependencies=[Depends(require_permissions("action.apps.config.manage"))],
)
def delete_application_config(
    app_id: str,
    config_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除项目配置项。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "config")
    row = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app.app_id, ApplicationConfig.config_id == config_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="配置项不存在")
    _add_config_revision(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        config_id=row.config_id,
        env=row.env or "prod",
        key=row.key or "",
        change_type="delete",
        old_value_type=row.value_type or "plain",
        old_value=row.value or "",
        old_credential_ref=row.credential_ref or "",
        remark=row.remark or "",
    )
    db.delete(row)
    db.commit()
    return {"success": True}


@router.get(
    "/{app_id}/configs/{config_id}/compare",
    dependencies=[Depends(require_permissions("module.apps.config"))],
)
def compare_application_config_versions(
    app_id: str,
    config_id: str,
    target_revision_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """对比配置项的两个版本。"""
    app = _get_visible_application(app_id, db, current_user)
    
    # 获取当前配置
    current_config = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app.app_id, ApplicationConfig.config_id == config_id)
        .first()
    )
    if not current_config:
        raise HTTPException(status_code=404, detail="配置项不存在")
    
    # 获取目标历史版本
    target_revision = (
        db.query(ApplicationConfigRevision)
        .filter(
            ApplicationConfigRevision.app_id == app.app_id,
            ApplicationConfigRevision.config_id == config_id,
            ApplicationConfigRevision.id == target_revision_id
        )
        .first()
    )
    if not target_revision:
        raise HTTPException(status_code=404, detail="目标版本不存在")
    
    # 获取当前配置的最新版本记录
    current_revision = (
        db.query(ApplicationConfigRevision)
        .filter(
            ApplicationConfigRevision.app_id == app.app_id,
            ApplicationConfigRevision.config_id == config_id
        )
        .order_by(ApplicationConfigRevision.id.desc())
        .first()
    )
    
    import difflib
    
    # 准备对比的内容
    current_value = str(current_config.value or "")
    target_value = str(target_revision.new_value or target_revision.old_value or "")
    
    # 生成差异
    current_lines = current_value.splitlines(keepends=True)
    target_lines = target_value.splitlines(keepends=True)
    
    diff = list(difflib.unified_diff(
        target_lines,
        current_lines,
        fromfile=f"Version {target_revision.id}",
        tofile="Current",
        lineterm=""
    ))
    
    return {
        "current": {
            "revision_id": current_revision.id if current_revision else None,
            "value": current_value,
            "value_type": current_config.value_type,
            "credential_ref": current_config.credential_ref or "",
            "updated_at": str(current_config.updated_at or ""),
            "updated_by": current_revision.operator if current_revision else "",
        },
        "target": {
            "revision_id": target_revision.id,
            "value": target_value,
            "value_type": target_revision.new_value_type or target_revision.old_value_type,
            "credential_ref": target_revision.new_credential_ref or target_revision.old_credential_ref or "",
            "updated_at": str(target_revision.created_at or ""),
            "updated_by": target_revision.operator,
        },
        "diff_text": "\n".join(diff),
        "has_changes": current_value != target_value,
    }


@router.post(
    "/{app_id}/configs/{config_id}/rollback",
    response_model=ApplicationConfigResponse,
    dependencies=[
        Depends(require_permissions("action.apps.config.manage")),
        # 回滚会直接覆盖线上配置值，与编辑同级：config:rollback 二次确认。
        Depends(require_risk_ack("action.apps.config.manage", "rollback")),
    ],
)
def rollback_application_config(
    app_id: str,
    config_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回滚配置项到指定历史版本。"""
    app = _get_visible_application(app_id, db, current_user)
    _ensure_application_action(app, current_user, "config")
    
    target_revision_id = payload.get("target_revision_id")
    if not target_revision_id:
        raise HTTPException(status_code=400, detail="缺少 target_revision_id 参数")
    
    # 获取当前配置
    row = (
        db.query(ApplicationConfig)
        .filter(ApplicationConfig.app_id == app.app_id, ApplicationConfig.config_id == config_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="配置项不存在")
    
    # 获取目标历史版本
    target_revision = (
        db.query(ApplicationConfigRevision)
        .filter(
            ApplicationConfigRevision.app_id == app.app_id,
            ApplicationConfigRevision.config_id == config_id,
            ApplicationConfigRevision.id == target_revision_id
        )
        .first()
    )
    if not target_revision:
        raise HTTPException(status_code=404, detail="目标版本不存在")
    
    # 保存旧值用于版本记录
    old_value = row.value
    old_value_type = row.value_type
    old_credential_ref = row.credential_ref
    
    # 回滚到目标版本的值
    target_value = target_revision.new_value if target_revision.new_value is not None else target_revision.old_value
    target_value_type = target_revision.new_value_type or target_revision.old_value_type
    target_credential_ref = target_revision.new_credential_ref if target_revision.new_credential_ref is not None else target_revision.old_credential_ref
    
    row.value = target_value
    row.value_type = target_value_type
    row.credential_ref = target_credential_ref or ""
    row.updated_at = _now_str()
    
    # 记录回滚操作到版本历史
    _add_config_revision(
        db=db,
        current_user=current_user,
        app_id=app.app_id,
        config_id=row.config_id,
        env=row.env or "prod",
        key=row.key or "",
        change_type="rollback",
        old_value_type=old_value_type or "plain",
        old_value=old_value or "",
        old_credential_ref=old_credential_ref or "",
        new_value_type=target_value_type or "plain",
        new_value=target_value or "",
        new_credential_ref=target_credential_ref or "",
        remark=f"回滚到版本 {target_revision_id}",
    )
    
    db.commit()
    db.refresh(row)
    return _serialize_config(row, db, {app.app_id: app.name})
