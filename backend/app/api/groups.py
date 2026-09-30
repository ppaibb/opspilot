"""
资源分组 API（方案 B:统一多态成员表）

提供分组的增删改查、资源归组(批量)以及按分组的资源/告警/费用聚合统计。
归属关系统一存放在 resource_group_members 表(resource_type + resource_id),
覆盖主机/虚机/集群/域名/证书等所有类型,业务表本身不需要 group_id 列。

主机为兼容既有前端仍同步维护 assets.group_id(双写),但成员表是新的归属事实来源。

设计要点:
- 资源与分组一对多:(resource_type, resource_id) 唯一,至多归属一个分组。
- 删除分组时不删资源,仅删除其成员行(主机另置空 assets.group_id),安全可逆。
- 读接口只读本地 DB,不阻塞在外部调用上。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.core.security import get_current_user, require_permissions
from app.models.resource_group import ResourceGroup
from app.models.resource_group_member import (
    RESOURCE_TYPE_HOST,
    VALID_RESOURCE_TYPES,
    ResourceGroupMember,
)
from app.models.asset import Asset, DomainRecord
from app.models.vm import VirtualMachine
from app.models.cluster import Cluster
from app.models.certificate import CertificateRecord
from app.models.application import Application
from app.models.user import User

router = APIRouter()

WRITE_PERM = "action.assets.hosts.manage"

# 各资源类型 → (ORM 模型, 业务标识列)。新增类型只需在此登记一行。
RESOURCE_REGISTRY: dict[str, tuple[type, object]] = {
    RESOURCE_TYPE_HOST: (Asset, Asset.instance_id),
    "vm": (VirtualMachine, VirtualMachine.vm_id),
    "cluster": (Cluster, Cluster.cluster_id),
    "domain": (DomainRecord, DomainRecord.domain),
    "cert": (CertificateRecord, CertificateRecord.cert_id),
    "application": (Application, Application.app_id),
}


# ---------------------------- Schemas ----------------------------

class GroupCreate(BaseModel):
    name: str
    color: str = "#0052d9"
    description: str = ""
    sort_order: int = 0


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None


class GroupResponse(BaseModel):
    id: int
    name: str
    color: str = "#0052d9"
    description: str = ""
    sort_order: int = 0
    asset_count: int = 0  # 兼容字段:主机成员数
    member_count: int = 0  # 全类型成员总数
    counts: dict = {}  # 按类型细分,如 {"host":3,"vm":2}
    created_at: str = ""
    updated_at: str = ""


class AssignRequest(BaseModel):
    asset_ids: List[str]  # 资源业务标识列表(主机=instance_id,虚机=vm_id 等)
    group_id: Optional[int] = None  # None = 移出分组
    resource_type: str = RESOURCE_TYPE_HOST  # 兼容旧前端:默认按主机


# ---------------------------- Helpers ----------------------------

def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _validate_resource_type(resource_type: str) -> str:
    rt = (resource_type or RESOURCE_TYPE_HOST).strip()
    if rt not in VALID_RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"未知的资源类型: {rt}")
    return rt


def _member_counts(db: Session) -> dict[int, dict[str, int]]:
    """返回 {group_id: {resource_type: count}}。"""
    rows = (
        db.query(
            ResourceGroupMember.group_id,
            ResourceGroupMember.resource_type,
            func.count(ResourceGroupMember.id),
        )
        .group_by(ResourceGroupMember.group_id, ResourceGroupMember.resource_type)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for gid, rtype, cnt in rows:
        out.setdefault(gid, {})[rtype] = cnt
    return out


def serialize_group(g: ResourceGroup, counts: dict[str, int] | None = None) -> dict:
    counts = counts or {}
    return {
        "id": g.id,
        "name": g.name or "",
        "color": g.color or "#0052d9",
        "description": g.description or "",
        "sort_order": g.sort_order or 0,
        "asset_count": counts.get(RESOURCE_TYPE_HOST, 0),
        "member_count": sum(counts.values()),
        "counts": counts,
        "created_at": _fmt(g.created_at),
        "updated_at": _fmt(g.updated_at),
    }


# ---------------------------- CRUD ----------------------------

@router.get("", response_model=List[GroupResponse])
def list_groups(db: Session = Depends(get_db)):
    """列出所有分组,附带各类型成员数量。"""
    counts = _member_counts(db)
    groups = db.query(ResourceGroup).order_by(
        ResourceGroup.sort_order.asc(), ResourceGroup.id.asc()
    ).all()
    return [serialize_group(g, counts.get(g.id, {})) for g in groups]


@router.post("", response_model=GroupResponse)
def create_group(
    payload: GroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions(WRITE_PERM)),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分组名称不能为空")
    if db.query(ResourceGroup).filter(ResourceGroup.name == name).first():
        raise HTTPException(status_code=409, detail=f"分组「{name}」已存在")
    g = ResourceGroup(
        name=name,
        color=(payload.color or "#0052d9").strip(),
        description=(payload.description or "").strip(),
        sort_order=payload.sort_order or 0,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return serialize_group(g, {})


@router.put("/{group_id}", response_model=GroupResponse)
def update_group(
    group_id: int,
    payload: GroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions(WRITE_PERM)),
):
    g = db.query(ResourceGroup).filter(ResourceGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="分组名称不能为空")
        dup = db.query(ResourceGroup).filter(
            ResourceGroup.name == new_name, ResourceGroup.id != group_id
        ).first()
        if dup:
            raise HTTPException(status_code=409, detail=f"分组「{new_name}」已存在")
        g.name = new_name
    if payload.color is not None:
        g.color = payload.color.strip()
    if payload.description is not None:
        g.description = payload.description.strip()
    if payload.sort_order is not None:
        g.sort_order = payload.sort_order
    db.commit()
    db.refresh(g)
    counts = _member_counts(db)
    return serialize_group(g, counts.get(g.id, {}))


@router.delete("/{group_id}")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions(WRITE_PERM)),
):
    g = db.query(ResourceGroup).filter(ResourceGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    # 删组不删资源:删成员行;主机另置空 group_id(双写,可逆)
    freed = db.query(ResourceGroupMember).filter(
        ResourceGroupMember.group_id == group_id
    ).count()
    db.query(Asset).filter(Asset.group_id == group_id).update(
        {Asset.group_id: None}, synchronize_session=False
    )
    db.query(ResourceGroupMember).filter(
        ResourceGroupMember.group_id == group_id
    ).delete(synchronize_session=False)
    db.delete(g)
    db.commit()
    return {"success": True, "freed_assets": freed}


# ---------------------------- 资源归组 ----------------------------

@router.get("/members")
def list_members(
    resource_type: str = RESOURCE_TYPE_HOST,
    db: Session = Depends(get_db),
):
    """返回某类资源的归属映射,供前端给列表着色 / 编辑回填分组。

    形如 [{resource_id, group_id, group_name, color}]。分组信息集中在本路由,
    业务列表接口(/api/vms 等)无需改动。
    """
    rt = _validate_resource_type(resource_type)
    groups = {g.id: g for g in db.query(ResourceGroup).all()}
    rows = db.query(ResourceGroupMember).filter(
        ResourceGroupMember.resource_type == rt
    ).all()
    out = []
    for m in rows:
        g = groups.get(m.group_id)
        out.append({
            "resource_id": m.resource_id,
            "group_id": m.group_id,
            "group_name": g.name if g else "",
            "color": (g.color if g else "") or "#0052d9",
        })
    return {"members": out, "resource_type": rt}


@router.post("/assign")
def assign_assets(
    payload: AssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions(WRITE_PERM)),
):
    """批量把资源归入分组(group_id=None 表示移出分组)。

    归属写入 resource_group_members;主机额外同步 assets.group_id(双写)。
    """
    rt = _validate_resource_type(payload.resource_type)
    if payload.group_id is not None:
        g = db.query(ResourceGroup).filter(ResourceGroup.id == payload.group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="目标分组不存在")
    ids = [i for i in (payload.asset_ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="未指定要归组的资源")

    # 先清掉这些资源的旧归属(同类型),再按需写入新归属
    db.query(ResourceGroupMember).filter(
        ResourceGroupMember.resource_type == rt,
        ResourceGroupMember.resource_id.in_(ids),
    ).delete(synchronize_session=False)

    if payload.group_id is not None:
        for rid in ids:
            db.add(ResourceGroupMember(
                group_id=payload.group_id,
                resource_type=rt,
                resource_id=rid,
            ))

    # 主机双写 assets.group_id,兼容既有前端
    if rt == RESOURCE_TYPE_HOST:
        db.query(Asset).filter(Asset.instance_id.in_(ids)).update(
            {Asset.group_id: payload.group_id}, synchronize_session=False
        )

    db.commit()
    return {"success": True, "updated": len(ids), "group_id": payload.group_id, "resource_type": rt}


# ---------------------------- 按分组统计 ----------------------------

@router.get("/stats")
def group_stats(
    resource_type: str = RESOURCE_TYPE_HOST,
    db: Session = Depends(get_db),
):
    """按分组聚合资源数(及主机的健康分布/费用估算)。

    resource_type 指定统计哪类资源(默认主机)。主机额外返回 healthy/critical 计数
    与规格费用估算指数(cpu_cores + memory_gb/2 + system_disk_gb/100);其余类型
    只返回成员计数。未分组单列。
    """
    rt = _validate_resource_type(resource_type)
    groups = db.query(ResourceGroup).order_by(
        ResourceGroup.sort_order.asc(), ResourceGroup.id.asc()
    ).all()

    if rt == RESOURCE_TYPE_HOST:
        return {"groups": _host_stats(db, groups)}

    # 通用类型:按成员表计数
    rows = (
        db.query(ResourceGroupMember.group_id, func.count(ResourceGroupMember.id))
        .filter(ResourceGroupMember.resource_type == rt)
        .group_by(ResourceGroupMember.group_id)
        .all()
    )
    count_map = {gid: cnt for gid, cnt in rows}
    result = []
    for g in groups:
        result.append({
            "group_id": g.id,
            "name": g.name,
            "color": g.color or "#0052d9",
            "host_count": count_map.get(g.id, 0),  # 兼容字段名,语义为该类型成员数
            "healthy": 0,
            "critical": 0,
            "cost_index": 0.0,
        })
    return {"groups": result, "resource_type": rt}


def _host_stats(db: Session, groups: list[ResourceGroup]) -> list[dict]:
    """主机维度的丰富统计(健康分布 + 费用估算指数)。"""
    group_map = {g.id: g for g in groups}
    buckets: dict[Optional[int], dict] = {}

    def _bucket(gid: Optional[int]) -> dict:
        if gid not in buckets:
            buckets[gid] = {"host_count": 0, "healthy": 0, "critical": 0, "cost_index": 0.0}
        return buckets[gid]

    for a in db.query(Asset).all():
        gid = a.group_id if (a.group_id is not None and a.group_id in group_map) else None
        b = _bucket(gid)
        b["host_count"] += 1
        if (a.type or "") == "healthy":
            b["healthy"] += 1
        elif (a.type or "") == "critical":
            b["critical"] += 1
        b["cost_index"] += round(
            float(a.cpu_cores or 0.0)
            + float(a.memory_gb or 0.0) / 2.0
            + float(a.system_disk_gb or 0.0) / 100.0,
            2,
        )

    result = []
    for g in groups:
        b = buckets.get(g.id, {"host_count": 0, "healthy": 0, "critical": 0, "cost_index": 0.0})
        result.append({
            "group_id": g.id,
            "name": g.name,
            "color": g.color or "#0052d9",
            **b,
            "cost_index": round(b["cost_index"], 2),
        })
    ungrouped = buckets.get(None)
    if ungrouped:
        result.append({
            "group_id": None,
            "name": "未分组",
            "color": "#94a3b8",
            **ungrouped,
            "cost_index": round(ungrouped["cost_index"], 2),
        })
    return result
