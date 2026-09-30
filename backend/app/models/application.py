"""应用中心数据模型。"""
from sqlalchemy import Column, DateTime, Integer, String

from app.db.database import Base
from app.models.json_fields import JsonListText


class Application(Base):
    """应用目录主表。

    物理表名沿用 applications，避免开发期反复迁移；产品语义上这里表示一个可发布、可配置的应用。
    所属项目不再单独建重表，一期直接使用 tags JSON 数组表达多项目归属。
    """

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    app_id = Column(String(50), unique=True, index=True, comment="应用业务ID")
    name = Column(String(120), index=True, comment="应用名称")
    description = Column(String(500), default="", comment="应用说明")
    category = Column(String(80), default="业务应用", comment="应用分类")
    owner = Column(String(80), default="", comment="负责人")
    status = Column(String(30), default="active", comment="active/maintenance/offline")
    environment = Column(String(30), default="prod", comment="prod/test/dev/mixed")
    run_mode = Column(String(30), default="external", comment="历史兼容字段；真实运行方式已下沉到 application_components.deployment_type")
    entry_type = Column(String(30), default="external", comment="external/internal/grafana/tool")
    entry_url = Column(String(500), default="", comment="应用入口地址")
    tags = Column(JsonListText(), default=list, comment="所属项目标签 JSON 数组")
    allowed_roles = Column(JsonListText(), default=list, comment="可见权限组 JSON 数组，空表示全部可见")
    deploy_roles = Column(JsonListText(), default=list, comment="发布权限组 JSON 数组，空表示继承可见范围")
    control_roles = Column(JsonListText(), default=list, comment="启停权限组 JSON 数组，空表示继承可见范围")
    config_roles = Column(JsonListText(), default=list, comment="配置权限组 JSON 数组，空表示继承可见范围")
    remark = Column(String(500), default="", comment="备注")
    created_at = Column(DateTime, nullable=True, comment="创建时间")
    updated_at = Column(DateTime, nullable=True, comment="更新时间")


class ApplicationResource(Base):
    """应用与已有资产资源的绑定快照。"""

    __tablename__ = "application_resources"

    id = Column(Integer, primary_key=True, index=True)
    app_id = Column(String(50), index=True, comment="应用业务ID")
    resource_type = Column(String(30), index=True, comment="host/vm/domain/certificate")
    resource_id = Column(String(80), index=True, comment="资源业务ID")
    resource_name = Column(String(255), default="", comment="资源展示名称快照")
    created_at = Column(DateTime, nullable=True, comment="绑定时间")


class ApplicationComponent(Base):
    """历史兼容的应用服务表。

    一期前端已隐藏服务层，保留该表仅用于兼容历史数据。
    该表只保存服务元数据和资源引用，不保存账号、密钥或配置明文。
    """

    __tablename__ = "application_components"

    id = Column(Integer, primary_key=True, index=True)
    component_id = Column(String(50), unique=True, index=True, comment="应用服务业务ID")
    app_id = Column(String(50), index=True, comment="所属应用业务ID")
    name = Column(String(120), index=True, comment="应用服务名称")
    component_type = Column(String(30), default="backend", comment="frontend/backend/gateway/worker/other")
    deployment_type = Column(String(30), default="binary", comment="binary/supervisor/docker/docker_compose/kubernetes_yaml/helm/static_site/external")
    control_method = Column(String(30), default="systemd", comment="systemd/supervisor/docker/docker_compose/kubectl_apply/helm/ansible/manual")
    control_ref = Column(String(500), default="", comment="运行标识，例如 supervisor program、systemd 服务名、compose 文件路径或 K8s namespace/deployment")
    execution_channel = Column(String(30), default="auto", comment="执行通道: auto/ansible/ssh_shell")
    runtime = Column(String(120), default="", comment="历史兼容字段；运行配置文件统一保存到 application_configs")
    owner = Column(String(80), default="", comment="服务负责人")
    env = Column(String(30), default="prod", comment="prod/test/dev/mixed")
    status = Column(String(30), default="active", comment="active/maintenance/offline")
    resource_refs = Column(JsonListText(), default=list, comment="关联应用资源键 JSON 数组，例如 host:i-xxx")
    description = Column(String(500), default="", comment="服务说明")
    last_control_action = Column(String(20), default="", comment="最近一次控制动作")
    last_control_result = Column(String(20), default="", comment="最近一次控制结果 success/failed")
    last_control_at = Column(DateTime, nullable=True, comment="最近一次控制时间")
    last_control_duration_ms = Column(Integer, default=0, comment="最近一次控制耗时毫秒")
    last_control_output = Column(String(4000), default="", comment="最近一次控制输出，已截断")
    created_at = Column(DateTime, nullable=True, comment="创建时间")
    updated_at = Column(DateTime, nullable=True, comment="更新时间")


class ApplicationLink(Base):
    """应用快捷入口链接。"""

    __tablename__ = "application_links"

    id = Column(Integer, primary_key=True, index=True)
    app_id = Column(String(50), index=True, comment="应用业务ID")
    title = Column(String(120), comment="链接标题")
    url = Column(String(500), comment="链接地址")
    link_type = Column(String(30), default="external", comment="external/internal/grafana/tool")
    created_at = Column(DateTime, nullable=True, comment="创建时间")


