"""虚拟机管理接口的请求与响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VMDiskItem(BaseModel):
    """虚机磁盘展示项，只包含前端需要的安全字段。"""

    key: str
    label: str
    size_gb: float


class VMResponse(BaseModel):
    """虚拟机列表和详情的标准响应结构。"""

    id: str
    name: str
    provider: str
    node: str
    status: str
    ip: str
    os: str
    cpu_cores: float
    memory_gb: float
    system_disk_gb: float
    disks: list[VMDiskItem] = Field(default_factory=list)
    description: str


class VMCreate(BaseModel):
    """创建或编辑虚机时使用的输入模型。"""

    name: str
    provider: str = "proxmox"
    node: str = ""
    status: str | None = None
    ip: str = ""
    os: str = ""
    cpu_cores: float = 0.0
    memory_gb: float = 0.0
    system_disk_gb: float = 0.0
    description: str = ""
    vmid: int | None = None
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    template_vmid: int | None = None
    full_clone: bool = True
    target_storage: str = ""
    start_after_create: bool = False
    boot_iso: str = ""


class VMCloneRequest(BaseModel):
    """独立虚拟机克隆请求。"""

    name: str
    full_clone: bool = True
    target_node: str = ""
    target_storage: str = ""
    start_after_clone: bool = False


class VMPowerRequest(BaseModel):
    """虚机电源操作请求。"""

    action: str


class VMBatchDeleteRequest(BaseModel):
    """虚机批量删除请求。"""

    ids: list[str]


class VMNodeItem(BaseModel):
    """PVE 节点下拉项。"""

    node: str
    status: str


class VMStorageItem(BaseModel):
    """PVE 存储下拉项。"""

    storage: str
    node: str
    storage_type: str
    content: str
    active: bool
    enabled: bool
    shared: bool


class VMTemplateItem(BaseModel):
    """PVE 模板虚机下拉项。"""

    vmid: int
    name: str
    node: str
    status: str


class VMIsoItem(BaseModel):
    """PVE ISO 镜像下拉项。"""

    volid: str
    storage: str
    node: str
    file_name: str
    size_mb: float
    ctime: int


class VMBridgeItem(BaseModel):
    """PVE 网桥下拉项。"""

    bridge: str
    node: str
    active: bool
    autostart: bool
    comments: str


class VMSelfCheckItem(BaseModel):
    """虚机模块自检结果。"""

    name: str
    ok: bool
    message: str


class VMSyncJobResponse(BaseModel):
    """异步同步任务进度响应。"""

    job_id: str
    status: str
    current_step: str
    progress: int
    message: str
    started_at: int
    finished_at: int
    result: dict[str, Any]


class VMHostManageRequest(BaseModel):
    """虚机纳管为主机资产的最小输入。

    虚机页面只负责把平台侧 VM 转换为主机资产；SSH 验证、Web 终端、
    监控和日志 Agent 纳管继续由主机管理页面承载。
    """

    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str = ""
    credential_ref: str = ""
    group: str = "虚拟机纳管"
    remark: str = ""
