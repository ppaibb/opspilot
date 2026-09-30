"""监控告警模块 Pydantic 数据模型。

【分层设计说明】
将数据模型独立为 schemas.py，与 routes/services 分离，好处是：
1. 前后端契约显性化：前端 TypeScript 类型可对照本文件生成，减少接口不一致；
2. 避免循环导入：services 之间互相调用时，若模型嵌套在路由文件里极易产生 import cycle；
3. 单测可独立 import：测试数据构造无需拉起 FastAPI 依赖注入。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, AliasChoices

from app.api.monitoring.constants import (
    DEFAULT_NODE_EXPORTER_PORT,
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)


class MonitoringConfigResponse(BaseModel):
    vm_url: str
    vmui_url: str
    grafana_url: str
    vlogs_url: str
    vlogs_ingest_url: str
    remote_write_url: str = ""
    vlogsui_url: str
    auth_type: str
    username: str
    verify_tls: bool
    timeout_seconds: int
    default_step_seconds: int
    has_secret: bool
    # resolve_agent_write_urls 结果（只增不减，旧前端可忽略）
    agent_write_urls: dict[str, Any] | None = None


class MonitoringConfigUpdate(BaseModel):
    """增量更新：字段可省略，省略表示保持原值（只改一项时不必重填其余）。"""

    vm_url: str | None = None
    vmui_url: str = ""
    grafana_url: str = ""
    vlogs_url: str = ""
    vlogs_ingest_url: str = ""
    remote_write_url: str = ""
    vlogsui_url: str = ""
    auth_type: str = "none"
    username: str = ""
    secret: str = ""
    verify_tls: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    default_step_seconds: int = DEFAULT_STEP_SECONDS


class MonitoringCheckItem(BaseModel):
    name: str
    ok: bool
    message: str


class MonitoringSelfCheckResponse(BaseModel):
    overall_ok: bool
    checks: list[MonitoringCheckItem]
    hints: list[str]
    # resolve_agent_write_urls + 可达性探测（只增不减）
    agent_write_urls: dict[str, Any] | None = None


class TargetEnableRequest(BaseModel):
    port: int = DEFAULT_NODE_EXPORTER_PORT
    job: str = "opspilot-node"


class LogTargetEnableRequest(BaseModel):
    """主机日志采集纳管请求。"""

    profile: str = "linux-default"


class LogQueryRequest(BaseModel):
    query: str = ""
    start: str = ""
    end: str = ""
    limit: int = 200
    host_ips: list[str] = Field(default_factory=list)
    host_names: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


class LogSuggestRequest(BaseModel):
    """自然语言 → LogsQL 建议查询。"""

    text: str = Field(..., min_length=1, max_length=300)


class LogFieldsRequest(BaseModel):
    """标签/字段枚举（Grafana 式下拉筛选）。"""

    field: str = ""
    start: str = ""
    end: str = ""
    query: str = ""


class LogHistogramRequest(BaseModel):
    """日志直方图请求——按时间分桶统计日志数量。"""
    query: str = "*"
    start: str = ""
    end: str = ""
    step: str = "5m"
    host_ips: list[str] = Field(default_factory=list)


class LogContextRequest(BaseModel):
    """日志上下文请求——查看某条日志前后的行。"""
    stream_id: str = ""
    timestamp: str = ""
    before: int = 10
    after: int = 10


class MonitoringDeploymentAction(BaseModel):
    """监控套件部署动作，只允许后端白名单中的固定操作。"""

    action: str
    apply_config: bool = True


class NotificationChannelItem(BaseModel):
    """通知渠道条目，一期仅支持通用 Webhook。"""

    id: str = ""
    name: str
    type: str = "webhook"
    webhook_url: str
    enabled: bool = True
    send_resolved: bool = True
    template_id: str = ""


class NotificationTemplateItem(BaseModel):
    """通知模板条目，用于自定义消息渲染。"""

    id: str = ""
    name: str
    type: str = "webhook"
    trigger_type: str = "all"
    title_template: str = ""
    body_template: str = ""
    enabled: bool = True
    remark: str = ""


class NotificationTemplatesResponse(BaseModel):
    """通知模板读取响应。"""

    templates: list[NotificationTemplateItem]
    updated_at: str


class NotificationTemplatesUpdate(BaseModel):
    """通知模板更新请求。"""

    templates: list[NotificationTemplateItem]


class NotificationTemplatePreviewRequest(BaseModel):
    """通知模板预览请求。"""

    title_template: str
    body_template: str
    trigger_type: str = "firing"


class AlertingFileChangeItem(BaseModel):
    """告警配置文件写入结果。"""

    path: str
    changed: bool


class AlertingRuntimeReloadItem(BaseModel):
    """告警相关服务重载结果。"""

    attempted: bool
    ok: bool
    message: str
    stdout: str = ""
    stderr: str = ""


class AlertingSyncInfo(BaseModel):
    """规则/通知保存结果。

    内置告警引擎架构：规则/通知仅持久化到数据库，引擎按周期自动读取生效，
    不再有 alerts.yml 文件写入或 Alertmanager 重载动作。旧的文件同步字段
    （rule_file/notification_file/runtime_reload）保留为可选，仅供旧通知保存
    路径继续回填，新规则保存路径返回 engine/message。
    """

    updated_at: str = ""
    engine: str = ""
    message: str = ""
    rule_file: AlertingFileChangeItem | None = None
    notification_file: AlertingFileChangeItem | None = None
    runtime_reload: dict[str, AlertingRuntimeReloadItem] | None = None


class NotificationChannelsResponse(BaseModel):
    """通知渠道读取响应。"""

    channels: list[NotificationChannelItem]
    updated_at: str
    sync_info: AlertingSyncInfo | None = None


class NotificationChannelsUpdate(BaseModel):
    """通知渠道更新请求。"""

    channels: list[NotificationChannelItem]


class AlertRuleItem(BaseModel):
    """告警规则条目。

    字段契约与后端引擎一致：`name` + `duration`。
    同时兼容旧前端别名 `alert` / `for`（validation_alias），保存不再 422。
    """

    id: str = ""
    name: str = Field(default="", validation_alias=AliasChoices("name", "alert"))
    expr: str
    duration: str = Field(default="5m", validation_alias=AliasChoices("duration", "for"))
    severity: str = "warning"
    enabled: bool = True
    channel_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    description: str = ""
    recovery_debounce_seconds: int = 60
    reminder_interval_minutes: int = 30
    max_reminders_cap: int = 0
    host_ids: list[str] = Field(default_factory=list)
    group: str = ""
    im_user_ids: list[int] = Field(default_factory=list)
    updated_at: str = ""

    model_config = ConfigDict(populate_by_name=True)


class MonitoringRulesResponse(BaseModel):
    """告警规则读取响应。"""

    rules: list[AlertRuleItem]
    updated_at: str
    sync_info: AlertingSyncInfo | None = None


class MonitoringRulesUpdate(BaseModel):
    """告警规则更新请求。"""

    rules: list[AlertRuleItem]


class AlertRuleTemplateItem(BaseModel):
    """告警规则模板条目（来自内置模板库，不可修改）。"""

    id: str
    category: str
    name: str
    expr: str
    duration: str = "5m"
    severity: str = "warning"
    enabled: bool = False
    channel_ids: list[str] = []
    summary: str
    description: str
    required_exporter: str = ""


class AlertRuleTemplatesResponse(BaseModel):
    """告警规则模板列表响应。"""

    templates: list[AlertRuleTemplateItem]


class ApplyTemplatesRequest(BaseModel):
    """从模板库批量创建告警规则的请求。"""

    template_ids: list[str]