class ApplicationConfig(Base):
    """应用配置项。

    安全边界：
    1. 普通配置仅保存非敏感值；
    2. 敏感配置只保存 credential_ref，不保存凭证明文；
    3. 后端写入时必须校验当前用户是否有权使用该凭证。
    """

    __tablename__ = "application_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(String(50), unique=True, index=True, comment="配置业务ID")
    app_id = Column(String(50), index=True, comment="应用业务ID")
    env = Column(String(30), index=True, default="prod", comment="prod/test/dev/mixed")
    key = Column(String(120), index=True, comment="配置键")
    value = Column(String(20000), default="", comment="普通配置值或小型配置文件内容")
    value_type = Column(String(30), default="plain", comment="plain/credential_ref/file")
    credential_ref = Column(String(80), default="", comment="引用凭证ID")
    remark = Column(String(500), default="", comment="备注")
    created_at = Column(DateTime, nullable=True, comment="创建时间")
    updated_at = Column(DateTime, nullable=True, comment="更新时间")


class ApplicationConfigRevision(Base):
    """应用配置版本历史。

    安全边界：
    1. 普通配置保存变更前后的值快照；
    2. 凭证引用配置只保存凭证 ID，不保存凭证内容或表单中的敏感值；
    3. 写入历史与配置 CRUD 使用同一个事务，避免配置成功但审计缺失。
    """

    __tablename__ = "application_config_revisions"

    id = Column(Integer, primary_key=True, index=True)
    revision_id = Column(String(50), unique=True, index=True, comment="配置版本业务ID")
    app_id = Column(String(50), index=True, comment="应用业务ID")
    config_id = Column(String(50), index=True, comment="配置业务ID")
    env = Column(String(30), index=True, default="prod", comment="prod/test/dev/mixed")
    key = Column(String(120), index=True, comment="配置键")
    change_type = Column(String(30), index=True, default="update", comment="create/update/delete")
    value_type = Column(String(30), default="plain", comment="plain/credential_ref")
    old_value_snapshot = Column(String(2000), default="", comment="变更前普通值快照")
    new_value_snapshot = Column(String(2000), default="", comment="变更后普通值快照")
    old_credential_ref = Column(String(80), default="", comment="变更前凭证引用ID")
    new_credential_ref = Column(String(80), default="", comment="变更后凭证引用ID")
    operator = Column(String(80), default="", comment="操作人")
    remark = Column(String(500), default="", comment="变更备注")
    created_at = Column(DateTime, nullable=True, comment="创建时间")


class ApplicationRelease(Base):
    """应用发布记录。

    发布记录可以来自外部流水线，也可以由 OpsPilot 调用 Ansible 模板执行。
    本表只保存发布元数据、外部链接和截断后的输出摘要。
    """

    __tablename__ = "application_releases"

    id = Column(Integer, primary_key=True, index=True)
    release_id = Column(String(50), unique=True, index=True, comment="发布业务ID")
    app_id = Column(String(50), index=True, comment="应用业务ID")
    component_id = Column(String(50), index=True, default="", comment="应用服务业务ID")
    component_name = Column(String(120), default="", comment="应用服务名称快照")
    version = Column(String(120), index=True, comment="发布版本")
    env = Column(String(30), index=True, default="prod", comment="prod/test/dev/mixed")
    release_type = Column(String(30), index=True, default="external", comment="external/ansible/kubernetes/helm/rollback")
    external_url = Column(String(500), default="", comment="外部发布链接")
    template_id = Column(String(80), index=True, comment="Ansible 模板ID")
    template_name = Column(String(120), default="", comment="Ansible 模板名称快照")
    artifact_ref = Column(String(500), default="", comment="发布制品引用，例如 manifest 路径或 Helm chart")
    rollback_release_id = Column(String(50), default="", comment="回滚来源发布记录ID")
    target_resource_ids = Column(JsonListText(), default=list, comment="目标应用资源ID JSON数组")
    target_names = Column(JsonListText(), default=list, comment="目标主机名称快照 JSON数组")
    status = Column(String(30), index=True, default="pending", comment="pending/running/success/failed/canceled")
    operator = Column(String(80), default="", comment="操作人")
    started_at = Column(DateTime, nullable=True, comment="开始时间")
    finished_at = Column(DateTime, nullable=True, comment="结束时间")
    duration_ms = Column(Integer, default=0, comment="执行耗时毫秒")
    summary = Column(String(500), default="", comment="摘要")
    output = Column(String(4000), default="", comment="截断后的执行输出")


class ApplicationHealthSnapshot(Base):
    """应用健康快照（周期性聚合）。

    安全边界：
    1. 快照只保存状态字符串和若干计数，不保存任何凭证、告警原始 payload 或日志内容；
    2. 每条记录来自后台巡检，用户不能直接写入；
    3. 默认保留 7 天，超期数据在巡检器内清理。
    """

    __tablename__ = "application_health_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    app_id = Column(String(50), index=True, comment="应用业务ID")
    snapshot_time = Column(DateTime, index=True, comment="快照时间")
    health_status = Column(String(30), default="unknown", comment="healthy/warning/degraded/offline/unknown")
    health_label = Column(String(30), default="", comment="页面展示标签")
    health_reasons = Column(JsonListText(), default=list, comment="健康原因 JSON 数组")
    component_total = Column(Integer, default=0, comment="运行实例总数")
    component_active = Column(Integer, default=0, comment="运行实例活跃数")
    host_total = Column(Integer, default=0, comment="绑定主机总数")
    host_healthy = Column(Integer, default=0, comment="绑定主机健康数")
    active_alert_count = Column(Integer, default=0, comment="活动告警数")
    domain_expiring_count = Column(Integer, default=0, comment="即将到期域名数")
    certificate_expiring_count = Column(Integer, default=0, comment="即将到期证书数")
