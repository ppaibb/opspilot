"""WireGuard 虚拟组网接口。

能力（对标 wg-easy，叠加 OpsPilot 主机联动）：
1. 组网 CRUD：创建时自动生成服务端密钥，出口节点固定占网段 .1；
2. 成员管理：添加（自动分配隧道 IP）、启停、删除；
3. 配置出口：服务端/成员配置下载、成员二维码（移动端扫码即用）；
4. 部署脚本：内嵌配置的一键安装脚本，可粘贴到「脚本执行」批量下发。

安全约定：私钥加密落库，接口不回传明文；明文只经配置下载/二维码两个出口。
"""
from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.user import User
from app.models.wireguard import WireGuardNetwork, WireGuardPeer
from app.schemas.wireguard import NetworkCreate, NetworkUpdate, PeerCreate, PeerUpdate
from app.services.application_time import format_app_datetime, now_dt
from app.services.wireguard import (
    allocate_peer_ip,
    generate_keypair,
    peer_qr_data_url,
    render_peer_config,
    render_peer_install_script,
    render_server_config,
    render_server_install_script,
    seal_key,
    serialize_network,
    serialize_peer,
)

router = APIRouter()


# ───────────────────────── 内部工具 ─────────────────────────

def _get_network(db: Session, network_id: int) -> WireGuardNetwork:
    network = db.query(WireGuardNetwork).filter(WireGuardNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="组网不存在")
    return network


def _validate_cidr(cidr: str) -> ipaddress.IPv4Network:
    """校验网段格式；仅接受 IPv4（WireGuard 场景 v6 暂不开放）。"""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"非法网段: {cidr}")
    if network.version != 4 or network.prefixlen > 28:
        raise HTTPException(status_code=422, detail="仅支持 IPv4 网段，且掩码不大于 /28（避免地址计算过大）")
    return network


def _peer_config_response(network: WireGuardNetwork, peer: WireGuardPeer) -> dict[str, Any]:
    config = render_peer_config(network, peer)
    return {
        "peer": serialize_peer(peer),
        "config": config,
        "qr": peer_qr_data_url(config),
        "install_script": render_peer_install_script(network, peer, config),
    }


# ───────────────────────── 组网 CRUD ─────────────────────────

@router.get("/networks")
def list_networks(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    networks = db.query(WireGuardNetwork).order_by(WireGuardNetwork.id.desc()).all()
    counts = dict(_peer_counts(db))
    return [serialize_network(n, counts.get(n.id, 0)) for n in networks]


def _peer_counts(db: Session) -> list[tuple[int, int]]:
    rows = (
        db.query(WireGuardPeer.network_id, WireGuardPeer.id)
        .all()
    )
    result: dict[int, int] = {}
    for network_id, _ in rows:
        result[network_id] = result.get(network_id, 0) + 1
    return list(result.items())


@router.post("/networks")
def create_network(
    payload: NetworkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
):
    """创建组网：校验网段、生成服务端密钥、占用 .1 给出口节点。"""
    network = _validate_cidr(payload.cidr)
    # 同名组网拒绝（避免运维混淆）。
    exists = db.query(WireGuardNetwork).filter(WireGuardNetwork.name == payload.name.strip()).first()
    if exists:
        raise HTTPException(status_code=422, detail=f"组网名称已存在: {payload.name}")
    # 网段重叠检测：两个组网地址空间重叠会导致路由歧义。
    for other in db.query(WireGuardNetwork).all():
        if ipaddress.ip_network(other.cidr, strict=False).overlaps(network):
            raise HTTPException(status_code=422, detail=f"网段与组网「{other.name}」重叠: {other.cidr}")

    private_key, public_key = generate_keypair()
    record = WireGuardNetwork(
        name=payload.name.strip(),
        cidr=str(network),
        server_asset_id=payload.server_asset_id,
        server_name=payload.server_name,
        endpoint=payload.endpoint.strip(),
        port=payload.port,
        server_private_key=seal_key(private_key),
        server_public_key=public_key,
        forward_iface=payload.forward_iface.strip() or "eth0",
        full_tunnel=1 if payload.full_tunnel else 0,
        dns=payload.dns.strip(),
        remark=payload.remark.strip(),
        created_at=format_app_datetime(now_dt()),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return serialize_network(record, 0)


@router.put("/networks/{network_id}")
def update_network(
    network_id: int,
    payload: NetworkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
):
    """编辑组网元信息（网段与密钥不可变——变更需重建组网并重发全部配置）。"""
    record = _get_network(db, network_id)
    record.name = payload.name.strip()
    record.endpoint = payload.endpoint.strip()
    record.port = payload.port
    record.forward_iface = payload.forward_iface.strip() or "eth0"
    record.full_tunnel = 1 if payload.full_tunnel else 0
    record.dns = payload.dns.strip()
    record.remark = payload.remark.strip()
    db.commit()
    db.refresh(record)
    peer_count = db.query(WireGuardPeer).filter(WireGuardPeer.network_id == record.id).count()
    return serialize_network(record, peer_count)


@router.delete("/networks/{network_id}")
def delete_network(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
    _risk=Depends(require_risk_ack("action.wireguard.manage", "delete")),
):
    """删除组网：连带删除全部成员记录（服务器上的 wg 接口需人工或脚本卸载）。

    删除不可恢复且影响在线成员隧道，需 wireguard:delete 二次确认。
    """
    record = _get_network(db, network_id)
    db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network_id).delete(synchronize_session=False)
    db.delete(record)
    db.commit()
    return {"message": "组网及成员记录已删除；服务器上的 wg 接口请通过脚本卸载"}


# ───────────────────────── 成员管理 ─────────────────────────

@router.get("/networks/{network_id}/peers")
def list_peers(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_network(db, network_id)
    peers = db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network_id).order_by(WireGuardPeer.id).all()
    return [serialize_peer(p) for p in peers]


@router.post("/networks/{network_id}/peers")
def create_peer(
    network_id: int,
    payload: PeerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
):
    """添加成员：生成密钥对；IP 手工指定或自动分配下一个空闲地址。"""
    network = _get_network(db, network_id)

    taken = [p.allowed_ip for p in db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network_id).all()]
    if payload.allowed_ip.strip():
        allowed_ip = payload.allowed_ip.strip()
        # 手工指定时校验：在网段内、未被占用。
        try:
            iface = ipaddress.ip_interface(allowed_ip)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"非法 IP: {allowed_ip}")
        cidr = ipaddress.ip_network(network.cidr, strict=False)
        if iface.ip not in cidr:
            raise HTTPException(status_code=422, detail=f"IP 不在网段 {network.cidr} 内")
        if f"{iface.ip}/{cidr.prefixlen}" in taken or allowed_ip in taken:
            raise HTTPException(status_code=422, detail=f"IP 已被占用: {allowed_ip}")
    else:
        allocated = allocate_peer_ip(network.cidr, taken)
        if not allocated:
            raise HTTPException(status_code=422, detail="网段地址已耗尽，请扩容网段或清理成员")
        allowed_ip = allocated

    private_key, public_key = generate_keypair()
    peer = WireGuardPeer(
        network_id=network_id,
        name=payload.name.strip(),
        asset_id=payload.asset_id,
        allowed_ip=allowed_ip,
        private_key=seal_key(private_key),
        public_key=public_key,
        enabled=1,
        remark=payload.remark.strip(),
        created_at=format_app_datetime(now_dt()),
    )
    db.add(peer)
    db.commit()
    db.refresh(peer)
    return serialize_peer(peer)


