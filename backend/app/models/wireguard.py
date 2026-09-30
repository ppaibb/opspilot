"""
WireGuard 虚拟组网表（网络 + 成员对端）。

安全约定：
1. 私钥一律 encrypt_secret 加密落库，任何列表/详情接口不回传明文；
   明文只出现在两个出口——配置文件下载、二维码（本质就是配置）；
2. 公钥/网段/IP 等非敏感字段明文存储便于展示与分配。
"""
from sqlalchemy import Column, Integer, String, Text, UniqueConstraint

from app.db.database import Base


class WireGuardNetwork(Base):
    """一个虚拟组网：对应一台出口服务器上的一个 wg 接口。"""

    __tablename__ = "wg_networks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="组网名称，如 生产-跨机房内网")
    cidr = Column(String(50), nullable=False, comment="隧道网段，如 10.8.0.0/24")
    # 出口节点：从 CMDB 资产选择，记录资产 ID 与连接信息快照。
    server_asset_id = Column(String(50), default="", comment="出口节点资产 instance_id")
    server_name = Column(String(100), default="", comment="出口节点名称快照")
    endpoint = Column(String(200), nullable=False, comment="对端可达地址（公网 IP 或域名）")
    port = Column(Integer, default=51820, comment="WireGuard 监听端口")
    # 服务端密钥：出口节点即 peer .1。
    server_private_key = Column(Text, default="", comment="服务端私钥（加密）")
    server_public_key = Column(String(100), default="", comment="服务端公钥（明文）")
    forward_iface = Column(String(30), default="eth0", comment="出口网卡名（MASQUERADE 用）")
    full_tunnel = Column(Integer, default=0, comment="成员是否全隧道（AllowedIPs=0.0.0.0/0）: 1/0")
    dns = Column(String(200), default="", comment="下发给成员的 DNS（可选）")
    remark = Column(String(500), default="", comment="备注")
    created_at = Column(String(50), comment="创建时间")


class WireGuardPeer(Base):
    """组网成员对端：一台成员主机（或手工登记的移动端/外部机器）。"""

    __tablename__ = "wg_peers"
    __table_args__ = (
        UniqueConstraint("network_id", "allowed_ip", name="uq_wg_peer_ip"),
    )

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(Integer, nullable=False, index=True, comment="所属组网")
    name = Column(String(100), nullable=False, comment="成员名称，如 上海节点-A")
    # 关联 CMDB 资产（可选）：手工登记的移动端等可留空。
    asset_id = Column(String(50), default="", comment="关联资产 instance_id（可选）")
    allowed_ip = Column(String(50), nullable=False, comment="隧道内 IP（含掩码位），如 10.8.0.2/32")
    private_key = Column(Text, default="", comment="成员私钥（加密）")
    public_key = Column(String(100), default="", comment="成员公钥（明文）")
    enabled = Column(Integer, default=1, comment="是否启用（禁用后配置标记移除）: 1/0")
    remark = Column(String(500), default="", comment="备注")
    created_at = Column(String(50), comment="创建时间")
