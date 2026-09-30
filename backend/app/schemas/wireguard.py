"""WireGuard 组网接口的请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class NetworkCreate(BaseModel):
    """创建组网。"""

    name: str = Field(min_length=1, max_length=100)
    cidr: str = Field(min_length=9, max_length=50, description="隧道网段，如 10.8.0.0/24")
    server_asset_id: str = ""
    server_name: str = ""
    endpoint: str = Field(min_length=3, max_length=200, description="对端可达地址")
    port: int = Field(default=51820, ge=1024, le=65535)
    forward_iface: str = "eth0"
    full_tunnel: bool = False
    dns: str = ""
    remark: str = ""


class NetworkUpdate(BaseModel):
    """编辑组网（网段/密钥不可改——改网段等于重建组网）。"""

    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=3, max_length=200)
    port: int = Field(ge=1024, le=65535)
    forward_iface: str = "eth0"
    full_tunnel: bool = False
    dns: str = ""
    remark: str = ""


class PeerCreate(BaseModel):
    """添加成员：名称必填，可关联 CMDB 资产；IP 留空自动分配。"""

    name: str = Field(min_length=1, max_length=100)
    asset_id: str = ""
    allowed_ip: str = Field(default="", max_length=50, description="留空自动分配")
    remark: str = ""


class PeerUpdate(BaseModel):
    """成员编辑（启停 / 备注）。"""

    enabled: bool | None = None
    remark: str | None = None
