"""数据库管理接口（类 Yearning 的 SQL 工单平台，P0）。

四组能力：
1. 数据源（实例）：CRUD + 连通测试（密码加密落库，接口不回传）；
2. SQL 工单：提交（自动过规则引擎）→ 多级审批（审批流可配置）→ 执行 → 逐条结果；
   高危（审核 block）直接拒绝提交；提交（submit）/ 执行（execute）/ 审批（approve）
   三权分离；禁止自审批；查询工单终审通过自动生成时限查询授权；
3. 自助查询：只读语句执行（上限 500 行、60 秒），需本人名下生效中的查询授权
   （管理员豁免），全部落查询日志；
4. 查询授权：工单申请 / 管理员手动授权 / 吊销 / 到期自动回收。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.rbac import normalize_role_name
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.database import DbInstance, DbQueryGrant, DbQueryLog, DbTicket
from app.models.user import User
from app.services.application_time import format_app_datetime, now_dt
from app.services.db_ops import (
    DEFAULT_AUDIT_RULES,
    _build_connection,
    audit_sql,
    build_rollback_inserts,
    execute_statements,
    filter_selectable_databases,
    snapshot_dangerous_statements,
    split_sql_statements,
    test_instance_connection,
)

router = APIRouter()

DB_TYPES = {"mysql", "postgresql"}
DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432}


AUDIT_RULES_CONFIG_KEY = "db_audit_rules"
APPROVAL_FLOWS_CONFIG_KEY = "db_approval_flows"


def _parse_roles(raw: str | None) -> list[str]:
    """解析实例授权角色（JSON 数组文本；空/坏数据按全员可用处理）。"""
    import json as _json

    if not raw:
        return []
    try:
        parsed = _json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:  # noqa: BLE001
        return []


def _read_audit_rules(db: Session) -> dict:
    """读取审核规则配置（system_config），缺省回落 DEFAULT_AUDIT_RULES。"""
    import json as _json

    from app.models.system_config import SystemConfig

    row = db.query(SystemConfig).filter(SystemConfig.config_key == AUDIT_RULES_CONFIG_KEY).first()
    if not row or not row.config_value:
        return {}
    try:
        parsed = _json.loads(row.config_value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _user_can_use_instance(db: Session, instance: DbInstance, user: User) -> bool:
    """实例使用授权：allowed_roles 为空=全员；否则用户角色需在列表内。"""
    roles = _parse_roles(instance.allowed_roles)
    if not roles:
        return True
    return str(getattr(user, "role", "") or "") in roles


def _read_approval_flows(db: Session) -> dict[str, dict[str, list[str]]]:
    """读取审批流配置（system_config）。

    结构：{"change": {"steps": ["运维", "管理员"]}, "query": {"steps": ["运维"]}}。
    steps 为角色名数组（有序 = 审批顺序）；某类型缺配 / steps 空 = 单级模式
    （任何具备审批权的人可审），与历史行为兼容。
    """
    import json as _json

    from app.models.system_config import SystemConfig

    row = db.query(SystemConfig).filter(SystemConfig.config_key == APPROVAL_FLOWS_CONFIG_KEY).first()
    if not row or not row.config_value:
        return {}
    try:
        parsed = _json.loads(row.config_value)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}
    flows: dict[str, dict[str, list[str]]] = {}
    for ticket_type, meta in parsed.items():
        if ticket_type not in {"change", "query"} or not isinstance(meta, dict):
            continue
        steps_raw = meta.get("steps")
        steps = [str(s).strip() for s in steps_raw if str(s).strip()] if isinstance(steps_raw, list) else []
        flows[ticket_type] = {"steps": steps}
    return flows


def _resolve_approval_steps(db: Session, ticket_type: str) -> list[str]:
    """按工单类型解析审批链（提交时快照到工单，后续配置变更不影响在途工单）。"""
    flow = _read_approval_flows(db).get(ticket_type) or {}
    return list(flow.get("steps") or [])


def _user_has_manage(current_user: User) -> bool:
    """判断当前用户是否具备数据库管理权限（自助查询豁免用）。"""
    from app.core.security import resolve_effective_permissions

    try:
        return bool(resolve_effective_permissions(current_user).get("action.db.manage"))
    except Exception:  # noqa: BLE001
        return False


def _user_is_db_privileged(current_user: User) -> bool:
    """工单/查询日志的全量可见特权：审批、执行、管理任一权限即可。

    提交人只能看自己的单是隔离需求；而审批人/执行人必须能看到待办与
    历史工单，否则流程走不下去。
    """
    from app.core.security import resolve_effective_permissions

    try:
        perms = resolve_effective_permissions(current_user)
    except Exception:  # noqa: BLE001
        return False
    return any(
        bool(perms.get(key))
        for key in ("action.db.approve", "action.db.execute", "action.db.manage")
    )


def _own_identity_values(current_user: User) -> list[str]:
    """工单/日志落库的身份文本：可能记展示名（name）或账号名（username），两个都匹配。"""
    return [
        value
        for value in {str(current_user.name or "").strip(), str(current_user.username or "").strip()}
        if value
    ]


def _allowed_instance_ids(db: Session, current_user: User) -> set[int]:
    """当前用户可使用的实例集合：allowed_roles 为空 = 全员可用，否则角色需命中。"""
    return {
        instance.id
        for instance in db.query(DbInstance).all()
        if _user_can_use_instance(db, instance, current_user)
    }


def _is_db_admin(current_user: User) -> bool:
    """自审批豁免只认"管理员"角色本身。

    不用 action.db.manage 判断：运维模板也带 manage，用它当豁免条件
    等于把自审批禁令整个架空。管理员的单管理员自己清，天经地义。
    """
    return normalize_role_name(str(getattr(current_user, "role", "") or "")) == "管理员"


def _get_instance(db: Session, instance_id: int) -> DbInstance:
    instance = db.query(DbInstance).filter(DbInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="数据库实例不存在")
    return instance


def _serialize_instance(instance: DbInstance) -> dict[str, Any]:
    return {
        "id": instance.id,
        "name": instance.name,
        "db_type": instance.db_type,
        "host": instance.host,
        "port": instance.port,
        "database": instance.database or "",
        "username": instance.username,
        "has_password": bool(instance.password),
        "remark": instance.remark or "",
        "allowed_roles": _parse_roles(instance.allowed_roles),
        "status": instance.status or "unknown",
        "status_message": instance.status_message or "",
        "created_at": instance.created_at or "",
    }


def _serialize_ticket(t: DbTicket, instance_name: str = "") -> dict[str, Any]:
    steps = t.approval_steps if isinstance(t.approval_steps, list) else []
    return {
        "id": t.id,
        "instance_id": t.instance_id,
        "instance_name": instance_name,
        "title": t.title or "",
        "sql_text": t.sql_text or "",
        "database": t.database or "",
        "ticket_type": t.ticket_type,
        "status": t.status,
        "audit_result": t.audit_result or [],
        "audit_passed": bool(t.audit_passed),
        "exec_result": t.exec_result or [],
        "submitter": t.submitter or "",
        "reviewer": t.reviewer or "",
        "executor": t.executor or "",
        "review_note": t.review_note or "",
        # 多级审批链：空数组 = 单级模式；pending 时前端按 current_step 高亮当前级。
        "approval_steps": steps,
        "current_step": t.current_step or 1,
        "approval_log": t.approval_log if isinstance(t.approval_log, list) else [],
        "grant_detail": t.grant_detail if isinstance(t.grant_detail, dict) else {},
        "execute_after": t.execute_after or "",
        "has_backup": bool(t.backup_snapshot),
        "has_rollback": bool(t.rollback_statements),
        "created_at": t.created_at or "",
        "executed_at": t.executed_at or "",
    }


# ───────────────────────── 数据源管理 ─────────────────────────

class InstancePayload:
    """请求体（用 dict 接收以支持部分字段灵活校验）。"""

    @staticmethod
    def parse(body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        db_type = str(body.get("db_type") or "mysql").strip()
        host = str(body.get("host") or "").strip()
        username = str(body.get("username") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="实例名称不能为空")
        if db_type not in DB_TYPES:
            raise HTTPException(status_code=422, detail=f"仅支持 {sorted(DB_TYPES)}")
        if not host:
            raise HTTPException(status_code=422, detail="主机地址不能为空")
        if not username:
            raise HTTPException(status_code=422, detail="连接用户不能为空")
        database = str(body.get("database") or "").strip()
        if db_type == "postgresql" and not database:
            raise HTTPException(status_code=422, detail="PostgreSQL 必须指定库名")
        try:
            port = int(body.get("port") or DEFAULT_PORTS[db_type])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="端口必须是数字")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=422, detail="端口范围 1-65535")
        roles_raw = body.get("allowed_roles")
        allowed_roles = (
            [str(r).strip() for r in roles_raw if str(r).strip()]
            if isinstance(roles_raw, list) else []
        )
        return {
            "name": name, "db_type": db_type, "host": host, "port": port,
            "database": database, "username": username,
            "password": str(body.get("password") or ""),
            "remark": str(body.get("remark") or "").strip(),
            "allowed_roles": allowed_roles,
        }


@router.get("/instances")
def list_instances(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    instances = db.query(DbInstance).order_by(DbInstance.id.desc()).all()
    return [_serialize_instance(i) for i in instances]


@router.post("/instances")
def create_instance(body: dict, db: Session = Depends(get_db),
                    current_user: User = Depends(require_permissions("action.db.manage"))):
    data = InstancePayload.parse(body)
    record = DbInstance(
        name=data["name"], db_type=data["db_type"], host=data["host"], port=data["port"],
        database=data["database"], username=data["username"],
        password=encrypt_secret(data["password"]) if data["password"] else "",
        remark=data["remark"], created_at=format_app_datetime(now_dt()),
        allowed_roles=__import__("json").dumps(data["allowed_roles"], ensure_ascii=False) if data["allowed_roles"] else "",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    # 创建即测一次连通（失败不阻断）。
    _try_test(db, record, data["password"])
    return _serialize_instance(record)


@router.put("/instances/{instance_id}")
def update_instance(instance_id: int, body: dict, db: Session = Depends(get_db),
                    current_user: User = Depends(require_permissions("action.db.manage"))):
    """编辑实例：密码留空表示沿用原密码。"""
    record = _get_instance(db, instance_id)
    data = InstancePayload.parse(body)
    record.name = data["name"]
    record.db_type = data["db_type"]
    record.host = data["host"]
    record.port = data["port"]
    record.database = data["database"]
    record.username = data["username"]
    record.remark = data["remark"]
    record.allowed_roles = __import__("json").dumps(data["allowed_roles"], ensure_ascii=False) if data["allowed_roles"] else ""
    if data["password"]:
        record.password = encrypt_secret(data["password"])
    db.commit()
    db.refresh(record)
    return _serialize_instance(record)


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: int, db: Session = Depends(get_db),
                    current_user: User = Depends(require_permissions("action.db.manage"))):
    record = _get_instance(db, instance_id)
    used = db.query(DbTicket).filter(DbTicket.instance_id == instance_id).count()
    if used:
        raise HTTPException(status_code=422, detail=f"该实例还有 {used} 张工单记录，禁止删除（保留审计）")
    db.delete(record)
    db.commit()
    return {"message": "实例已删除"}


@router.post("/instances/{instance_id}/test")
def test_instance(instance_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(require_permissions("action.db.manage"))):
    record = _get_instance(db, instance_id)
    password = decrypt_secret(record.password or "")
    ok, message = test_instance_connection(
        record.db_type, record.host, record.port, record.username, password, record.database,
    )
    record.status = "ok" if ok else "error"
    record.status_message = message
    db.commit()
    return {"ok": ok, "message": message}


def _try_test(db: Session, record: DbInstance, plain_password: str) -> None:
    """创建/编辑后的 best-effort 连通探测，结果写回状态字段。"""
    try:
        ok, message = test_instance_connection(
            record.db_type, record.host, record.port, record.username, plain_password, record.database,
        )
    except Exception as exc:  # noqa: BLE001
        ok, message = False, str(exc)[:200]
    record.status = "ok" if ok else "error"
    record.status_message = message
    db.commit()


# ───────────────────────── SQL 工单 ─────────────────────────

class TicketPayload:
    @staticmethod
    def parse(body: dict) -> dict:
        ticket_type = str(body.get("ticket_type") or "change")
        if ticket_type not in {"change", "query"}:
            raise HTTPException(status_code=422, detail="ticket_type 仅支持 change/query")
        sql_text = str(body.get("sql_text") or "").strip()
        # 变更工单必须有 SQL；查询工单是授权申请，SQL 可空（可附验证语句作审批参考）。
        if not sql_text and ticket_type == "change":
            raise HTTPException(status_code=422, detail="SQL 内容不能为空")
        if len(sql_text) > 200_000:
            raise HTTPException(status_code=422, detail="SQL 超过 200KB 上限")
        database = str(body.get("database") or "").strip()
        # 查询授权申请：目标库必填，时限 1~720 小时。
        grant_detail: dict = {}
        if ticket_type == "query":
            if not database:
                raise HTTPException(status_code=422, detail="查询授权申请必须指定目标库")
            tables_raw = body.get("grant_tables")
            tables = [str(t).strip() for t in tables_raw if str(t).strip()] if isinstance(tables_raw, list) else []
            try:
                hours = int(body.get("grant_hours") or 4)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="授权时长必须是数字（小时）")
            if not (1 <= hours <= 720):
                raise HTTPException(status_code=422, detail="授权时长范围 1-720 小时")
            grant_detail = {
                "tables": tables,
                "hours": hours,
                "reason": str(body.get("grant_reason") or "").strip()[:500],
            }
        execute_after = str(body.get("execute_after") or "").strip()
        if execute_after:
            # 简单格式校验：YYYY-MM-DD HH:MM:SS，且必须是将来的时间。
            import datetime as _dt

            try:
                target = _dt.datetime.strptime(execute_after, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise HTTPException(status_code=422, detail="定时执行时间格式应为 YYYY-MM-DD HH:MM:SS")
            if target <= _dt.datetime.now():
                raise HTTPException(status_code=422, detail="定时执行时间必须晚于当前时间")
        return {
            "sql_text": sql_text,
            "ticket_type": ticket_type,
            "title": str(body.get("title") or "").strip()[:200],
            "execute_after": execute_after,
            "database": database,
            "grant_detail": grant_detail,
        }


@router.get("/tickets")
def list_tickets(
    # status/limit 用普通默认值而非 Query()：本仓库测试直接调用路由函数，
    # Query 默认值会漏进来（与 notifications.list_notifications 同约定）。
    status: str = "",
    instance_id: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(DbTicket)
    if status:
        query = query.filter(DbTicket.status == status)
    if instance_id:
        query = query.filter(DbTicket.instance_id == instance_id)
    # 用户隔离：特权角色（审批/执行/管理）看全量；普通用户只能看到
    # 自己提交的工单，或目标实例对自己开放（allowed_roles 命中）的工单。
    if not _user_is_db_privileged(current_user):
        query = query.filter(
            or_(
                DbTicket.submitter.in_(_own_identity_values(current_user)),
                DbTicket.instance_id.in_(_allowed_instance_ids(db, current_user)),
            )
        )
    tickets = query.order_by(DbTicket.id.desc()).limit(limit).all()
    names = {i.id: i.name for i in db.query(DbInstance).all()}
    return [_serialize_ticket(t, names.get(t.instance_id, "")) for t in tickets]


@router.post("/tickets")
def create_ticket(body: dict, db: Session = Depends(get_db),
                  current_user: User = Depends(require_permissions("action.db.submit"))):
    """提交工单：先过规则引擎，block 级问题直接 422 拒绝（不进人工审批）。

    变更工单要求 action.db.submit；查询工单是授权申请，同样走 submit 权限，
    SQL 可空（申请理由落在 grant_detail / sql_text）。提交时按审批流配置快照
    审批链，之后配置变更不影响在途工单。
    """
    instance_id = body.get("instance_id")
    instance = _get_instance(db, int(instance_id)) if instance_id else None
    if not instance:
        raise HTTPException(status_code=422, detail="请选择目标实例")
    # 实例级使用授权：未授权角色不能对该库提工单。
    if not _user_can_use_instance(db, instance, current_user):
        raise HTTPException(status_code=403, detail="你的角色未被授权使用该数据源")
    data = TicketPayload.parse(body)

    # 查询授权申请没有 SQL 可审，跳过规则引擎；变更工单照常全量审核。
    audit_findings: list[dict] = []
    if data["sql_text"]:
        audit = audit_sql(data["sql_text"], data["ticket_type"], rule_config=_read_audit_rules(db))
        if not audit["passed"]:
            blocks = [f["message"] for f in audit["findings"] if f["level"] == "block"]
            raise HTTPException(status_code=422, detail={"message": "SQL 审核未通过，已拒绝提交", "findings": blocks})
        audit_findings = audit["findings"]

    approval_steps = _resolve_approval_steps(db, data["ticket_type"])
    default_title = (
        f"查询授权-{instance.name}.{data['database']}"
        if data["ticket_type"] == "query"
        else f"{data['ticket_type']}工单-{now_dt().strftime('%m%d %H%M')}"
    )
    ticket = DbTicket(
        instance_id=int(instance_id),
        title=data["title"] or default_title,
        sql_text=data["sql_text"],
        ticket_type=data["ticket_type"],
        status="pending",
        audit_result=audit_findings,
        audit_passed=1,
        submitter=current_user.name or current_user.username,
        execute_after=data["execute_after"],
        database=data["database"],
        grant_detail=data["grant_detail"],
        approval_steps=approval_steps,
        current_step=1,
        approval_log=[],
        created_at=format_app_datetime(now_dt()),
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    # 通知当前级审批人（best-effort）
    _notify_step_approvers(db, ticket)
    return _serialize_ticket(ticket)


def _append_approval_log(ticket: DbTicket, *, role: str, reviewer: str, decision: str, note: str) -> None:
    """追加一条逐级审批记录。JSON 列必须整体重新赋值，原地 append 不会落库。"""
    log = list(ticket.approval_log or [])
    log.append({
        "step": ticket.current_step or 1,
        "role": role,
        "reviewer": reviewer,
        "decision": decision,
        "note": note,
        "at": format_app_datetime(now_dt()),
    })
    ticket.approval_log = log


@router.post("/tickets/{ticket_id}/review")
def review_ticket(ticket_id: int, body: dict, db: Session = Depends(get_db),
                  current_user: User = Depends(require_permissions("action.db.approve"))):
    """审批：approve 推进审批链（最后一级 → 待执行/授权生效）；reject 终止。

    规则：
    - 禁止自审批（提交人不能审自己的单；仅"管理员"角色豁免）；
    - 多级审批时只有当前级配置的角色能审（配了链但没有该角色的用户则 403）；
    - 查询工单在最后一级批准后自动生成时限授权，无需执行。
    """
    ticket = db.query(DbTicket).filter(DbTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status != "pending":
        raise HTTPException(status_code=422, detail=f"工单当前状态 {ticket.status}，不可审批")
    decision = str(body.get("decision") or "")
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=422, detail="decision 仅支持 approve/reject")

    # 自审批拦截：提交人不能审自己的单（管理员角色豁免，兜住"管理员顺手清工单"场景）。
    reviewer_name = current_user.name or current_user.username
    if ticket.submitter in {reviewer_name, current_user.username} and not _is_db_admin(current_user):
        raise HTTPException(status_code=403, detail="不能审批自己提交的工单")

    steps = ticket.approval_steps if isinstance(ticket.approval_steps, list) else []
    step_index = max(1, ticket.current_step or 1)  # 1-based
    current_role = ""
    if steps:
        current_role = str(steps[step_index - 1] or "")
        if normalize_role_name(current_user.role) != normalize_role_name(current_role):
            raise HTTPException(
                status_code=403,
                detail=f"多级审批中：第 {step_index}/{len(steps)} 级应由角色「{current_role}」审批",
            )

    note = str(body.get("note") or "")[:500]
    ticket.review_note = note
    if decision == "reject":
        _append_approval_log(ticket, role=current_role, reviewer=reviewer_name, decision="reject", note=note)
        ticket.reviewer = reviewer_name
        ticket.status = "rejected"
        db.commit()
        db.refresh(ticket)
        _notify_ticket_reviewed(db, ticket, approved=False)
        return _serialize_ticket(ticket)

    # 批准：推进审批链。
    _append_approval_log(ticket, role=current_role, reviewer=reviewer_name, decision="approve", note=note)
    if steps and step_index < len(steps):
        ticket.current_step = step_index + 1
        ticket.status = "pending"
        db.commit()
        db.refresh(ticket)
        _notify_step_approvers(db, ticket)
        return _serialize_ticket(ticket)

    # 最后一级批准：变更工单进入待执行；查询工单直接生成时限授权并终态。
    ticket.reviewer = reviewer_name
    if ticket.ticket_type == "query":
        grant = _grant_from_ticket(db, ticket, granted_by=reviewer_name)
        ticket.status = "success"
        ticket.executed_at = format_app_datetime(now_dt())
        db.commit()
        db.refresh(ticket)
        _notify_query_grant_activated(db, ticket, grant)
        return _serialize_ticket(ticket)
    ticket.status = "approved"
    db.commit()
    db.refresh(ticket)
    _notify_ticket_reviewed(db, ticket, approved=True)
    return _serialize_ticket(ticket)


def _grant_from_ticket(db: Session, ticket: DbTicket, *, granted_by: str) -> DbQueryGrant:
    """查询工单终审通过 → 生成时限查询授权（到期时间 = 现在 + 申请时长）。"""
    import datetime as _dt
    import json as _json

    detail = ticket.grant_detail if isinstance(ticket.grant_detail, dict) else {}
    hours = int(detail.get("hours") or 4)
    expires_at = format_app_datetime(now_dt() + _dt.timedelta(hours=hours))
    submitter_user = db.query(User).filter(
        (User.name == ticket.submitter) | (User.username == ticket.submitter)
    ).first()
    grant = DbQueryGrant(
        ticket_id=ticket.id,
        instance_id=ticket.instance_id,
        username=submitter_user.username if submitter_user else ticket.submitter,
        display_name=ticket.submitter,
        database=ticket.database or "",
        tables=_json.dumps(detail.get("tables") or [], ensure_ascii=False) if detail.get("tables") else "",
        reason=str(detail.get("reason") or "")[:500],
        expires_at=expires_at,
        granted_by=granted_by,
        status="active",
        created_at=format_app_datetime(now_dt()),
    )
    db.add(grant)
    db.flush()
    return grant


def _claim_ticket_for_execution(db: Session, ticket_id: int) -> DbTicket:
    """原子认领待执行工单：仅当 status=approved 时才翻转为 executing。

    单条 UPDATE 带状态条件，两个并发入口（接口 / 定时 runner）同时认领
    同一张工单时只可能有一个 rowcount==1，从数据库层面杜绝重复执行。
    认领同时写入心跳时间（heartbeat_at），作为卡死恢复的判据。
    认领失败（不存在 / 非 approved / 已被并发认领）一律 422。
    """
    claimed = (
        db.query(DbTicket)
        .filter(DbTicket.id == ticket_id, DbTicket.status == "approved")
        .update(
            {"status": "executing", "heartbeat_at": format_app_datetime(now_dt())},
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed == 0:
        raise HTTPException(status_code=422, detail="工单不存在或已被执行/状态已变化")
    ticket = db.query(DbTicket).filter(DbTicket.id == ticket_id).first()
    if not ticket:  # 理论不可达（刚认领成功），防御数据被旁路清理
        raise HTTPException(status_code=422, detail="工单不存在或已被执行/状态已变化")
    return ticket


@router.post("/tickets/{ticket_id}/execute")
def execute_ticket(ticket_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(require_permissions("action.db.execute"))):
    """执行已批准工单：逐条执行并记录结果；执行人需具备执行权限（与提交/审批分权）。

    查询工单不走这里——终审通过时授权已自动生效（review_ticket 内生成）。
    状态转换走 _claim_ticket_for_execution 原子认领，并发执行只有一个能成功。
    """
    ticket = db.query(DbTicket).filter(DbTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.ticket_type == "query":
        raise HTTPException(status_code=422, detail="查询授权工单在终审通过后授权自动生效，无需执行")

    # 原子认领：approved → executing，抢不到直接 422（替代旧的"先读后判"）。
    ticket = _claim_ticket_for_execution(db, ticket_id)

    instance = _get_instance(db, ticket.instance_id)
    statements = split_sql_statements(ticket.sql_text)

    # 变更安全网：执行前对 UPDATE/DELETE 做受影响行快照 + 回滚语句草稿。
    # 备份失败不阻塞执行（风险由审批人把控），但该单会缺失回滚能力。
    conn_args = (
        instance.db_type, instance.host, instance.port, instance.username,
        decrypt_secret(instance.password or ""), ticket.database or instance.database,
    )
    try:
        snapshot = snapshot_dangerous_statements(*conn_args, statements)
        if snapshot["items"]:
            import json as _json

            rollback_parts = [
                build_rollback_inserts(item["table"], item["columns"], item["rows"], db_type=instance.db_type, source_stmt=item.get("sql") or "")
                for item in snapshot["items"]
            ]
            ticket.backup_snapshot = _json.dumps(snapshot, ensure_ascii=False, default=str)
            ticket.rollback_statements = "\n".join(part for part in rollback_parts if part)
    except Exception:  # noqa: BLE001
        pass

    result = execute_statements(*conn_args, statements, fetch_rows=False)
    ticket.status = "success" if result["ok"] else "failed"
    ticket.exec_result = result["results"]
    ticket.executor = current_user.name or current_user.username
    ticket.executed_at = format_app_datetime(now_dt())
    db.commit()
    _notify_ticket_result(db, ticket)
    db.refresh(ticket)
    return _serialize_ticket(ticket)


def _notify_ticket_result(db: Session, ticket: DbTicket) -> None:
    """执行结果站内通知提交人（best-effort，失败不影响工单状态）。

    提交人字段存的是展示名或账号名，与 _notify_ticket_reviewed 同款双匹配：
    username 或 name 任一命中即可，避免用展示名提交的人收不到结果通知。
    """
    try:
        from app.api.notifications import NotificationCreate, create_notification
        from app.models.user import User as _User

        submitter = db.query(_User).filter(
            (_User.username == ticket.submitter) | (_User.name == ticket.submitter)
        ).first()
        if not submitter:
            return
        ok = ticket.status == "success"
        create_notification(db, data=NotificationCreate(
            user_id=submitter.id,
            type="success" if ok else "error",
            category="db",
            title=f"SQL 工单执行{'成功' if ok else '失败'}：{ticket.title}",
            content=(
                f"工单 #{ticket.id} 于 {ticket.executed_at} 执行"
                + ("成功" if ok else "失败，详见工单详情")
                + ("；已自动备份受影响数据并生成回滚语句" if ticket.backup_snapshot else "")
            ),
            link="/assets/databases",
            resource_id=str(ticket.id),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _notify_step_approvers(db: Session, ticket: DbTicket) -> None:
    """通知"当前应审批的人"：待审批工单提醒（best-effort）。

    收件人判定（对齐多级审批 + 提交/审批分权）：
    - 工单带审批链（approval_steps 非空）→ 只通知"当前级角色 ∩ 具备审批权"的用户，
      逐人解析有效权限（含用户级覆盖），保证自定义角色也能收到；
    - 无审批链（单级模式）→ 通知所有具备 action.db.approve 的用户；
    - 提交人本人即使有审批权也排除（后端禁止自审批，通知只会造成困惑）。
    """
    try:
        from app.api.notifications import NotificationCreate, create_notification
        from app.core.security import resolve_effective_permissions

        steps = ticket.approval_steps if isinstance(ticket.approval_steps, list) else []
        step_role = normalize_role_name(str(steps[max(1, ticket.current_step or 1) - 1] or "")) if steps else ""
        step_hint = (
            f"第 {ticket.current_step}/{len(steps)} 级（{step_role}）"
            if steps else ""
        )
        candidates = db.query(User).filter(User.status == "active").all()
        for approver in candidates:
            if approver.username == ticket.submitter or approver.name == ticket.submitter:
                continue  # 提交人不能自审，不发给他
            try:
                perms = resolve_effective_permissions(approver)
            except Exception:  # noqa: BLE001  单人权限解析失败不波及其余审批人
                continue
            if not perms.get("action.db.approve"):
                continue
            if step_role and normalize_role_name(str(approver.role or "")) != step_role:
                continue  # 多级审批：只通知当前级角色
            is_query = ticket.ticket_type == "query"
            create_notification(db, data=NotificationCreate(
                user_id=approver.id,
                type="warning",
                category="db",
                title=f"待审批 {'查询授权' if is_query else 'SQL'} 工单：{ticket.title}",
                content=(
                    f"提交人 {ticket.submitter} 提交了 {ticket.ticket_type} 工单 #{ticket.id}"
                    + (f"（{step_hint}）" if step_hint else "")
                    + "，请在数据库管理中审批"
                ),
                link="/assets/databases",
                resource_id=str(ticket.id),
            ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _notify_ticket_reviewed(db: Session, ticket: DbTicket, *, approved: bool) -> None:
    """审批结论（终审通过/驳回）通知提交人（best-effort）。"""
    try:
        from app.api.notifications import NotificationCreate, create_notification
        from app.models.user import User as _User

        submitter = db.query(_User).filter(
            (_User.username == ticket.submitter) | (_User.name == ticket.submitter)
        ).first()
        if not submitter:
            return
        create_notification(db, data=NotificationCreate(
            user_id=submitter.id,
            type="success" if approved else "error",
            category="db",
            title=f"SQL 工单审批{'通过' if approved else '被驳回'}：{ticket.title}",
            content=(
                f"工单 #{ticket.id} 已被 {ticket.reviewer} "
                + ("审批通过，等待执行" if approved else "驳回")
                + (f"。审批意见：{ticket.review_note}" if ticket.review_note else "")
            ),
            link="/assets/databases",
            resource_id=str(ticket.id),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _notify_query_grant_activated(db: Session, ticket: DbTicket, grant: DbQueryGrant) -> None:
    """查询授权生效通知提交人（best-effort）。"""
    try:
        from app.api.notifications import NotificationCreate, create_notification
        from app.models.user import User as _User

        user = db.query(_User).filter(
            (_User.username == ticket.submitter) | (_User.name == ticket.submitter)
        ).first()
        if not user:
            return
        create_notification(db, data=NotificationCreate(
            user_id=user.id,
            type="success",
            category="db",
            title=f"查询授权已生效：{ticket.title}",
            content=(
                f"工单 #{ticket.id} 终审通过，你在「{ticket.database}」库的查询授权已生效，"
                f"{grant.expires_at} 前可自助查询，到期自动回收"
            ),
            link="/assets/databases",
            resource_id=str(ticket.id),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


# ───────────────────────── 自助查询 ─────────────────────────

def _active_grant(db: Session, username: str, instance_id: int, database: str) -> DbQueryGrant | None:
    """查本人名下对该库生效中的查询授权（未过期且未吊销）。"""
    now_text = format_app_datetime(now_dt())
    return (
        db.query(DbQueryGrant)
        .filter(
            DbQueryGrant.username == username,
            DbQueryGrant.instance_id == instance_id,
            DbQueryGrant.database == database,
            DbQueryGrant.status == "active",
            DbQueryGrant.expires_at > now_text,
        )
        .order_by(DbQueryGrant.expires_at.desc())
        .first()
    )


@router.post("/query")
def run_query(body: dict, db: Session = Depends(get_db),
              current_user: User = Depends(require_permissions("action.db.submit"))):
    """自助查询：只读语句直接执行（不建工单），结果上限 500 行，落查询日志。

    对齐 Yearning 的"先审批后查询"：普通用户需本人名下生效中的查询授权
    （目标库精确匹配）；具备 action.db.manage 的管理员豁免。写语句在规则
    引擎 R8 层直接拒绝，与授权无关。
    """
    instance_id = body.get("instance_id")
    sql_text = str(body.get("sql_text") or "").strip()
    if not instance_id:
        raise HTTPException(status_code=422, detail="请选择实例")
    if not sql_text:
        raise HTTPException(status_code=422, detail="SQL 不能为空")
    instance = _get_instance(db, int(instance_id))
    if not _user_can_use_instance(db, instance, current_user):
        raise HTTPException(status_code=403, detail="你的角色未被授权使用该数据源")

    target_database = str(body.get("database") or "").strip() or instance.database
    # 查询授权校验：管理员豁免；其他用户必须有目标库的有效授权。
    if not _user_has_manage(current_user):
        grant = _active_grant(db, current_user.username, instance.id, target_database)
        if not grant:
            raise HTTPException(
                status_code=403,
                detail=f"你在「{target_database}」库没有生效中的查询授权，请先提交查询授权工单",
            )

    # 复用审核引擎的只读规则（R8）：任何写语句直接拒绝。
    audit = audit_sql(sql_text, ticket_type="query", rule_config=_read_audit_rules(db))
    if not audit["passed"]:
        blocks = [f["message"] for f in audit["findings"] if f["level"] == "block"]
        raise HTTPException(status_code=422, detail={"message": "自助查询仅允许只读语句", "findings": blocks})

    # readonly=True：执行用户 SQL 前把连接切为会话级只读，即使审核被绕过，
    # 数据库本身也会拒绝写语句（纵深防御；返回值多带 readonly_notice 字段）。
    result = execute_statements(
        instance.db_type, instance.host, instance.port, instance.username,
        decrypt_secret(instance.password or ""), target_database,
        split_sql_statements(sql_text), fetch_rows=True, readonly=True,
    )
    log = DbQueryLog(
        instance_id=instance.id,
        username=current_user.name or current_user.username,
        sql_text=sql_text[:10000],
        database=target_database,
        row_count=len(result.get("rows") or []),
        duration_ms=result.get("total_duration_ms") or 0,
        ok=1 if result["ok"] else 0,
        error=str((result["results"] or [{}])[-1].get("error") or "")[:500],
        created_at=format_app_datetime(now_dt()),
    )
    db.add(log)
    db.commit()
    return {
        "ok": result["ok"],
        "columns": result["columns"],
        "rows": result["rows"],
        "results": result["results"],
        "duration_ms": result["total_duration_ms"],
        "database": target_database,
        "instance_name": instance.name,
        "hint": next((r.get("hint") for r in (result["results"] or []) if not r.get("ok")), ""),
    }


@router.get("/query-logs")
def list_query_logs(limit: int = 50,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """查询日志列表：特权角色看全量，普通用户只看自己名下 / 授权实例上的记录。"""
    query = db.query(DbQueryLog)
    if not _user_is_db_privileged(current_user):
        query = query.filter(
            or_(
                DbQueryLog.username.in_(_own_identity_values(current_user)),
                DbQueryLog.instance_id.in_(_allowed_instance_ids(db, current_user)),
            )
        )
    logs = query.order_by(DbQueryLog.id.desc()).limit(limit).all()
    names = {i.id: i.name for i in db.query(DbInstance).all()}
    return [
        {
            "id": log.id,
            "instance_name": names.get(log.instance_id, str(log.instance_id)),
            "username": log.username or "",
            "sql_text": log.sql_text or "",
            "database": log.database or "",
            "row_count": log.row_count or 0,
            "duration_ms": log.duration_ms or 0,
            "ok": bool(log.ok),
            "error": log.error or "",
            "created_at": log.created_at or "",
        }
        for log in logs
    ]


# ───────────────────────── 审核规则配置 / 回滚导出 ─────────────────────────

@router.get("/audit-rules")
def get_audit_rules(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """审核规则配置读取：默认规则 + 当前覆盖（前端渲染配置表）。"""
    saved = _read_audit_rules(db)
    rules = []
    for rule_id, meta in DEFAULT_AUDIT_RULES.items():
        override = saved.get(rule_id) or {}
        rules.append({
            "rule": rule_id,
            "name": meta["name"],
            "default_level": meta["level"],
            "enabled": bool(override.get("enabled", True)),
            "level": str(override.get("level") or meta["level"]),
        })
    return {"rules": rules}


@router.put("/audit-rules")
def update_audit_rules(body: dict, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permissions("action.db.manage"))):
    """保存审核规则配置（开关 + 级别）。R0（空 SQL）不可配置。"""
    import json as _json

    from app.models.system_config import SystemConfig

    raw_rules = body.get("rules")
    if not isinstance(raw_rules, list):
        raise HTTPException(status_code=422, detail="rules 必须是数组")
    config: dict = {}
    for item in raw_rules:
        rule_id = str(item.get("rule") or "")
        if rule_id not in DEFAULT_AUDIT_RULES:
            raise HTTPException(status_code=422, detail=f"未知规则: {rule_id}")
        level = str(item.get("level") or DEFAULT_AUDIT_RULES[rule_id]["level"])
        if level not in {"block", "warn"}:
            raise HTTPException(status_code=422, detail=f"{rule_id} 级别仅支持 block/warn")
        config[rule_id] = {"enabled": bool(item.get("enabled", True)), "level": level}

    row = db.query(SystemConfig).filter(SystemConfig.config_key == AUDIT_RULES_CONFIG_KEY).first()
    if not row:
        row = SystemConfig(config_key=AUDIT_RULES_CONFIG_KEY)
        db.add(row)
    row.config_value = _json.dumps(config, ensure_ascii=False)
    row.updated_at = format_app_datetime(now_dt())
    db.commit()
    return get_audit_rules(db=db, current_user=current_user)


@router.get("/tickets/{ticket_id}/rollback")
def export_ticket_rollback(
    ticket_id: int,
    download: int = 1,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.db.execute")),
):
    """导出工单回滚语句（执行前自动生成的反向 INSERT；文件下载）。"""
    from fastapi import Response

    ticket = db.query(DbTicket).filter(DbTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if not ticket.rollback_statements:
        raise HTTPException(status_code=404, detail="该工单没有回滚语句（无 UPDATE/DELETE 或备份未生成）")
    return Response(
        content=ticket.rollback_statements,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="rollback-ticket-{ticket_id}.sql"'},
    )


# ───────────────────────── 审批流配置 ─────────────────────────

@router.get("/approval-flows")
def get_approval_flows(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """审批流配置读取：按工单类型的审批链 + 可选角色列表（渲染配置表单）。

    steps 空 = 单级模式（任何具备审批权的人可审），与历史行为兼容。
    """
    from app.core.rbac import get_role_template_map

    flows = _read_approval_flows(db)
    return {
        "change": {"steps": (flows.get("change") or {}).get("steps") or []},
        "query": {"steps": (flows.get("query") or {}).get("steps") or []},
        # 角色下拉数据源：系统预设 + 自定义角色模板名。
        "roles": sorted(get_role_template_map().keys()),
    }


@router.put("/approval-flows")
def update_approval_flows(body: dict, db: Session = Depends(get_db),
                          current_user: User = Depends(require_permissions("action.db.manage"))):
    """保存审批流配置。仅校验结构，不校验角色存在性（自定义角色随时可增删）。"""
    import json as _json

    from app.models.system_config import SystemConfig

    config: dict = {}
    for ticket_type in ("change", "query"):
        meta = body.get(ticket_type)
        if meta is None:
            continue
        if not isinstance(meta, dict):
            raise HTTPException(status_code=422, detail=f"{ticket_type} 配置必须是对象")
        steps_raw = meta.get("steps")
        if steps_raw is not None and not isinstance(steps_raw, list):
            raise HTTPException(status_code=422, detail=f"{ticket_type}.steps 必须是数组")
        steps = [str(s).strip() for s in (steps_raw or []) if str(s).strip()]
        if len(set(steps)) != len(steps):
            raise HTTPException(status_code=422, detail=f"{ticket_type} 审批链中角色不能重复")
        config[ticket_type] = {"steps": steps}

    row = db.query(SystemConfig).filter(SystemConfig.config_key == APPROVAL_FLOWS_CONFIG_KEY).first()
    if not row:
        row = SystemConfig(config_key=APPROVAL_FLOWS_CONFIG_KEY)
        db.add(row)
    row.config_value = _json.dumps(config, ensure_ascii=False)
    row.updated_at = format_app_datetime(now_dt())
    db.commit()
    return get_approval_flows(db=db, current_user=current_user)


# ───────────────────────── 查询授权 ─────────────────────────

def _serialize_grant(g: DbQueryGrant, instance_name: str = "") -> dict[str, Any]:
    import json as _json

    try:
        tables = _json.loads(g.tables) if g.tables else []
    except Exception:  # noqa: BLE001
        tables = []
    return {
        "id": g.id,
        "ticket_id": g.ticket_id or 0,
        "instance_id": g.instance_id,
        "instance_name": instance_name,
        "username": g.username or "",
        "display_name": g.display_name or "",
        "database": g.database or "",
        "tables": tables,
        "reason": g.reason or "",
        "expires_at": g.expires_at or "",
        "granted_by": g.granted_by or "",
        "status": g.status or "active",
        "created_at": g.created_at or "",
    }


def _grant_status_now(grant: DbQueryGrant) -> str:
    """展示用状态：active 但已过期的按 expired 显示（scheduler 翻转前的兜底）。"""
    if grant.status == "active" and grant.expires_at and grant.expires_at <= format_app_datetime(now_dt()):
        return "expired"
    return grant.status or "active"


@router.get("/query-grants")
def list_query_grants(
    scope: str = Query(default="mine", description="mine=我的授权 / all=全部（需管理权限）"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查询授权列表：普通用户看自己的（自助查询页展示有效期），管理权限看全部。"""
    query = db.query(DbQueryGrant)
    if scope == "all" and _user_has_manage(current_user):
        pass
    else:
        query = query.filter(DbQueryGrant.username == current_user.username)
    grants = query.order_by(DbQueryGrant.id.desc()).limit(200).all()
    names = {i.id: i.name for i in db.query(DbInstance).all()}
    result = [_serialize_grant(g, names.get(g.instance_id, "")) for g in grants]
    for item, g in zip(result, grants):
        item["status"] = _grant_status_now(g)
    return result


