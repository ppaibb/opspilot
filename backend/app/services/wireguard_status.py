"""WireGuard 运行状态回读（SSH 到出口节点执行 wg show）。

设计要点：
1. 复用主机资产的 SSH 凭证体系（resolve_asset_ssh_auth），平台不落任何额外凭据；
2. `wg show <iface> dump` 输出制表符分隔：首行接口、随后每行一个 peer——
   解析为 {公钥: 握手时间/收发字节}，再按公钥映射回 wg_peers 记录；
3. online 判定：最近握手在 180 秒内（keepalive=25s，放宽到 3 分钟容忍抖动）；
4. 任何失败（无出口资产/SSH 不通/wg 未安装）都以结构化错误返回，不抛 5xx——
   状态是增强能力，不能阻塞组网管理主流程。
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.wireguard import WireGuardNetwork, WireGuardPeer
from app.services.asset_auth import resolve_asset_ssh_auth

# 在线判定窗口（秒）：keepalive=25s，容忍网络抖动放宽到 3 分钟。
ONLINE_WINDOW_SECONDS = 180
# SSH 命令执行超时。
SSH_COMMAND_TIMEOUT = 8


def parse_wg_dump(output: str) -> dict[str, dict[str, Any]]:
    """解析 `wg show <iface> dump` 输出。

    首行为接口行（私钥/公钥/端口...），其后每行一个 peer：
    public_key \\t preshared_key \\t endpoint \\t allowed_ips \\
    \\t latest_handshake(epoch) \\t rx_bytes \\t tx_bytes \\t keepalive
    """
    peers: dict[str, dict[str, Any]] = {}
    lines = [line for line in (output or "").splitlines() if line.strip()]
    for line in lines[1:]:  # 跳过接口行
        fields = line.split("\t")
        if len(fields) < 8:
            continue
        try:
            handshake = int(fields[4] or 0)
        except ValueError:
            handshake = 0
        peers[fields[0]] = {
            "endpoint": fields[2],
            "allowed_ips": fields[3],
            "latest_handshake": handshake,
            "rx_bytes": _to_int(fields[5]),
            "tx_bytes": _to_int(fields[6]),
        }
    return peers


def _to_int(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _run_ssh_command(asset: Asset, command: str, db: Session | None = None) -> str:
    """通过主机 SSH 凭证执行一次性命令并返回 stdout（同步、短超时）。"""
    import paramiko

    from app.services.ssh_client import pin_and_connect

    auth = resolve_asset_ssh_auth(asset)
    if not auth.password and not auth.pkey_content:
        raise RuntimeError(f"出口节点 {asset.name} 未配置 SSH 密码或凭证")

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, asset.ip, int(asset.ssh_port or 22), db=db)
    try:
        kwargs = dict(
            hostname=asset.ip,
            port=int(asset.ssh_port or 22),
            username=asset.ssh_user or "root",
            timeout=6.0,
            banner_timeout=6.0,
            auth_timeout=6.0,
        )
        if auth.pkey_content:
            pkey = paramiko.RSAKey.from_private_key_string(auth.pkey_content) if "BEGIN RSA" in auth.pkey_content else paramiko.Ed25519Key.from_private_key_string(auth.pkey_content)
            client.connect(pkey=pkey, **kwargs)
        else:
            client.connect(password=auth.password, **kwargs)
        _, stdout, stderr = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError(f"wg 命令执行失败: {error or f'exit {exit_code}'}")
        return output
    finally:
        client.close()


def read_network_status(db: Session, network: WireGuardNetwork) -> dict[str, Any]:
    """读取一个组网的实时状态：接口信息 + 各成员握手/流量/在线判定。"""
    now = int(time.time())

    if not network.server_asset_id:
        return {"available": False, "message": "组网未关联出口节点资产，无法回读状态"}
    asset = db.query(Asset).filter(Asset.instance_id == network.server_asset_id).first()
    if not asset:
        return {"available": False, "message": f"出口节点资产不存在: {network.server_asset_id}"}

    try:
        output = _run_ssh_command(asset, "wg show wg0 dump 2>/dev/null || wg show all dump", db=db)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "message": f"状态回读失败（SSH 不通或 wg 未安装）: {str(exc)[:160]}"}

    live = parse_wg_dump(output)
    peers = db.query(WireGuardPeer).filter(WireGuardPeer.network_id == network.id).all()

    items = []
    for peer in peers:
        info = live.get(peer.public_key)
        handshake = int(info["latest_handshake"]) if info else 0
        items.append({
            "peer_id": peer.id,
            "name": peer.name,
            "allowed_ip": peer.allowed_ip,
            "public_key": peer.public_key,
            "online": bool(handshake and now - handshake < ONLINE_WINDOW_SECONDS),
            "latest_handshake": handshake,
            "latest_handshake_text": _format_handshake(handshake, now),
            "endpoint": info["endpoint"] if info else "",
            "rx_bytes": info["rx_bytes"] if info else 0,
            "tx_bytes": info["tx_bytes"] if info else 0,
        })

    return {
        "available": True,
        "checked_at": now,
        "server_online": bool(live),  # dump 有 peer 行即接口存在
        "peers": items,
    }


def _format_handshake(handshake: int, now: int) -> str:
    if not handshake:
        return "从未握手"
    delta = now - handshake
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"
