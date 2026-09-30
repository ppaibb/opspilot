"""WireGuard 组网核心服务：密钥生成、IP 分配、配置渲染、二维码。

设计要点：
1. 密钥用 X25519（WireGuard 即 Curve25519），直接用项目已有的 cryptography 库，
   不依赖 wg 二进制——服务端无需安装任何额外工具即可生成配置；
2. IP 分配基于 ipaddress 标准库计算网段，落库前由 API 层查重；
3. 二维码用 segno（纯 Python、自带 PNG 编码器、无 PIL 依赖），输出 data URL。
"""
from __future__ import annotations

import base64
import ipaddress
from typing import Any

from app.core.crypto import decrypt_secret, encrypt_secret

# WireGuard 密钥是 32 字节 curve25519 原始值的 base64（44 字符、= 结尾）。
WG_KEY_LENGTH = 44


# ───────────────────────── 密钥 ─────────────────────────

def generate_keypair() -> tuple[str, str]:
    """生成一对 WireGuard 密钥，返回 (私钥, 公钥) 的 base64 文本。

    X25519 私钥是 32 字节随机数；公钥为私钥的标量基点乘法结果，
    与 `wg genkey | wg pubkey` 完全等价。
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private = X25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(private_bytes).decode(),
        base64.b64encode(public_bytes).decode(),
    )


def is_valid_private_key(value: str) -> bool:
    """校验 base64 私钥格式（32 字节解码通过即认为合法）。"""
    if not value or len(value) != WG_KEY_LENGTH:
        return False
    try:
        raw = base64.b64decode(value)
        return len(raw) == 32
    except Exception:  # noqa: BLE001
        return False


# ───────────────────────── IP 分配 ─────────────────────────

def network_hosts(cidr: str) -> list[str]:
    """返回网段内全部可用主机地址（含掩码位），如 10.8.0.1/24, 10.8.0.2/24 ...。

    约定：第一个地址（.1）保留给出口节点；广播/网络地址由 ipaddress 自动排除。
    """
    network = ipaddress.ip_network(cidr, strict=False)
    prefix = network.prefixlen
    return [f"{host}/{prefix}" for host in network.hosts()]


def allocate_peer_ip(cidr: str, taken_ips: list[str]) -> str | None:
    """从网段中分配下一个空闲地址；跳过 .1（出口节点保留）与已占用地址。

    返回带掩码位的地址（/32 语义由配置渲染时处理为 /prefix）。
    分配耗尽返回 None。
    """
    taken = set(taken_ips)
    for candidate in network_hosts(cidr):
        if candidate in taken:
            continue
        ip = ipaddress.ip_interface(candidate).ip
        # .1 固定给出口节点。
        if ip == ipaddress.ip_network(cidr, strict=False).network_address + 1:
            continue
        return candidate
    return None


def first_host_ip(cidr: str) -> str:
    """出口节点地址（网段第一个可用主机位）。"""
    return network_hosts(cidr)[0]


# ───────────────────────── 配置渲染 ─────────────────────────

def render_server_config(network: Any, peers: list[Any]) -> str:
    """渲染出口节点 wg0.conf。

    只包含启用状态的成员（禁用成员的 [Peer] 段被移除，保留记录以便恢复）。
    """
    private_key = decrypt_secret(network.server_private_key or "")
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {first_host_ip(network.cidr)}",
        f"ListenPort = {network.port}",
        # 转发与 NAT：成员间互访 + 出公网（全隧道场景必需）。
        f"PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o {network.forward_iface or 'eth0'} -j MASQUERADE",
        f"PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o {network.forward_iface or 'eth0'} -j MASQUERADE",
        "SaveConfig = false",
        "",
    ]
    for peer in peers:
        if not peer.enabled:
            continue
        lines += [
            "# " + (peer.name or peer.allowed_ip),
            "[Peer]",
            f"PublicKey = {peer.public_key}",
            f"AllowedIPs = {peer.allowed_ip}",
            "",
        ]
    return "\n".join(lines)


def render_peer_config(network: Any, peer: Any) -> str:
    """渲染成员配置文件。

    AllowedIPs 语义：全隧道 = 0.0.0.0/0（全部流量走隧道）；否则只路由隧道网段（分流）。
    """
    private_key = decrypt_secret(peer.private_key or "")
    cidr = ipaddress.ip_network(network.cidr, strict=False)
    allowed_ip = ipaddress.ip_interface(peer.allowed_ip).ip
    allowed_ips = "0.0.0.0/0, ::/0" if network.full_tunnel else str(cidr)
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {allowed_ip}/{cidr.prefixlen}",
    ]
    if network.dns:
        lines.append(f"DNS = {network.dns}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {network.server_public_key}",
        f"Endpoint = {network.endpoint}:{network.port}",
        f"AllowedIPs = {allowed_ips}",
        # 移动端/ NAT 后机器需要保活以维持隧道。
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines)


# ───────────────────────── 部署脚本 ─────────────────────────

def render_server_install_script(network: Any, server_config: str, iface: str = "wg0") -> str:
    """生成出口节点的一键安装脚本（Debian/Ubuntu 系，内嵌配置）。

    用户可复制到「运维工具 → 脚本执行」以 shell 模块跑，或手工在服务器执行。
    """
    return "\n".join([
        "#!/usr/bin/env bash",
        f"# WireGuard 出口节点安装脚本 — 组网: {network.name}",
        "# 适用 Debian/Ubuntu；CentOS 请将 apt 换成 yum/dnf。",
        "set -euo pipefail",
        "",
        "apt-get update -qq && apt-get install -y -qq wireguard iptables >/dev/null",
        f"umask 077 && mkdir -p /etc/wireguard",
        f"cat > /etc/wireguard/{iface}.conf <<'WGCONF_EOF'",
        server_config,
        "WGCONF_EOF",
        f"chmod 600 /etc/wireguard/{iface}.conf",
        f"systemctl enable --now wg-quick@{iface}",
        f"systemctl status wg-quick@{iface} --no-pager | head -5",
        f"echo ' 完成。查看隧道: wg show'",
    ])


def render_peer_install_script(network: Any, peer: Any, peer_config: str, iface: str = "wg0") -> str:
    """生成成员主机的一键安装脚本。"""
    return "\n".join([
        "#!/usr/bin/env bash",
        f"# WireGuard 成员安装脚本 — 组网: {network.name} / 成员: {peer.name}",
        "set -euo pipefail",
        "",
        "apt-get update -qq && apt-get install -y -qq wireguard >/dev/null",
        f"umask 077 && mkdir -p /etc/wireguard",
        f"cat > /etc/wireguard/{iface}.conf <<'WGCONF_EOF'",
        peer_config,
        "WGCONF_EOF",
        f"chmod 600 /etc/wireguard/{iface}.conf",
        f"systemctl enable --now wg-quick@{iface}",
        f"echo ' 完成。查看隧道: wg show；出网测试: ping {first_host_ip(network.cidr).split('/')[0]}'",
    ])


# ───────────────────────── 二维码 ─────────────────────────

def peer_qr_data_url(peer_config: str) -> str:
    """成员配置转 PNG data URL（segno 纯 Python 编码，无 PIL 依赖）。"""
    import segno

    qr = segno.make(peer_config, error="l")
    return qr.png_data_uri(scale=6, border=2)


# ───────────────────────── 序列化 ─────────────────────────

def serialize_network(network: Any, peer_count: int = 0) -> dict[str, Any]:
    """网络列表/详情输出（不含任何私钥）。"""
    return {
        "id": network.id,
        "name": network.name,
        "cidr": network.cidr,
        "server_asset_id": network.server_asset_id or "",
        "server_name": network.server_name or "",
        "endpoint": network.endpoint,
        "port": network.port,
        "server_public_key": network.server_public_key or "",
        "forward_iface": network.forward_iface or "eth0",
        "full_tunnel": bool(network.full_tunnel),
        "dns": network.dns or "",
        "remark": network.remark or "",
        "created_at": network.created_at or "",
        "peer_count": peer_count,
    }


def serialize_peer(peer: Any) -> dict[str, Any]:
    """成员输出（不含私钥）。"""
    return {
        "id": peer.id,
        "network_id": peer.network_id,
        "name": peer.name,
        "asset_id": peer.asset_id or "",
        "allowed_ip": peer.allowed_ip,
        "public_key": peer.public_key or "",
        "enabled": bool(peer.enabled),
        "remark": peer.remark or "",
        "created_at": peer.created_at or "",
    }


# 加密便利函数：模型落库前的统一入口（保持调用侧可读）。
def seal_key(private_key: str) -> str:
    return encrypt_secret(private_key)