@router.post("/query-grants")
def create_query_grant(body: dict, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permissions("action.db.manage"))):
    """管理员手动授权（绕过工单，适合临时救急；全程留痕 granted_by）。"""
    import datetime as _dt
    import json as _json

    username = str(body.get("username") or "").strip()
    instance_id = body.get("instance_id")
    database = str(body.get("database") or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="被授权人账号不能为空")
    if not instance_id:
        raise HTTPException(status_code=422, detail="请选择实例")
    instance = _get_instance(db, int(instance_id))
    if not database:
        raise HTTPException(status_code=422, detail="授权目标库不能为空")
    try:
        hours = int(body.get("hours") or 4)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="授权时长必须是数字（小时）")
    if not (1 <= hours <= 720):
        raise HTTPException(status_code=422, detail="授权时长范围 1-720 小时")
    target_user = db.query(User).filter(User.username == username).first()
    if not target_user:
        raise HTTPException(status_code=422, detail=f"用户 {username} 不存在")
    grant = DbQueryGrant(
        ticket_id=0,
        instance_id=instance.id,
        username=username,
        display_name=target_user.name or username,
        database=database,
        tables=_json.dumps([str(t).strip() for t in (body.get("tables") or []) if str(t).strip()], ensure_ascii=False),
        reason=str(body.get("reason") or "").strip()[:500],
        expires_at=format_app_datetime(now_dt() + _dt.timedelta(hours=hours)),
        granted_by=current_user.name or current_user.username,
        status="active",
        created_at=format_app_datetime(now_dt()),
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return _serialize_grant(grant, instance.name)


@router.post("/query-grants/{grant_id}/revoke")
def revoke_query_grant(grant_id: int, db: Session = Depends(get_db),
                       current_user: User = Depends(require_permissions("action.db.manage"))):
    """吊销查询授权（立即失效），并通知被授权人。"""
    grant = db.query(DbQueryGrant).filter(DbQueryGrant.id == grant_id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="授权记录不存在")
    if grant.status != "revoked":
        grant.status = "revoked"
        db.commit()
        _notify_grant_revoked(db, grant)
    db.refresh(grant)
    names = {i.id: i.name for i in db.query(DbInstance).all()}
    return _serialize_grant(grant, names.get(grant.instance_id, ""))


def _notify_grant_revoked(db: Session, grant: DbQueryGrant) -> None:
    """授权被吊销时通知被授权人（best-effort）。"""
    try:
        from app.api.notifications import NotificationCreate, create_notification
        from app.models.user import User as _User

        user = db.query(_User).filter(_User.username == grant.username).first()
        if not user:
            return
        create_notification(db, data=NotificationCreate(
            user_id=user.id,
            type="warning",
            category="db",
            title=f"查询授权已被吊销：{grant.database}",
            content=(
                f"你在「{grant.database}」库的查询授权已被 {grant.granted_by} 吊销，"
                "如需继续查询请重新提交查询授权工单"
            ),
            link="/assets/databases",
            resource_id=str(grant.id),
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


# ───────────────────────── 定时工单 Runner ─────────────────────────

async def start_db_ticket_scheduler() -> None:
    """后台扫描已批准且到期（execute_after <= now）的定时工单并执行。

    每 30 秒一轮；执行复用 execute_ticket 的核心路径（备份/通知一致），
    执行人记为 scheduler。工单执行失败保持 failed 状态等人工介入。
    """
    import asyncio

    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "db_ticket_scheduler",
        "SQL 定时工单调度",
        "每 30 秒扫描已批准且到期的定时工单并执行（自动备份 + 结果站内通知）；顺带回收到期的查询授权",
        30,
    )
    await asyncio.sleep(45)
    while True:
        with track_run("db_ticket_scheduler"):
            try:
                await asyncio.to_thread(_run_due_tickets)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(30)


def recover_stale_executing_tickets(db: Session, stale_hours: int = 2) -> int:
    """回收卡死在 executing 的工单（执行器中断恢复），返回处理条数。

    后台执行跑在 Web 进程内，进程重启/崩溃会让认领后的工单永远停在
    executing——既不能再执行（状态机不认），也没有结果。判定依据是
    _claim_ticket_for_execution 写入的 heartbeat_at：executing 且心跳早于
    stale_hours 小时（正常执行整单上限约 1 分钟，2 小时足以排除误判），
    或心跳为空（心跳机制上线前的历史认领行，其执行进程必然已随重启消亡），
    一律标记 failed 并注明"结果未知"，由人工核对目标库。幂等、廉价：
    lifespan 启动时与定时 runner 每轮开头各调一次。
    """
    threshold = format_app_datetime(now_dt() - timedelta(hours=stale_hours))
    stale = (
        db.query(DbTicket)
        .filter(
            DbTicket.status == "executing",
            or_(
                DbTicket.heartbeat_at == "",
                DbTicket.heartbeat_at.is_(None),
                DbTicket.heartbeat_at <= threshold,
            ),
        )
        .all()
    )
    if not stale:
        return 0
    for ticket in stale:
        ticket.status = "failed"
        ticket.exec_result = [{
            "index": 0,
            "sql": (ticket.sql_text or "")[:200],
            "ok": False,
            "error": "执行器中断，结果未知，请人工核对目标库",
        }]
        ticket.executed_at = format_app_datetime(now_dt())
    db.commit()
    return len(stale)


def _run_due_tickets() -> None:
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        # 先回收上一进程（或上一轮）遗留的 executing 卡死单：幂等且廉价，
        # 避免周期内进程崩溃的工单要等下次重启才被收敛。
        recover_stale_executing_tickets(db)
        now_text = format_app_datetime(now_dt())
        # 查询授权到期回收：active 且已过期的批量翻转为 expired（幂等）。
        # 展示与查询校验本来就比对 expires_at，翻转只为列表状态准确。
        db.query(DbQueryGrant).filter(
            DbQueryGrant.status == "active",
            DbQueryGrant.expires_at != "",
            DbQueryGrant.expires_at <= now_text,
        ).update({DbQueryGrant.status: "expired"}, synchronize_session=False)
        db.commit()
        tickets = db.query(DbTicket).filter(
            DbTicket.status == "approved",
            DbTicket.execute_after != "",
        ).all()
        for ticket in tickets:
            # 字符串时间比较（同格式 YYYY-MM-DD HH:MM:SS 可比）。
            if ticket.execute_after <= now_text:
                # 与 execute_ticket 走同一个原子认领：定时扫描与人工执行并发时，
                # 只有一方抢得到 approved → executing，杜绝重复执行。
                try:
                    ticket = _claim_ticket_for_execution(db, ticket.id)
                except HTTPException:
                    continue  # 已被执行 / 状态已变化，跳过交给下一个周期
                instance = db.query(DbInstance).filter(DbInstance.id == ticket.instance_id).first()
                if not instance:
                    ticket.status = "failed"
                    db.commit()
                    continue
                statements = split_sql_statements(ticket.sql_text)
                # 目标库与 execute_ticket 保持一致：工单指定库优先，空才落实例默认库。
                # 此前写死 instance.database，定时单会绕过审批人选择的目标库，
                # 在默认库上执行——P0 级错库事故。
                conn_args = (
                    instance.db_type, instance.host, instance.port, instance.username,
                    decrypt_secret(instance.password or ""), ticket.database or instance.database,
                )
                try:
                    snapshot = snapshot_dangerous_statements(*conn_args, statements)
                    if snapshot["items"]:
                        import json as _json

                        rollback_parts = [
                            build_rollback_inserts(item["table"], item["columns"], item["rows"], db_type=instance.db_type, source_stmt=item.get("sql") or "")
                            for item in snapshot["items"]
                        ]
                        ticket.backup_snapshot = _json.dumps(snapshot, ensure_ascii=False, default=str)
                        ticket.rollback_statements = "\n".join(p for p in rollback_parts if p)
                except Exception:  # noqa: BLE001
                    pass
                result = execute_statements(*conn_args, statements, fetch_rows=False)
                ticket.status = "success" if result["ok"] else "failed"
                ticket.exec_result = result["results"]
                ticket.executor = "scheduler"
                ticket.executed_at = format_app_datetime(now_dt())
                db.commit()
                _notify_ticket_result(db, ticket)
    finally:
        db.close()


# ───────────────────────── 库表浏览（选数据源带出库/表） ─────────────────────────

@router.get("/instances/{instance_id}/databases")
def list_databases(
    instance_id: int,
    db: Session = Depends(get_db),
    # 提交工单（submit）与执行/查询（execute）都要浏览库列表，任一即可。
    current_user: User = Depends(require_permissions("action.db.submit", "action.db.execute", match="any")),
):
    """列出实例上的全部数据库（工单/查询的目标库选择）。

    维护库（postgres/template*、mysql/sys 等）不返回——列出来只会让人误连，
    OpsPilot 自己的业务表从来不在那些库里。实例自己配置的库永远保留。
    """
    instance = _get_instance(db, instance_id)
    if not _user_can_use_instance(db, instance, current_user):
        raise HTTPException(status_code=403, detail="你的角色未被授权使用该数据源")
    conn = _build_connection(
        instance.db_type, instance.host, instance.port, instance.username,
        decrypt_secret(instance.password or ""), instance.database,
    )
    try:
        with conn.cursor() as cursor:
            if instance.db_type == "mysql":
                cursor.execute("SHOW DATABASES")
                names = [row[0] for row in cursor.fetchall()]
            else:
                cursor.execute("SELECT datname FROM pg_database WHERE datistemplate = false")
                names = [row[0] for row in cursor.fetchall()]
        return {
            "databases": sorted(filter_selectable_databases(instance.db_type, names, instance.database)),
            "default_database": instance.database,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取库列表失败: {str(exc)[:200]}")
    finally:
        conn.close()


@router.get("/instances/{instance_id}/tables")
def list_tables(
    instance_id: int,
    database: str = Query(..., description="目标库名"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.db.submit", "action.db.execute", match="any")),
):
    """列出库内全部表（含行数估计），供工单/查询页点选插入 SQL。"""
    instance = _get_instance(db, instance_id)
    if not _user_can_use_instance(db, instance, current_user):
        raise HTTPException(status_code=403, detail="你的角色未被授权使用该数据源")
    conn = _build_connection(
        instance.db_type, instance.host, instance.port, instance.username,
        decrypt_secret(instance.password or ""), database,
    )
    try:
        with conn.cursor() as cursor:
            if instance.db_type == "mysql":
                cursor.execute(
                    "SELECT table_name, table_rows FROM information_schema.tables "
                    "WHERE table_schema = %s ORDER BY table_name",
                    (database,),
                )
                tables = [{"name": r[0], "rows": int(r[1] or 0)} for r in cursor.fetchall()]
            else:
                cursor.execute(
                    "SELECT relname, GREATEST(reltuples, 0)::bigint FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY relname",
                )
                tables = [{"name": r[0], "rows": int(r[1] or 0)} for r in cursor.fetchall()]
        return {"database": database, "tables": tables}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取表列表失败: {str(exc)[:200]}")
    finally:
        conn.close()


@router.get("/instances/{instance_id}/tables/{table_name}/schema")
def get_table_schema(
    instance_id: int,
    table_name: str,
    database: str = Query(..., description="目标库名"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.db.submit", "action.db.execute", match="any")),
):
    """数据字典：表的字段 / 索引 / 建表 DDL（MySQL 提供原版 DDL，PG 拼近似版）。

    供"表结构"抽屉展示，帮助审批人与提交人核对字段语义（对标 Yearning 数据字典）。
    """
    instance = _get_instance(db, instance_id)
    if not _user_can_use_instance(db, instance, current_user):
        raise HTTPException(status_code=403, detail="你的角色未被授权使用该数据源")
    conn = _build_connection(
        instance.db_type, instance.host, instance.port, instance.username,
        decrypt_secret(instance.password or ""), database,
    )
    try:
        columns: list[dict] = []
        indexes: list[dict] = []
        ddl = ""
        if instance.db_type == "mysql":
            # SHOW 语句不能参数化，反引号内的反引号用双写转义，堵住标识符注入。
            safe = table_name.replace("`", "``")
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW FULL COLUMNS FROM `{safe}`")
                for row in cursor.fetchall():
                    columns.append({
                        "name": row[0], "type": row[1], "nullable": row[3] == "YES",
                        "key": row[4] or "", "default": row[5], "extra": row[6] or "",
                        "comment": row[8] or "",
                    })
                cursor.execute(f"SHOW INDEX FROM `{safe}`")
                seen: set[tuple] = set()
                for row in cursor.fetchall():
                    ident = (row[2], row[4])  # Key_name + Column_name 去重（联合索引逐列一行）
                    if ident in seen:
                        continue
                    seen.add(ident)
                    indexes.append({
                        "name": row[2], "column": row[4], "unique": not row[1],
                        "type": row[10] if len(row) > 10 else "BTREE",
                    })
                cursor.execute(f"SHOW CREATE TABLE `{safe}`")
                ddl_row = cursor.fetchone()
                ddl = str(ddl_row[1] or "") if ddl_row else ""
        else:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT c.column_name, c.data_type, c.is_nullable, c.column_default, "
                    "pd.description "
                    "FROM information_schema.columns c "
                    "LEFT JOIN pg_catalog.pg_statio_all_tables st "
                    "  ON st.schemaname = c.table_schema AND st.tablename = c.table_name "
                    "LEFT JOIN pg_catalog.pg_description pd "
                    "  ON pd.objoid = st.relid AND pd.objsubid = c.ordinal_position "
                    "WHERE c.table_schema = 'public' AND c.table_name = %s "
                    "ORDER BY c.ordinal_position",
                    (table_name,),
                )
                for row in cursor.fetchall():
                    columns.append({
                        "name": row[0], "type": row[1], "nullable": row[2] == "YES",
                        "key": "", "default": row[3], "extra": "",
                        "comment": row[4] or "",
                    })
                cursor.execute(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname",
                    (table_name,),
                )
                for row in cursor.fetchall():
                    indexes.append({
                        "name": row[0], "column": "", "unique": "UNIQUE" in str(row[1]).upper(),
                        "type": str(row[1]),
                    })
        if not columns:
            raise HTTPException(status_code=404, detail=f"表 {table_name} 不存在（库 {database}）")
        return {
            "database": database,
            "table": table_name,
            "columns": columns,
            "indexes": indexes,
            "ddl": ddl,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取表结构失败: {str(exc)[:200]}")
    finally:
        conn.close()
