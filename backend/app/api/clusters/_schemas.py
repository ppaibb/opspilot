"""Pydantic request/response models for cluster APIs."""

from typing import List, Optional

from pydantic import BaseModel, Field

class ClusterResponse(BaseModel):
    cluster_id: str
    name: str
    provider: str
    version: str
    node_count: int
    pod_capacity: int
    pod_usage: int
    cpu_cores: int
    mem_gb: int
    status: str
    kubeconfig_ref: str
    created_at: Optional[str] = ""

    class Config:
        from_attributes = True

class ClusterCreate(BaseModel):
    """纳管请求。

    version/水位数字一律可空：探测失败、用户不知道或不想填都不能挡住纳管
    （纳管后可用「刷新」重新探测回填，见 refresh 端点）。
    """
    name: str
    provider: str
    version: str = ""
    node_count: int = 0
    pod_capacity: int = 0
    pod_usage: int = 0
    cpu_cores: int = 0
    mem_gb: int = 0
    kubeconfig_ref: str = "managed"
    kubeconfig_yaml: Optional[str] = None  # 原始 YAML 用于物理落盘

class KubeScanRequest(BaseModel):
    kubeconfig_yaml: str

class ClusterUpdate(BaseModel):
    """集群信息编辑请求（对应列表页编辑抽屉）。"""

    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(default="", max_length=40)

class PatchNodeLabelsRequest(BaseModel):
    labels: dict[str, str | None] = Field(..., description="要设置的 labels，value 为 None 表示删除该 key")

class PatchNodeTaintsRequest(BaseModel):
    taints: list[dict] = Field(..., description="完整的 taints 数组（替换式）")

class NodeDrainRequest(BaseModel):
    """节点 drain 请求。

    默认最保守：不驱逐 emptyDir、不驱逐无控制器的裸 Pod。
    想要覆盖这两条必须显式传 True，对齐 `kubectl drain` 的 `--delete-emptydir-data`
    与 `--force`。
    """

    ignore_daemonsets: bool = True
    delete_emptydir_data: bool = False
    force: bool = False
    grace_period_seconds: int = Field(default=30, ge=0, le=600)

class YamlApplyRequest(BaseModel):
    """YAML 内容提交体，支持多文档（--- 分隔）。"""
    yaml_content: str

class ResourceContainerPatch(BaseModel):
    """资源表单编辑中的容器变更项。"""
    name: str
    image: str
    ports: Optional[List[dict]] = None
    env: Optional[List[dict]] = None

class ResourceServicePortPatch(BaseModel):
    """Service 表单编辑中的端口变更项。"""
    name: Optional[str] = ""
    port: int = Field(ge=1, le=65535)
    target_port: Optional[str] = ""
    protocol: str = "TCP"
    node_port: Optional[int] = Field(default=None, ge=1, le=65535)

class ResourceFormPatchRequest(BaseModel):
    """表单化资源编辑请求。

    安全边界：
    1. 不接收 arbitrary patch，不允许用户从表单传入任意 spec 字段。
    2. Secret data/stringData 永远不走该接口。
    3. 容器只允许按已有 name 修改 image，不允许新增容器或挂载。
    """
    labels: Optional[dict[str, str]] = None
    annotations: Optional[dict[str, str]] = None
    replicas: Optional[int] = Field(default=None, ge=0, le=999)
    containers: Optional[List[ResourceContainerPatch]] = None
    service_type: Optional[str] = None
    ports: Optional[List[ResourceServicePortPatch]] = None
    data: Optional[dict[str, str]] = None
    stringData: Optional[dict[str, str]] = None

class ScaleRequest(BaseModel):
    """Scale request body, replicas range 0-999."""
    replicas: int = Field(ge=0, le=999)

class CreateNamespaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")
    labels: dict[str, str] = Field(default_factory=dict)
    quota_preset: str | None = Field(None, description="small/medium/large/custom 或 None 不设置")
    custom_quota: dict[str, str] | None = Field(None, description="仅 preset=custom 时有效")

class NamespaceLabelsPatch(BaseModel):
    """命名空间标签变更：value 为 None 表示删除该 key（与节点标签同语义）。"""

    labels: dict[str, str | None] = Field(..., description="要设置的 labels")

class SetImageRequest(BaseModel):
    """更新镜像请求。

    安全边界：
    - 只允许覆盖已有容器的镜像，不允许改变容器名或新增容器；
    - 镜像字符串只做长度与基本字符过滤，不做签名校验——签名在 K8s ImagePolicyWebhook 侧做。
    """

    images: List[ResourceContainerPatch] = Field(default_factory=list)
    change_cause: str = ""

class RolloutUndoRequest(BaseModel):
    """回滚到指定 revision 的请求。不传 `revision` 等同回滚到上一次。"""

    revision: Optional[int] = None

