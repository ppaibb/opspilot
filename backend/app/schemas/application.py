"""应用中心接口的请求与响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApplicationPayload(BaseModel):
    """创建/更新应用目录的请求体。"""

    name: str
    description: str = ""
    category: str = "业务应用"
    owner: str = ""
    status: str = "active"
    environment: str = "prod"
    # 兼容历史客户端字段。产品语义上运行方式已经下沉到运行实例，
    # 应用登记不再允许选择 supervisor/systemd/docker/compose 等具体管控方式。
    run_mode: str = "external"
    entry_type: str = "external"
    entry_url: str = ""
    tags: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    deploy_roles: list[str] = Field(default_factory=list)
    control_roles: list[str] = Field(default_factory=list)
    config_roles: list[str] = Field(default_factory=list)
    remark: str = ""


class ApplicationResponse(ApplicationPayload):
    """应用列表返回结构。"""

    app_id: str
    health_status: str = "unknown"
    health_label: str = "未纳管"
    health_reasons: list[str] = Field(default_factory=list)
    resource_count: int = 0
    component_count: int = 0
    link_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class ApplicationResourcePayload(BaseModel):
    """应用绑定资源请求。"""

    resource_type: str
    resource_id: str


class ApplicationResourceResponse(ApplicationResourcePayload):
    """应用绑定资源响应。"""

    id: int
    resource_name: str
    created_at: str


from typing import Optional

class ComponentDeployConfig(BaseModel):
    image_ref: Optional[str] = None
    docker_run_args: Optional[str] = None
    compose_content: Optional[str] = None
    artifact_url: Optional[str] = None


class ApplicationComponentPayload(BaseModel):
    """应用服务创建/更新请求。

    resource_refs 只能引用当前项目已经绑定的资源，格式为 `资源类型:资源ID`。
    deployment_type/control_ref 只保存部署元数据和运行标识，不保存密钥。
    control_method 由后端根据 deployment_type 派生，保留字段仅用于兼容历史数据。
    """

    name: str
    component_type: str = "backend"
    deployment_type: str = "binary"
    control_method: str = ""
    control_ref: str = ""
    execution_channel: str = "auto"
    runtime: str = ""
    owner: str = ""
    env: str = "prod"
    status: str = "active"
    resource_refs: list[str] = Field(default_factory=list)
    description: str = ""
    create_mode: Optional[str] = "manage"
    deploy_config: Optional[ComponentDeployConfig] = None
    # Compose 文件内容不落到 application_components 表，后端会镜像到配置中心。
    compose_content: str = ""
    compose_env_filename: str = ".env"
    compose_env_content: str = ""
    pull_image: bool = True


class ApplicationComponentResponse(ApplicationComponentPayload):
    """应用服务响应。"""

    component_id: str
    last_control_action: str = ""
    last_control_result: str = ""
    last_control_at: str = ""
    last_control_duration_ms: int = 0
    last_control_output: str = ""
    created_at: str = ""
    updated_at: str = ""


class ApplicationComponentBatchDeleteRequest(BaseModel):
    component_ids: list[str] = Field(min_length=1, max_length=2000)


class ApplicationComposePreflightRequest(BaseModel):
    resource_id: str
    project_name: str
    deploy_dir: str
    compose_filename: str = "docker-compose.yml"
    compose_content: str
    env_filename: str = ".env"
    env_content: str = ""


class ApplicationComposePreflightResponse(BaseModel):
    ok: bool
    checks: list[dict[str, str]] = Field(default_factory=list)
    compose_file: str
    cli: str = ""
    stdout: str = ""
    stderr: str = ""


# 产品语义上这张历史表现在表达“运行实例”。保留旧类名和旧接口兼容历史调用，
# 同时导出运行实例别名，避免后续代码继续依赖“应用服务”这个模糊概念。
ApplicationRuntimeInstancePayload = ApplicationComponentPayload
ApplicationRuntimeInstanceResponse = ApplicationComponentResponse


class ApplicationRuntimeActionPayload(BaseModel):
    """运行实例控制请求。

    只允许传递动作，不允许传递命令；实际命令由后端白名单生成。
    """

    action: str = Field(default="status", max_length=16)


class ApplicationRuntimeActionResponse(BaseModel):
    """运行实例控制响应。"""

    ok: bool
    action: str
    runtime_type: str
    target_count: int
    duration_ms: int
    code: int
    execution_channel: str = "auto"
    stdout: str
    stderr: str
    command_preview: str
    notice: str


class ApplicationRuntimeDiscoveryRequest(BaseModel):
    """运行实例扫描请求。

    只允许选择当前应用已绑定主机和固定扫描类型，不接收任意命令。
    """

    asset_ids: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=lambda: ["supervisor", "docker", "docker_compose"])
    include_system_services: bool = False


class ApplicationRuntimeDiscoveryCandidate(BaseModel):
    """运行实例扫描候选。"""

    asset_id: str
    asset_name: str
    deployment_type: str
    name: str
    control_ref: str
    status: str
    description: str = ""
    project_name: str = ""
    management_status: str = "new"
    managed_component_id: str = ""


class ApplicationRuntimeDiscoveryResponse(BaseModel):
    """运行实例扫描响应。"""

    scanned_count: int
    candidates: list[ApplicationRuntimeDiscoveryCandidate]
    errors: list[str] = Field(default_factory=list)
    filtered_count: int = 0


class ApplicationLinkPayload(BaseModel):
    """应用入口链接请求。"""

    title: str
    url: str
    link_type: str = "external"


class ApplicationLinkResponse(ApplicationLinkPayload):
    """应用入口链接响应。"""

    id: int
    created_at: str


class ApplicationObservabilityContext(BaseModel):
    """应用观测上下文。

    该结构只包含过滤条件，不包含 VictoriaMetrics / VictoriaLogs 内网地址。
    """

    app_id: str
    app_name: str
    host_ids: list[str] = Field(default_factory=list)
    host_ips: list[str] = Field(default_factory=list)
    host_names: list[str] = Field(default_factory=list)
    runtime_instances: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    certificate_ids: list[str] = Field(default_factory=list)


class ApplicationDetailResponse(BaseModel):
    """应用详情响应。"""

    app: ApplicationResponse
    resources: list[ApplicationResourceResponse]
    components: list[ApplicationComponentResponse]
    links: list[ApplicationLinkResponse]
    observability: ApplicationObservabilityContext
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ResourceOption(BaseModel):
    """应用可绑定资源选项。"""

    resource_type: str
    resource_id: str
    name: str
    meta: str = ""


class RoleOption(BaseModel):
    """应用权限角色选项。"""

    role: str
    title: str
    description: str = ""


class AnsibleTemplateOption(BaseModel):
    """应用发布可选批量模板。"""

    template_id: str
    kind: str = "ansible"
    name: str
    description: str = ""
    module: str = ""


class CredentialRefOption(BaseModel):
    """应用配置可引用凭证选项。"""

    cred_id: str
    name: str
    type: str


class ApplicationConfigPayload(BaseModel):
    """应用配置创建/更新请求。"""

    env: str = "prod"
    key: str
    value_type: str = "plain"
    value: str = ""
    credential_ref: str = ""
    remark: str = ""


class ApplicationConfigImportPayload(BaseModel):
    """.env / KV 批量导入请求。

    安全边界：
    - 只导入纯文本值，凭证必须经过凭证中心，后续人工改绑；
    - `allow_sensitive=False` 时，疑似敏感键（PASSWORD/SECRET/TOKEN/KEY 结尾）会被拒绝导入；
    - `overwrite=False` 时，同键不覆盖现有配置，只作为 skipped 返回。
    """

    env: str = "prod"
    content: str = ""
    overwrite: bool = False
    allow_sensitive: bool = False
    remark: str = ""


class ApplicationConfigImportItemResult(BaseModel):
    """单行导入结果。"""

    key: str
    status: str  # created / updated / skipped
    reason: str = ""


class ApplicationConfigImportResponse(BaseModel):
    """.env 批量导入响应。"""

    total_lines: int
    parsed: int
    imported: list[ApplicationConfigImportItemResult] = Field(default_factory=list)


class ApplicationConfigResponse(ApplicationConfigPayload):
    """应用配置响应。"""

    config_id: str
    app_id: str
    app_name: str = ""
    credential_name: str = ""
    created_at: str = ""
    updated_at: str = ""


class ApplicationConfigRevisionResponse(BaseModel):
    """应用配置版本历史响应。

    凭证引用类型只返回 credential_ref 快照，不返回任何凭证明文。
    """

    revision_id: str
    app_id: str
    config_id: str
    env: str
    key: str
    change_type: str
    value_type: str
    old_value_snapshot: str = ""
    new_value_snapshot: str = ""
    old_credential_ref: str = ""
    new_credential_ref: str = ""
    operator: str = ""
    remark: str = ""
    created_at: str = ""


class ApplicationReleasePayload(BaseModel):
    """应用发布请求。"""

    version: str
    env: str = "prod"
    component_id: str = ""
    release_type: str = "external"
    status: str = "success"
    external_url: str = ""
    template_id: str = ""
    artifact_ref: str = ""
    artifact_filename: str = ""
    artifact_content_b64: str = ""
    rollback_release_id: str = ""
    target_resource_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    # Docker Compose 标准部署字段：前端只提交 compose 文件和受限部署参数，
    # 后端会生成固定 Playbook，避免把发布入口变成任意 shell 执行器。
    deploy_dir: str = ""
    compose_filename: str = "docker-compose.yml"
    compose_content: str = ""
    service_name: str = ""
    pull_image: bool = True
    # Docker 单容器发布字段。这里接收的是受后端白名单限制的 docker run 参数，
    # 不是 shell 命令；镜像地址仍放在 artifact_ref，便于发布记录统一展示。
    docker_run_args: str = ""
    # Supervisor 进程托管发布字段。command 会写入 supervisor program 配置，
    # 后端会限制危险 shell 元字符；environment 使用多行 KEY=VALUE。
    supervisor_runtime: str = "system"
    supervisor_command: str = ""
    supervisor_user: str = ""
    supervisor_environment: str = ""


class ApplicationReleaseResponse(BaseModel):
    """应用发布记录响应。"""

    release_id: str
    app_id: str
    app_name: str = ""
    component_id: str = ""
    component_name: str = ""
    version: str
    env: str
    release_type: str = "external"
    external_url: str = ""
    template_id: str
    template_name: str = ""
    artifact_ref: str = ""
    rollback_release_id: str = ""
    target_resource_ids: list[str] = Field(default_factory=list)
    target_names: list[str] = Field(default_factory=list)
    status: str
    operator: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    summary: str = ""
    output: str = ""


class ApplicationReleasePreviewTarget(BaseModel):
    """发布预检的目标主机。"""

    resource_id: str
    name: str
    ip: str = ""


class ApplicationReleasePreviewCheck(BaseModel):
    """发布预检的单项检查。

    level:
    - `error`：阻断问题，真实发布时会被后端拒绝；
    - `warning`：软警告，用户需关注但可以继续；
    - `info`：展示性信息，例如命令摘要、凭证状态。
    """

    level: str
    message: str


class ApplicationReleasePreviewResponse(BaseModel):
    """发布预检响应。

    说明：预检只做只读计划，不会下发任何远程命令。
    """

    release_type: str
    allowed: bool
    targets: list[ApplicationReleasePreviewTarget] = Field(default_factory=list)
    command_preview: list[str] = Field(default_factory=list)
    checks: list[ApplicationReleasePreviewCheck] = Field(default_factory=list)


class ApplicationOverviewResponse(BaseModel):
    """项目运维工作台总览响应。"""

    app: ApplicationResponse
    resource_stats: dict[str, int]
    host_stats: dict[str, int]
    vm_stats: dict[str, int]
    expiry_stats: dict[str, int]
    recent_releases: list[ApplicationReleaseResponse]
    recent_configs: list[ApplicationConfigResponse]


class ApplicationTimelineEvent(BaseModel):
    """应用时间线单条事件。

    所有字段均来自当前应用已有的只读数据源；不触发 SSH、HTTP 探测或用户自定义查询。
    """

    id: str
    category: str  # release / config / runtime_control / alert
    event_type: str
    time: str = ""
    title: str
    summary: str = ""
    severity: str = "info"  # info / success / warning / error
    operator: str = ""
    entity_id: str = ""
    entity_name: str = ""


class ApplicationTimelineResponse(BaseModel):
    """应用时间线响应。"""

    app_id: str
    app_name: str
    events: list[ApplicationTimelineEvent] = Field(default_factory=list)
    truncated: bool = False


class ApplicationHealthSnapshotItem(BaseModel):
    """单条应用健康快照。"""

    snapshot_time: str
    health_status: str
    health_label: str
    health_reasons: list[str] = Field(default_factory=list)
    component_total: int = 0
    component_active: int = 0
    host_total: int = 0
    host_healthy: int = 0
    active_alert_count: int = 0
    domain_expiring_count: int = 0
    certificate_expiring_count: int = 0


class ApplicationHealthTimelineResponse(BaseModel):
    """应用健康时间带响应。"""

    app_id: str
    app_name: str
    hours: int
    now: str
    snapshots: list[ApplicationHealthSnapshotItem] = Field(default_factory=list)


__all__ = [
    "AnsibleTemplateOption",
    "ApplicationComponentPayload",
    "ApplicationComponentResponse",
    "ApplicationComponentBatchDeleteRequest",
    "ApplicationComposePreflightRequest",
    "ApplicationComposePreflightResponse",
    "ApplicationConfigImportItemResult",
    "ApplicationConfigImportPayload",
    "ApplicationConfigImportResponse",
    "ApplicationConfigPayload",
    "ApplicationConfigResponse",
    "ApplicationConfigRevisionResponse",
    "ApplicationDetailResponse",
    "ApplicationHealthSnapshotItem",
    "ApplicationHealthTimelineResponse",
    "ApplicationLinkPayload",
    "ApplicationLinkResponse",
    "ApplicationObservabilityContext",
    "ApplicationOverviewResponse",
    "ApplicationPayload",
    "ApplicationReleasePayload",
    "ApplicationReleasePreviewCheck",
    "ApplicationReleasePreviewResponse",
    "ApplicationReleasePreviewTarget",
    "ApplicationReleaseResponse",
    "ApplicationResourcePayload",
    "ApplicationResourceResponse",
    "ApplicationResponse",
    "ApplicationRuntimeActionPayload",
    "ApplicationRuntimeActionResponse",
    "ApplicationRuntimeDiscoveryCandidate",
    "ApplicationRuntimeDiscoveryRequest",
    "ApplicationRuntimeDiscoveryResponse",
    "ApplicationRuntimeInstancePayload",
    "ApplicationRuntimeInstanceResponse",
    "CredentialRefOption",
    "ResourceOption",
    "RoleOption",
]