@router.patch("/peers/{peer_id}")
def update_peer(
    peer_id: int,
    payload: PeerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
):
    """启停成员或改备注：禁用后服务端配置（重新生成时）不再包含该 [Peer] 段。"""
    peer = db.query(WireGuardPeer).filter(WireGuardPeer.id == peer_id).first()
    if not peer:
        raise HTTPException(status_code=404, detail="成员不存在")
    if payload.enabled is not None:
        peer.enabled = 1 if payload.enabled else 0
    if payload.remark is not None:
        peer.remark = payload.remark.strip()
    db.commit()
    db.refresh(peer)
    return serialize_peer(peer)


@router.delete("/peers/{peer_id}")
def delete_peer(
    peer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permissions("action.wireguard.manage")),
    _risk=Depends(require_risk_ack("action.wireguard.manage", "delete")),
):
    """删除成员（不可恢复，需 wireguard:delete 二次确认）。"""
    peer = db.query(WireGuardPeer).filter(WireGuardPeer.id == peer_id).first()
    if not peer:
        raise HTTPException(status_code=404, detail="成员不存在")
    db.delete(peer)
    db.commit()
    return {"message": "成员已删除；请重新下载服务端配置并重载生效"}


# ───────────────────────── 配置出口 ─────────────────────────

@router.get("/networks/{network_id}/server-config")
def get_server_config(
    network_id: int,
    download: int = Query(default=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """服务端 wg0.conf（含启用的成员段）；download=1 时作为文件下发。"""
    network = _get_network(db, network_id)
    peers = db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network_id).all()
    config = render_server_config(network, peers)
    if download:
        return Response(
            content=config,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="wg0-{network.name}.conf"'},
        )
    return {"network": serialize_network(network, len(peers)), "config": config}


@router.get("/networks/{network_id}/server-install-script")
def get_server_install_script(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """出口节点一键安装脚本（内嵌配置，可直接在服务器执行或贴进脚本执行页）。"""
    network = _get_network(db, network_id)
    peers = db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network_id).all()
    config = render_server_config(network, peers)
    return {"script": render_server_install_script(network, config)}


@router.get("/networks/{network_id}/status")
def get_network_status(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """实时状态回读：SSH 到出口节点解析 wg show（握手/流量/在线）。"""
    from app.services.wireguard_status import read_network_status

    network = _get_network(db, network_id)
    return read_network_status(db, network)


@router.get("/by-asset/{asset_id}")
def lookup_by_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """主机联动：查询某台主机参与的组网与隧道 IP（供主机页抽屉展示）。"""
    peers = db.query(WireGuardPeer).filter(WireGuardPeer.asset_id == asset_id).all()
    result = []
    for peer in peers:
        network = db.query(WireGuardNetwork).filter(WireGuardNetwork.id == peer.network_id).first()
        if not network:
            continue
        result.append({
            "peer": serialize_peer(peer),
            "network": {
                "id": network.id,
                "name": network.name,
                "cidr": network.cidr,
                "endpoint": network.endpoint,
                "port": network.port,
                "full_tunnel": bool(network.full_tunnel),
            },
        })
    return {"asset_id": asset_id, "memberships": result}


@router.get("/peers/{peer_id}/config")
def get_peer_config(
    peer_id: int,
    download: int = Query(default=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """成员配置 + 二维码 + 安装脚本（一次性返回，前端弹窗展示）。"""
    peer = db.query(WireGuardPeer).filter(WireGuardPeer.id == peer_id).first()
    if not peer:
        raise HTTPException(status_code=404, detail="成员不存在")
    network = _get_network(db, peer.network_id)
    payload = _peer_config_response(network, peer)
    if download:
        safe_name = peer.name.replace(" ", "_") or f"peer-{peer.id}"
        return Response(
            content=payload["config"],
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.conf"'},
        )
    return payload
