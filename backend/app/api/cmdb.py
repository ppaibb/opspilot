"""CMDB 关联图谱：把「应用 ↔ 资产」与「资产 ↔ 资产」从业务表里拼成节点/边，
供拓扑与影响面分析。

节点：
- application（业务应用）— 有 owner / environment / category
- host / vm / domain / certificate / credential（受管资产与凭据）

边：
- application_resources 的绑定关系（app → resource），kind='binds'。
- 资产间派生边（不新加表，只从已有字段派生），kind 固定为：
  - issued_for：证书 → 域名（primary_domain / san_domains 精确匹配，支持通配符）
  - acme_account：证书 → 凭据（CertificateRecord.account_id）
  - dns_account：域名 → 凭据（DomainRecord.account_id；dns_credential 只是展示名，不用）
  - ssh_credential：主机 → 凭据（Asset.credential_ref）
  - deployed_to：证书 → 主机（CertificateDeployment，target_type='host'，
    解析 results_json 里的 asset_id；targets 摘要字符串不解析）
  - app_credential：应用 → 凭据（ApplicationConfig，value_type='credential_ref'
    且有 app_id + credential_ref 时）
- Asset.cloud_credential_ref 暂不画边：它是主机导入时用的云账号 AK/SK 凭证，
  不是主机本身使用的 SSH 凭据，语义与 ssh_credential 不同，且固定 kind 表里
  没有对应 kind，不硬套。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.application import Application, ApplicationConfig, ApplicationResource
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord as Certificate
from app.models.certificate_deployment import CertificateDeployment
from app.models.credential import Credential
from app.models.vm import VirtualMachine as VM
from app.core.security import get_current_user, require_permissions

router = APIRouter()
CMDB_PERM = require_permissions("module.apps.list")

NODE_KINDS = {"application", "host", "vm", "domain", "certificate", "credential"}
RESOURCE_KINDS = {"host", "vm", "domain", "certificate", "credential"}

# 资产间边的固定 kind（前端按这套做图例，不要改拼写）
ASSET_EDGE_KINDS = {
    "issued_for",
    "acme_account",
    "dns_account",
    "ssh_credential",
    "deployed_to",
    "app_credential",
}


def _resource_label(kind: str, row) -> str:
    if kind == "host":
        return row.name or row.ip or row.instance_id
    if kind == "vm":
        return getattr(row, "name", None) or getattr(row, "instance_id", None) or str(getattr(row, "id", ""))
    if kind == "domain":
        return row.domain or str(row.id)
    if kind == "credential":
        return getattr(row, "name", None) or getattr(row, "cred_id", None) or str(getattr(row, "id", ""))
    return getattr(row, "name", None) or getattr(row, "cert_id", None) or str(getattr(row, "id", ""))


def _resource_biz_id(kind: str, row) -> str:
    if kind == "host":
        return row.instance_id
    if kind == "vm":
        return str(getattr(row, "vm_id", None) or getattr(row, "instance_id", None) or row.id)
    if kind == "domain":
        return str(row.id)
    if kind == "credential":
        return str(getattr(row, "cred_id", None) or row.id)
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
    for row in db.query(Credential).all():
        biz = _resource_biz_id("credential", row)
        index[("credential", biz)] = {
            "type": "credential",
            "id": biz,
            "label": _resource_label("credential", row),
            "status": getattr(row, "status", "") or "",
            "ip": "",
            "group_id": None,
        }
    return index


def _prefixed(kind: str, biz_id: str) -> str:
    return f"{kind}:{biz_id}"


def _split_prefixed(node_id: str) -> tuple[str, str]:
    kind, sep, biz = str(node_id).partition(":")
    if not sep:
        # 应用节点 id 不带类型前缀
        return "application", kind
    return kind, biz


def _cert_domain_matches(cert_domain: str, record) -> bool:
    """证书域名是否覆盖一条域名记录。

    非通配符：DomainRecord.domain 精确匹配。
    通配符 `*.example.com`：domain == 'example.com'、或以 '.example.com' 结尾、
    或 root_domain == 去星号后的根域 'example.com'。
    """
    wanted = (cert_domain or "").strip().lower()
    if not wanted:
        return False
    rec_domain = (getattr(record, "domain", "") or "").strip().lower()
    if wanted.startswith("*."):
        base = wanted[2:]
        if not base:
            return False
        if rec_domain == base or rec_domain.endswith("." + base):
            return True
        return (getattr(record, "root_domain", "") or "").strip().lower() == base
    return rec_domain == wanted


def _load_asset_edges(db: Session, resource_index: dict) -> list[dict]:
    """从已有字段派生全部资产间边（不含应用 → 凭据，见 _load_app_credential_edges）。

    引用不存在的凭据/主机/证书时不画边、不补悬空节点；同一对节点同一 kind 只画一条。
    """
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(source: str, target: str, kind: str) -> None:
        key = (source, target, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append({"source": source, "target": target, "kind": kind})

    certs = db.query(Certificate).all()
    domains = db.query(DomainRecord).all()

    for cert in certs:
        cert_node = _prefixed("certificate", _resource_biz_id("certificate", cert))
        names: list[str] = []
        if (cert.primary_domain or "").strip():
            names.append(cert.primary_domain)
        for part in (cert.san_domains or "").split(","):
            if part.strip():
                names.append(part)
        for name in names:
            for rec in domains:
                if _cert_domain_matches(name, rec):
                    add(cert_node, _prefixed("domain", str(rec.id)), "issued_for")
        acct = (cert.account_id or "").strip()
        if acct and ("credential", acct) in resource_index:
            add(cert_node, _prefixed("credential", acct), "acme_account")

    for rec in domains:
        acct = (rec.account_id or "").strip()
        if acct and ("credential", acct) in resource_index:
            add(_prefixed("domain", str(rec.id)), _prefixed("credential", acct), "dns_account")

    for asset in db.query(Asset).all():
        ref = (asset.credential_ref or "").strip()
        if ref and ("credential", ref) in resource_index:
            add(_prefixed("host", asset.instance_id), _prefixed("credential", ref), "ssh_credential")
        # cloud_credential_ref 暂不画边：见模块 docstring。

    for dep in db.query(CertificateDeployment).all():
        if (dep.target_type or "host").strip().lower() != "host":
            continue
        if ("certificate", str(dep.cert_id)) not in resource_index:
            continue
        try:
            results = json.loads(dep.results_json or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            # 只画部署成功的边：失败的部署证书并没落到主机上，画边会撒谎。
            if not item.get("success"):
                continue
            asset_id = str(item.get("asset_id") or "").strip()
            if not asset_id:
                continue
            if ("host", asset_id) not in resource_index:
                continue
            add(
                _prefixed("certificate", str(dep.cert_id)),
                _prefixed("host", asset_id),
                "deployed_to",
            )

    return edges


def _load_app_credential_edges(
    db: Session, resource_index: dict, app_ids: set[str]
) -> list[dict]:
    """应用 → 凭据边：ApplicationConfig 有 app_id + credential_ref
    且 value_type == 'credential_ref' 时才画；凭据不存在则不画边。"""
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if not app_ids:
        return edges
    for cfg in db.query(ApplicationConfig).all():
        if (cfg.value_type or "") != "credential_ref":
            continue
        if (cfg.app_id or "") not in app_ids:
            continue
        ref = (cfg.credential_ref or "").strip()
        if not ref or ("credential", ref) not in resource_index:
            continue
        key = (cfg.app_id, ref)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {"source": cfg.app_id, "target": _prefixed("credential", ref), "kind": "app_credential"}
        )
    return edges


def _add_edge_endpoint_nodes(
    nodes: list[dict],
    seen_nodes: set[tuple[str, str]],
    resource_index: dict,
    edges: list[dict],
) -> None:
    """把资产间边的端点节点补进节点列表（应用端点已在应用节点里，不重复加）。"""
    for edge in edges:
        for endpoint in (edge["source"], edge["target"]):
            kind, biz = _split_prefixed(endpoint)
            if kind == "application" or not biz:
                continue
            key = (kind, biz)
            if key in seen_nodes:
                continue
            info = resource_index.get(key)
            if not info:
                continue
            seen_nodes.add(key)
            nodes.append(info)


def _neighbors_for_node(
    node_prefixed_id: str, db: Session, resource_index: dict
) -> list[dict]:
    """被查节点的全部资产间边邻居。

    direction='out'：被查节点 → 邻居；'in'：邻居 → 被查节点。
    """
    apps = db.query(Application).all()
    apps_by_id = {a.app_id: a for a in apps}
    edges = _load_asset_edges(db, resource_index)
    edges += _load_app_credential_edges(db, resource_index, set(apps_by_id))

    neighbors: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        if edge["source"] == node_prefixed_id:
            other, direction = edge["target"], "out"
        elif edge["target"] == node_prefixed_id:
            other, direction = edge["source"], "in"
        else:
            continue
        kind, biz = _split_prefixed(other)
        if kind == "application":
            app = apps_by_id.get(biz)
            if not app:
                continue
            info = {
                "type": "application",
                "id": biz,
                "label": app.name or biz,
                "status": app.status or "active",
            }
        else:
            info = resource_index.get((kind, biz))
            if not info:
                continue
            info = {
                "type": info["type"],
                "id": info["id"],
                "label": info["label"],
                "status": info["status"],
            }
        dedup = (info["type"], info["id"], edge["kind"], direction)
        if dedup in seen:
            continue
        seen.add(dedup)
        neighbors.append({**info, "kind": edge["kind"], "direction": direction})
    return neighbors


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
    """返回 CMDB 拓扑：应用节点 + 资产节点 + 绑定边 + 资产间派生边。

    应用过滤（environment / owner）只作用于应用节点及其 binds 边；
    资产间边不受应用过滤影响，0 应用时图也不空。
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

    # 资产间边：不受应用过滤影响；应用 → 凭据边只涉及其已过滤出的应用
    asset_edges = _load_asset_edges(db, resource_index)
    asset_edges += _load_app_credential_edges(db, resource_index, app_ids)
    _add_edge_endpoint_nodes(nodes, seen_nodes, resource_index, asset_edges)
    edges.extend(asset_edges)

    # 节点 id 统一带类型前缀，避免 host:id-1 与 vm:id-1 撞车
    normalized = []
    for n in nodes:
        nid = n["id"] if n["type"] == "application" else f"{n['type']}:{n['id']}"
        normalized.append({**n, "id": nid})
    return {"nodes": normalized, "edges": edges, "total_nodes": len(normalized), "total_edges": len(edges)}


@router.get("/impact")
def get_cmdb_impact(
    type: str = Query(..., description="application/host/vm/domain/certificate/credential"),
    id: str = Query(..., description="节点业务 ID"),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
    __: object = Depends(CMDB_PERM),
):
    """影响面分析：给定节点，返回关联应用/资产与责任信息，外加 neighbors（全部资产间边邻居）。"""
    node_type = (type or "").strip().lower()
    node_id = (id or "").strip()
    if node_type not in NODE_KINDS or not node_id:
        raise HTTPException(status_code=400, detail="type/id 不合法")

    resource_index = _load_resource_index(db)
    node_prefixed_id = node_id if node_type == "application" else _prefixed(node_type, node_id)

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
            "neighbors": _neighbors_for_node(node_prefixed_id, db, resource_index),
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
        "neighbors": _neighbors_for_node(node_prefixed_id, db, resource_index),
    }
