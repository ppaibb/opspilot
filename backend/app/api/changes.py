"""通用变更工单 API：申请 → 多级审批 → 执行 → 归档。

设计对齐数据库工单（DbTicket）：
- 审批链 `approval_steps` 快照，逐级 `approve`/`reject`
- 执行分发到 `EXECUTORS` 注册表；未注册的类型只记工单、由人工执行后标记
- 高危执行走 `require_risk_ack`，与页面确认框并存
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.asset import Asset
from app.models.certificate import CertificateRecord
from app.models.change_ticket import ChangeTicket
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter()

# 变更类型 → 展示名
CHANGE_TYPES = {
    "host.delete": "删除主机",
    "host.batch_delete": "批量删除主机",
    "certificate.delete": "删除证书",
    "k8s.drain": "节点排水（Drain）",
    "k8s.delete": "删除 K8s 资源",
    "custom": "自定义变更",
}

# 执行器：入参 (db, ticket) -> (ok, message, raw)
# v1 只落地主机删除；其余类型走人工执行（executor 手动标记成功/失败）。
ExecFn = Callable[[Session, ChangeTicket], tuple[bool, str, dict]]


def _exec_host_delete(db: Session, ticket: ChangeTicket) -> tuple[bool, str, dict]:
    asset_id = (ticket.resource_id or "").strip()
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        return False, f"主机不存在: {asset_id}", {"asset_id": asset_id}
    name = asset.name or asset_id
    db.delete(asset)
    db.commit()
    return True, f"已删除主机「{name}」", {"asset_id": asset_id, "name": name}


def _exec_certificate_delete(db: Session, ticket: ChangeTicket) -> tuple[bool, str, dict]:
    cert_id = (ticket.resource_id or "").strip()
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        return False, f"证书不存在: {cert_id}", {"cert_id": cert_id}
    name = row.name or cert_id
    db.delete(row)
    db.commit()
    return True, f"已删除证书「{name}」", {"cert_id": cert_id, "name": name}


EXECUTORS: dict[str, ExecFn] = {
    "host.delete": _exec_host_delete,
    "certificate.delete": _exec_certificate_delete,
}


class ChangeTicketCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    change_type: str = Field(..., description="CHANGE_TYPES 之一")
    risk_level: str = Field("high", description="medium/high")
    resource_type: str = ""
    resource_id: str = ""
    resource_name: str = ""
    impact: str = ""
    rollback_plan: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    window_start: str = ""
    window_end: str = ""
    approval_steps: list[str] = Field(default_factory=lambda: ["管理员"], description="审批角色链，空=创建即 approved")


class ReviewBody(BaseModel):
    note: str = ""


class ExecuteBody(BaseModel):
    manual_result: bool | None = Field(None, description="人工执行结果；自动执行器类型可省略")


def _now() -> datetime:
    return datetime.utcnow()


def _display_name(user: User) -> str:
    return user.name or user.username


def _serialize(row: ChangeTicket) -> dict:
    return {
        "id": row.id,
        "ticket_no": row.ticket_no,
        "title": row.title,
        "change_type": row.change_type,
        "change_type_label": CHANGE_TYPES.get(row.change_type, row.change_type),
        "risk_level": row.risk_level,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_name": row.resource_name,
        "impact": row.impact or "",
        "rollback_plan": row.rollback_plan or "",
        "detail": row.detail or {},
        "status": row.status,
        "window_start": row.window_start or "",
        "window_end": row.window_end or "",
        "submitter": row.submitter,
        "reviewer": row.reviewer,
        "executor": row.executor,
        "review_note": row.review_note or "",
        "approval_steps": row.approval_steps or [],
        "current_step": row.current_step or 1,
        "approval_log": row.approval_log or [],
        "exec_result": row.exec_result or {},
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else "",
        "approved_at": row.approved_at.isoformat(sep=" ", timespec="seconds") if row.approved_at else "",
        "started_at": row.started_at.isoformat(sep=" ", timespec="seconds") if row.started_at else "",
        "finished_at": row.finished_at.isoformat(sep=" ", timespec="seconds") if row.finished_at else "",
        "auto_executor": row.change_type in EXECUTORS,
    }


def _ticket_no(db: Session) -> str:
    day = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"CHG-{day}-"
    n = db.query(ChangeTicket).filter(ChangeTicket.ticket_no.like(f"{prefix}%")).count()
    return f"{prefix}{n + 1:04d}"


def _in_window(row: ChangeTicket) -> bool:
    start, end = (row.window_start or "").strip(), (row.window_end or "").strip()
    if not start and not end:
        return True
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


@router.get("/types")
def list_change_types(_: User = Depends(get_current_user)):
    return [
        {"key": k, "label": v, "auto_executor": k in EXECUTORS}
        for k, v in CHANGE_TYPES.items()
    ]


@router.get("")
def list_tickets(
    status: str = Query(default=""),
    change_type: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(ChangeTicket)
    if status.strip():
        query = query.filter(ChangeTicket.status == status.strip())
    if change_type.strip():
        query = query.filter(ChangeTicket.change_type == change_type.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            ChangeTicket.title.like(like)
            | ChangeTicket.ticket_no.like(like)
            | ChangeTicket.resource_name.like(like)
        )
    total = query.count()
    rows = (
        query.order_by(ChangeTicket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "items": [_serialize(r) for r in rows]}


@router.post("", status_code=201)
def create_ticket(
    payload: ChangeTicketCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.change.submit")),
):
    if payload.change_type not in CHANGE_TYPES:
        raise HTTPException(status_code=400, detail=f"未知变更类型: {payload.change_type}")
    steps = [s for s in (payload.approval_steps or []) if s]
    status = "pending" if steps else "approved"
    row = ChangeTicket(
        ticket_no=_ticket_no(db),
        title=payload.title.strip(),
        change_type=payload.change_type,
        risk_level=payload.risk_level or "high",
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        resource_name=payload.resource_name,
        impact=payload.impact,
        rollback_plan=payload.rollback_plan,
        detail=payload.detail or {},
        status=status,
        window_start=payload.window_start,
        window_end=payload.window_end,
        submitter=_display_name(current_user),
        approval_steps=steps,
        current_step=1,
        approval_log=[],
        created_at=_now(),
        approved_at=_now() if not steps else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("变更工单创建 %s by %s type=%s", row.ticket_no, row.submitter, row.change_type)
    return _serialize(row)


@router.get("/{ticket_id}")
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(ChangeTicket).filter(ChangeTicket.id == ticket_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")
    return _serialize(row)


@router.post("/{ticket_id}/approve")
def approve_ticket(
    ticket_id: int,
    body: ReviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.change.approve")),
):
    row = db.query(ChangeTicket).filter(ChangeTicket.id == ticket_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row.status != "pending":
        raise HTTPException(status_code=400, detail=f"当前状态不可审批: {row.status}")
    steps = list(row.approval_steps or [])
    log = list(row.approval_log or [])
    step = int(row.current_step or 1)
    log.append(
        {
            "step": step,
            "role": steps[step - 1] if steps and step <= len(steps) else "",
            "reviewer": _display_name(current_user),
            "decision": "approve",
            "note": body.note or "",
            "at": _now().isoformat(sep=" ", timespec="seconds"),
        }
    )
    row.approval_log = log
    if step < len(steps):
        row.current_step = step + 1
    else:
        row.status = "approved"
        row.reviewer = _display_name(current_user)
        row.review_note = body.note or ""
        row.approved_at = _now()
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.post("/{ticket_id}/reject")
def reject_ticket(
    ticket_id: int,
    body: ReviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.change.approve")),
):
    row = db.query(ChangeTicket).filter(ChangeTicket.id == ticket_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row.status != "pending":
        raise HTTPException(status_code=400, detail=f"当前状态不可驳回: {row.status}")
    log = list(row.approval_log or [])
    steps = list(row.approval_steps or [])
    step = int(row.current_step or 1)
    log.append(
        {
            "step": step,
            "role": steps[step - 1] if steps and step <= len(steps) else "",
            "reviewer": _display_name(current_user),
            "decision": "reject",
            "note": body.note or "",
            "at": _now().isoformat(sep=" ", timespec="seconds"),
        }
    )
    row.approval_log = log
    row.status = "rejected"
    row.reviewer = _display_name(current_user)
    row.review_note = body.note or ""
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.post("/{ticket_id}/cancel")
def cancel_ticket(
    ticket_id: int,
    body: ReviewBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.change.submit")),
):
    row = db.query(ChangeTicket).filter(ChangeTicket.id == ticket_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row.submitter != _display_name(current_user) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅提交人或管理员可取消")
    if row.status not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail=f"当前状态不可取消: {row.status}")
    row.status = "canceled"
    row.review_note = body.note or "提交人取消"
    db.commit()
    db.refresh(row)
    return _serialize(row)


@router.post("/{ticket_id}/execute")
def execute_ticket(
    ticket_id: int,
    body: ExecuteBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.change.execute")),
    _risk=Depends(require_risk_ack("action.change.execute", "execute")),
):
    row = db.query(ChangeTicket).filter(ChangeTicket.id == ticket_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")
    if row.status != "approved":
        raise HTTPException(status_code=400, detail=f"仅 approved 可执行，当前: {row.status}")
    if not _in_window(row):
        raise HTTPException(status_code=400, detail="未到变更窗口或窗口已过，请调整 window_start/window_end")

    row.status = "executing"
    row.executor = _display_name(current_user)
    row.started_at = _now()
    db.commit()

    exec_fn = EXECUTORS.get(row.change_type)
    try:
        if exec_fn is not None and body.manual_result is None:
            ok, message, raw = exec_fn(db, row)
        elif body.manual_result is not None:
            ok = bool(body.manual_result)
            message = "人工执行完成" if ok else "人工执行失败"
            raw = {"manual": True}
        else:
            ok, message, raw = False, f"类型 {row.change_type} 无自动执行器，请人工执行后带 manual_result 标记", {"manual": True}
    except Exception as exc:  # noqa: BLE001
        ok, message, raw = False, f"执行异常: {exc}", {"error": str(exc)}

    row.exec_result = {"ok": ok, "message": message, "raw": raw}
    row.status = "success" if ok else "failed"
    row.finished_at = _now()
    db.commit()
    db.refresh(row)
    logger.info("变更工单执行 %s -> %s (%s)", row.ticket_no, row.status, message)
    return _serialize(row)
