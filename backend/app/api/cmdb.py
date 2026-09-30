"""CMDB 关联图谱：把「应用 ↔ 资产」从业务表里拼成节点/边，供拓扑与影响面分析。

节点：
- application（业务应用）— 有 owner / environment / category
- host / vm / domain / certificate（受管资产）

边：application_resources 的绑定关系（app → resource）。
第一版只做「已有绑定」的可视化与影响面反查，不新建关系表。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.application import Application, ApplicationResource
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord as Certificate
from app.models.vm import VirtualMachine as VM
from app.core.security import get_current_user, require_permissions

router = APIRouter()
CMDB_PERM = require_permissions("module.apps.list")

NODE_KINDS = {"application", "host", "vm", "domain", "certificate"}
RESOURCE_KINDS = {"host", "vm", "domain", "certificate"}


def _resource_label(kind: str, row) -> str:
    if kind == "host":
        return row.name or row.ip or row.instance_id
    if kind == "vm":
        return getattr(row, "name", None) or getattr(row, "instance_id", None) or str(getattr(row, "id", ""))
    if kind == "domain":
        return row.domain or str(row.id)
    return getattr(row, "name", None) or getattr(row, "cert_id", None) or str(getattr(row, "id", ""))


def _resource_biz_id(kind: str, row) -> str:
    if kind == "host":
        return row.instance_id
    if kind == "vm":
        return str(getattr(row, "vm_id", None) or getattr(row, "instance_id", None) or row.id)
    if kind == "domain":
        return str(row.id)
    return str(getattr(row, "cert_id", None) or row.id)


def _load_resource_index(db: Session) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for row in db.query(Asset).all():
        biz = row.instance_id
        index[("host", biz)] = {
            "type": "host",
            "id": biz,
            "label": _resource_label("host", row),
            "status": row.type or "",
            "ip": row.ip or "",
            "group_id": row.group_id,
        }
    for row in db.query(VM).all():
        biz = _resource_biz_id("vm", row)
        index[("vm", biz)] = {
            "type": "vm",
            "id": biz,
            "label": _resource_label("vm", row),
            "status": getattr(row, "status", "") or "",
            "ip": getattr(row, "ip", "") or "",
            "group_id": getattr(row, "group_id", None),
        }
    for row in db.query(DomainRecord).all():
        biz = str(row.id)
        index[("domain", biz)] = {
            "type": "domain",
            "id": biz,
            "label": _resource_label("domain", row),
            "status": row.status or "",
            "ip": row.record_value or "",
            "group_id": None,
        }
    for row in db.query(Certificate).all():
        biz = _resource_biz_id("certificate", row)
        index[("certificate", biz)] = {
            "type": "certificate",
            "id": biz,
            "label": _resource_label("certificate", row),
            "status": getattr(row, "status", "") or "",
            "ip": "",
            "group_id": None,
        }
    return index


def _app_node(app: Application) -> dict:
    return {
        "type": "application",
        "id": app.app_id,
        "label": app.name or app.app_id,
        "status": app.status or "active",
        "owner": app.owner or "",
        "environment": app.environment or "",
        "category": app.category or "",
        "tags": list(app.tags or []),
    }


@router.get("/graph")
def get_cmdb_graph(
    environment: str = Query(default=""),
    owner: str = Query(default=""),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
    __: object = Depends(CMDB_PERM),
):
    """返回 CMDB 拓扑：应用节点 + 资产节点 + 绑定边。

    可选按应用的 environment / owner 过滤（过滤只作用于应用，连带裁剪边）。
    """
    apps_q = db.query(Application)
    if environment.strip():
        apps_q = apps_q.filter(Application.environment == environment.strip())
    if owner.strip():
        apps_q = apps_q.filter(Application.owner.contains(owner.strip()))
    apps = apps_q.all()
    app_ids = {a.app_id for a in apps}

    nodes: list[dict] = [_app_node(a) for a in apps]
    edges: list[dict] = []
    seen_nodes: set[tuple[str, str]] = set()

    resource_index = _load_resource_index(db)
    bindings = (
        db.query(ApplicationResource)
        .filter(ApplicationResource.app_id.in_(app_ids) if app_ids else False)
        .all()
        if app_ids
        else []
    )

    for row in bindings:
        key = (row.resource_type, str(row.resource_id))
        info = resource_index.get(key)
        if not info:
            # 绑定了但资产已删：仍画出悬空节点，避免影响面漏报
            info = {
                "type": row.resource_type,
                "id": str(row.resource_id),
                "label": row.resource_name or f"{row.resource_type}/{row.resource_id}",
                "status": "missing",
                "ip": "",
                "group_id": None,
            }
        if key not in seen_nodes:
            seen_nodes.add(key)
            nodes.append(info)
        edges.append({
            "source": row.app_id,
            "target": f"{row.resource_type}:{row.resource_id}",
            "kind": "binds",
        })

    # 节点 id 统一带类型前缀，避免 host:id-1 与 vm:id-1 撞车
    normalized = []
    for n in nodes:
        nid = n["id"] if n["type"] == "application" else f"{n['type']}:{n['id']}"
        normalized.append({**n, "id": nid})
    return {"nodes": normalized, "edges": edges, "total_nodes": len(normalized), "total_edges": len(edges)}


@router.get("/impact")
def get_cmdb_impact(
    type: str = Query(..., description="application/host/vm/domain/certificate"),
    id: str = Query(..., description="节点业务 ID"),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
    __: object = Depends(CMDB_PERM),
):
    """影响面分析：给定节点，返回关联应用/资产与责任信息。"""
    node_type = (type or "").strip().lower()
    node_id = (id or "").strip()
    if node_type not in NODE_KINDS or not node_id:
        raise HTTPException(status_code=400, detail="type/id 不合法")

    resource_index = _load_resource_index(db)

    if node_type == "application":
        app = db.query(Application).filter(Application.app_id == node_id).first()
        if not app:
            raise HTTPException(status_code=404, detail="应用不存在")
        bindings = db.query(ApplicationResource).filter(ApplicationResource.app_id == node_id).all()
        related = []
        for row in bindings:
            info = resource_index.get((row.resource_type, str(row.resource_id))) or {
                "type": row.resource_type,
                "id": str(row.resource_id),
                "label": row.resource_name or str(row.resource_id),
                "status": "missing",
            }
            related.append(info)
        return {
            "node": _app_node(app),
            "apps": [_app_node(app)],
            "resources": related,
            "owners": [app.owner] if app.owner else [],
        }

    bindings = (
        db.query(ApplicationResource)
        .filter(
            ApplicationResource.resource_type == node_type,
            ApplicationResource.resource_id == node_id,
        )
        .all()
    )
    apps = []
    for b in bindings:
        app = db.query(Application).filter(Application.app_id == b.app_id).first()
        if app:
            apps.append(app)
    info = resource_index.get((node_type, node_id))
    if not info and not apps:
        raise HTTPException(status_code=404, detail="节点不存在或未纳管")
    owners = sorted({a.owner for a in apps if a.owner})
    return {
        "node": info or {"type": node_type, "id": node_id, "label": node_id, "status": "missing"},
        "apps": [_app_node(a) for a in apps],
        "resources": [info] if info else [],
        "owners": owners,
        "impact_summary": {
            "app_count": len(apps),
            "owner_count": len(owners),
            "environments": sorted({a.environment for a in apps if a.environment}),
        },
    }
