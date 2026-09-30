// ── 通用包装（后端可能直接返回数据或 {code,data,message}，这里按实际情况适配） ──
export interface ApiResponse<T = unknown> {
  code?: number
  data: T
  message?: string
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
}

// ── 用户管理 ──
export interface UserResponse {
  id: number
  username: string
  name: string
  email: string
  role: string
  department: string
  status: string
  last_login: string
  mfa_enabled: boolean
  permission_count: number
  access_summary: string[]
}

export interface UserCreateRequest {
  username: string
  password: string
  name?: string
  email?: string
  role?: string
  department?: string
  status?: string
}

export interface UserAccessUpdate {
  role: string
  effective_permissions: Record<string, boolean>
}

export interface UserAccessResponse {
  user_id: number
  username: string
  name: string
  role: string
  email: string
  department: string
  role_permissions: Record<string, boolean>
  permission_overrides: Record<string, boolean>
  effective_permissions: Record<string, boolean>
}

// ── 权限/RBAC ──
export interface PermissionCatalogItem {
  key: string
  group: string
  title: string
  description: string
  path: string
}

export interface RoleTemplateItem {
  role: string
  title: string
  description: string
  permissions: Record<string, boolean>
  is_system?: boolean
  editable?: boolean
  deletable?: boolean
}

export interface RoleTemplateCreateRequest {
  role: string
  description?: string
  permissions: Record<string, boolean>
}

export interface RoleTemplateUpdateRequest {
  description?: string
  permissions: Record<string, boolean>
}

// ── 审计日志 ──
export interface AuditLogResponse {
  id: number
  time: string
  user: string
  action: string
  resource: string
  ip: string
  result: string
  risk_level?: string
  risk_reason?: string
}

// ── 登录 ──
export interface LoginRequest {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  user: {
    id: number
    username: string
    role: string
    [key: string]: unknown
  }
}

// ── 用户 ──
export interface UserInfo {
  id: number
  username: string
  name: string
  email: string
  role: string
  department: string
  permission_count: number
  access_summary: string[]
}

// ── 资产 ──
export interface AssetResponse {
  id: string
  name: string
  ip: string
  hostname?: string
  type: string
  provider: string
  region: string
  os: string
  cpu: number
  uptime: string
  remark?: string
  status: string[]
  ssh_port: number
  ssh_user: string
  credential_ref: string
  cpu_cores: number
  memory_gb: number
  system_disk_gb: number
  disks?: Array<{ key: string; label: string; size_gb: number }>
  group_id?: number | null
  health_last_checked_at?: string
  config_task_status?: string
  config_task_updated_at?: string
  config_task_error?: string
}

export interface AssetCreate {
  name: string
  ip: string
  /** 可选：留空默认 on-premise；localhost 启用内置采集 */
  provider?: string
  region?: string
  group_id?: number | null
  remark?: string
  os?: string
  ssh_port?: number
  ssh_user?: string
  ssh_password?: string
  credential_ref?: string
  cpu_cores?: number
  memory_gb?: number
  system_disk_gb?: number
}

// ── 分组 ──
export interface GroupResponse {
  id: number
  name: string
  color: string
  description: string
  sort_order: number
  member_count: number
}

export interface GroupCreate {
  name: string
  color?: string
  description?: string
}

// ── 集群 ──
export interface ClusterResponse {
  cluster_id: string
  name: string
  provider: string
  version: string
  node_count: number
  pod_capacity: number
  pod_usage: number
  cpu_cores: number
  mem_gb: number
  status: string
  kubeconfig_ref: string
  created_at?: string
}

export interface ClusterCreate {
  name: string
  provider: string
  version: string
  node_count: number
  pod_capacity: number
  pod_usage: number
  cpu_cores: number
  mem_gb: number
  kubeconfig_ref: string
  kubeconfig_yaml: string
}

// ── 虚拟机 ──
export interface VMResponse {
  id: string
  name: string
  provider: string
  node: string
  status: string
  ip: string
  os: string
  cpu_cores: number
  memory_gb: number
  system_disk_gb: number
  description: string
  disks?: Array<{ key: string; label: string; size_gb: number }>
}

export interface VMCreate {
  name: string
  provider?: string
  node?: string
  status?: string | null
  ip?: string
  os?: string
  cpu_cores?: number
  memory_gb?: number
  system_disk_gb?: number
  description?: string
  vmid?: number | null
  storage?: string
  bridge?: string
  template_vmid?: number | null
  full_clone?: boolean
  /** 模板克隆时克隆盘落盘的存储池（后端 target_storage） */
  target_storage?: string
  start_after_create?: boolean
  boot_iso?: string
}

/** PVE 节点下拉项（GET /vms/nodes） */
export interface VMNodeItem {
  node: string
  status: string
}

/** PVE 存储池下拉项（GET /vms/storages） */
export interface VMStorageItem {
  storage: string
  node: string
  storage_type: string
  content: string
  active: boolean
  enabled: boolean
  shared: boolean
}

/** PVE ISO 镜像下拉项（GET /vms/isos） */
export interface VMIsoItem {
  volid: string
  storage: string
  node: string
  file_name: string
  size_mb: number
  ctime: number
}

/** PVE 网桥下拉项（GET /vms/bridges） */
export interface VMBridgeItem {
  bridge: string
  node: string
  active: boolean
  autostart: boolean
  comments: string
}

/** PVE QEMU 模板下拉项（GET /vms/templates） */
export interface VMTemplateItem {
  vmid: number
  name: string
  node: string
  status: string
}

/** 批量删除虚机响应（部分失败场景后端逐条返回原因） */
export interface VMBatchDeleteResult {
  success_count: number
  failed: Array<{ id: string; reason: string }>
}

// ── 凭证 ──
/** 凭证类型取值必须与后端 _build_export_payload / is_long_lived_key_type 的判断保持一致 */
export type CredentialType = 'Password' | 'SSH Key' | 'Token' | 'AccessKey'

export interface CredentialResponse {
  cred_id: string
  name: string
  type: CredentialType
  scope?: string
  allowed_roles?: string[]
  created_at?: string
  expires_at?: string
  status?: string
  used_by?: number
  // 敏感内容（密码/私钥/Token/AK-SK）后端永不回传，只在写入时提交
}

/**
 * 新增凭证。后端只接受单个加密 content 字段：
 * - Password / Token：明文原样存入
 * - SSH Key：PEM 私钥原文
 * - AccessKey：JSON 字符串 {"access_key":"...","secret_key":"..."}
 */
export interface CredentialCreate {
  name: string
  type: CredentialType
  content: string
  /** YYYY-MM-DD；留空串 = 长期有效（后端 status 恒 active，列表显示「长期有效」） */
  expires_at: string
  allowed_roles?: string[]
  scope?: string
}

/** 编辑凭证：content 留空表示不改动密文，需配合 replace_content=true 才会替换 */
export interface CredentialUpdate {
  name: string
  type: CredentialType
  expires_at: string
  content?: string
  replace_content?: boolean
  allowed_roles?: string[]
  scope?: string
}

// ── 域名 ──
export interface DomainResponse {
  id: number
  domain: string
  display_domain?: string
  root_domain: string
  record_type: string
  record_value: string
  provider: string
  provider_label?: string
  status: string
  account_id?: string
  account_name?: string
  created_at?: string
  updated_at?: string
  expires_at?: string
  expire_reminder_days?: number
  remark?: string
  acme_enabled?: string
  cert_targets?: string
  records_count?: number
}

export interface DomainCreate {
  domain: string
  dns_credential?: string
  root_domain?: string
  record_type?: string
  record_value?: string
  provider?: string
  status?: string
  account_id?: string
  account_name?: string
  expires_at?: string
  expire_reminder_days?: number
  remark?: string
  acme_enabled?: boolean
  cert_targets?: string
}

export interface DomainUpdate {
  remark?: string
  expires_at?: string
  expire_reminder_days?: number
  status?: string
  acme_enabled?: boolean
  cert_targets?: string
}

export interface DomainDNSRecordResponse {
  id: number
  domain_id: number
  host: string
  record_type: string
  record_value: string
  ttl: number
  remark?: string
  updated_at?: string
}

export interface DomainDNSRecordCreate {
  host?: string
  record_type?: string
  record_value: string
  ttl?: number
  remark?: string
}

export interface DomainProviderItem {
  key: string
  label: string
  supports_acme: boolean
  supports_cert_distribution: boolean
}

export interface DomainAccountItem {
  account_id: string
  account_name: string
  provider_key: string
  provider_label: string
  credential_type: string
}

// ── 证书 ──
export interface CertificateResponse {
  cert_id: string
  id?: string
  name: string
  primary_domain: string
  san_domains: string[]
  provider: string
  provider_label?: string
  account_id?: string
  account_name?: string
  acme_server?: string
  challenge_type?: string
  key_type?: string
  status: string
  auto_renew: boolean
  renew_before_days?: number
  not_before?: string
  not_after?: string
  fingerprint_sha256?: string
  cert_path?: string
  key_path?: string
  last_issue_at?: string
  last_renew_at?: string
  last_error?: string
  deploy_targets?: string
  remark?: string
  created_at?: string
  updated_at?: string
}

export interface CertificateIssueRequest {
  name?: string
  primary_domain: string
  san_domains?: string[]
  account_id: string
  acme_server?: string
  key_type?: string
  auto_renew?: boolean
  renew_before_days?: number
  remark?: string
  deploy_targets?: string
}

export interface CertificateUploadRequest {
  name?: string
  cert_content: string
  key_content: string
  remark?: string
}

export interface CertificateUpdateRequest {
  name?: string
  san_domains?: string[]
  key_type?: string
  auto_renew?: boolean
  renew_before_days?: number
  remark?: string
  deploy_targets?: string
}

export interface CertificateDeployRequest {
  /** 部署目标：主机 SSH / 云服务 CDN·ALB / K8s TLS Secret。 */
  target_type: 'host' | 'cloud' | 'k8s'
  asset_ids?: string[]
  /** 前端表单旧别名，与 asset_ids 等价 */
  host_ids?: string[]
  cert_path?: string
  key_path?: string
  reload_cmd?: string
  cert_dest_path?: string
  key_dest_path?: string
  post_script?: string
  cloud_provider?: string
  cloud_account_id?: string
  /** k8s 目标（target_type='k8s'）的集群与 Secret 参数。 */
  cluster_id?: string
  namespace?: string
  secret_name?: string
  /** 云服务目标的凭证与产品参数。 */
  cloud_credential?: string
  cloud_service?: string
  domain_instance?: string
  /** 表单附带的其他兼容字段（旧字段别名等）由后端按需读取。 */
  [key: string]: unknown
}

export interface CertificateAccountItem {
  account_id: string
  account_name: string
  provider_key: string
  provider_label: string
  credential_type: string
}

// ── AI Agent ──
export interface AISessionItem {
  session_id: string
  title: string
  agent_id: string
  message_count: number
  created_at: string
  updated_at: string
}

export interface AIMessageMeta {
  provider?: string
  model?: string
  used_skills?: string[]
  suggestions?: string[]
  context_blocks?: Array<{
    key: string
    title: string
    summary: string
    items?: unknown[]
  }>
  notice?: string
  mode?: string
}

export interface AIMessageItem {
  id: number | string
  role: 'user' | 'assistant'
  content: string
  meta?: AIMessageMeta
  created_at?: string
}

export interface AISessionDetail extends AISessionItem {
  messages: AIMessageItem[]
}

export interface AISkillItem {
  key: string
  title: string
  description: string
  category: string
  risk_level: string
  enabled: boolean
  sample_queries: string[]
}

export interface LLMConfigItem {
  id: string
  name: string
  provider: 'gemini' | 'openai_compatible' | 'anthropic'
  base_url: string
  model: string
  /** 与后端 LLMConfigItemResponse 对齐 */
  is_active: boolean
  /** @deprecated 历史字段，后端已改用 is_active */
  active?: boolean
}

// ── 系统管理 ──
export interface ScheduledTaskStatus {
  key: string
  name: string
  description: string
  interval_seconds: number
  started_at: string
  last_run_at: string
  next_run_at: string
  last_status: 'pending' | 'ok' | 'error'
  last_error: string
  last_duration_ms: number | null
  run_count: number
  error_count: number
  enabled: boolean
  runtime_state: 'running' | 'disabled' | 'stopped'
  default_interval_seconds: number
  min_interval_seconds: number
  max_interval_seconds: number
  startup_delay_seconds: number
  config_updated_at: string
}

export interface NotificationChannel {
  id?: string
  name: string
  type: string
  webhook_url: string
  enabled?: boolean
  send_resolved?: boolean
  template_id?: string
}

// ── 个人 IM 渠道（飞书群 @ 通知） ──
export interface ImBinding {
  /** 是否已绑定 */
  bound: boolean
  platform: string
  /** 解析用的手机号或邮箱 */
  identity: string
  /** 脱敏后的飞书 open_id */
  open_id_masked: string
  /** group_at：群内 @ 本人；off：关闭 */
  notify_mode: string
  /** 个人群自定义机器人地址，空表示用系统默认 */
  webhook_url: string
  bound_at: string
}

export interface FeishuImConfig {
  app_id: string
  /** App Secret 只回是否已配置，不回明文 */
  app_secret_set: boolean
  /** 系统默认群机器人（未填个人地址时使用） */
  default_webhook_url: string
}

export interface ImUser {
  id: number
  username: string
  name: string
  /** 是否已绑定飞书且开启群 @ */
  im_bound: boolean
}

export interface FeatureToggles { [key: string]: boolean }

export interface LoginPageConfig {
  hero_title: string
  hero_subtitle: string
  background_image_url: string
  sso_enabled: boolean
  sso_button_text: string
  sso_hint: string
  updated_at?: string
}

export interface MonitoringConfig {
  /** 后端 → VictoriaMetrics（分体部署如 http://192.168.3.99:8428） */
  vm_url: string
  vmui_url?: string
  grafana_url?: string
  /** 后端 → VictoriaLogs */
  vlogs_url?: string
  /** 采集 Agent → 存储（可空，空则按 vm_url/vlogs_url 推导） */
  vlogs_ingest_url?: string
  remote_write_url?: string
  vlogsui_url?: string
  auth_type?: string
  username?: string
  verify_tls?: boolean
  timeout_seconds?: number
  default_step_seconds?: number
  has_secret?: boolean
  agent_write_urls?: AgentWriteUrls
}

// ── 站内通知 ──
export interface NotificationApiItem {
  id: string
  type: 'success' | 'error' | 'warning' | 'info'
  category?: string
  title: string
  content?: string
  link?: string | null
  resource_id?: string | null
  is_read: boolean
  created_at: string
}

export interface NotificationListResponse {
  items: NotificationApiItem[]
  total: number
  unread_count: number
}

// ── 仪表盘 ──
export interface DashboardOverview {
  [key: string]: unknown
}

// ── 分页通用 ──
export interface PageParams {
  page?: number
  pageSize?: number
}

// ── 主机监控指标（抽屉） ──
export interface HostMetricPoint {
  time: string
  cpu?: number | null
  memory?: number | null
  memTotal?: number | null
  memAvailable?: number | null
  networkIn?: number | null
  networkOut?: number | null
  /** 1 分钟平均负载 */
  load1?: number | null
  /** 磁盘读吞吐 MB/s */
  diskRead?: number | null
  /** 磁盘写吞吐 MB/s */
  diskWrite?: number | null
  [key: string]: string | number | null | undefined
}

export interface HostMetricsResponse {
  configured: boolean
  message?: string
  host_name?: string
  metrics: HostMetricPoint[]
  disk_partitions?: string[]
}

// ── 监控 / 日志采集 target 状态（列表灯） ──
export interface MonitoringTargetItem {
  asset_id: string
  name: string
  ip: string
  enabled: boolean
  status: string
  last_checked_at?: string
  last_error?: string
  /** 指标是否在采（Agent 心跳/策略下发） */
  metrics_enabled?: boolean
  last_metric_at?: string
  /** 日志是否在采（Docker 日志策略是否成功下发） */
  logs_enabled?: boolean
  last_log_at?: string
}

export interface MonitoringTargetsResponse {
  targets: MonitoringTargetItem[]
  enabled_count: number
  total: number
}

export interface LogTargetItem {
  asset_id: string
  name: string
  ip: string
  provider?: string
  enabled: boolean
  status: string
  last_checked_at?: string
  last_error?: string
  metrics_enabled?: boolean
  last_metric_at?: string
  /** 以「是否成功下发」为准的日志在采状态 */
  logs_enabled?: boolean
  last_log_at?: string
}

export interface LogTargetsResponse {
  targets: LogTargetItem[]
}

/** 历史告警条目（GET /monitoring/alerts/history）。 */
export interface HistoryAlertItem {
  id?: number | null
  name: string
  severity: string
  instance: string
  started_at: string
  resolved_at?: string
  status: string
  summary?: string
  group?: string
}

/** 静默规则（GET /monitoring/alerts/silence）。 */
export interface SilenceRuleItem {
  id: string
  alert_name: string
  instance: string
  starts_at: string
  ends_at: string
  created_by: string
  comment: string
  status: string
}

// ── 告警规则（GET/PUT /monitoring/rules） ──
// 契约与后端 schemas.AlertRuleItem 一致：name/duration。
// 旧别名 alert/for 后端也接受，但前端一律发 name/duration。
export interface AlertRuleItem {
  id?: string
  /** 规则名称（Prometheus alert 名称） */
  name: string
  /** PromQL 触发表达式 */
  expr: string
  /** 持续时间（如 "5m"） */
  duration: string
  severity: string
  summary?: string
  description?: string
  enabled: boolean
  /** 绑定通知渠道 ID */
  channel_ids?: string[]
  /** 绑定主机资产 instance_id */
  host_ids?: string[]
  group?: string
  recovery_debounce_seconds?: number
  reminder_interval_minutes?: number
  max_reminders_cap?: number
  /** 飞书通知人（告警触发/恢复时在群里 @） */
  im_user_ids?: number[]
  updated_at?: string
}

export interface AlertRulesResponse {
  rules: AlertRuleItem[]
  updated_at?: string
}

export interface RuleTestResponse {
  ok: boolean
  error?: string
  expr: string
  match_count: number
  series: Array<{ instance: string; labels: Record<string, string>; value: string }>
}

export interface LinkHealthCheck {
  key: string
  name: string
  lamp: 'green' | 'yellow' | 'red'
  message: string
  last_at?: string
}

export interface AgentWriteUrlStatus {
  name: string
  url: string
  ok: boolean
  message: string
  hint?: string
}

export interface AgentWriteUrls {
  remote_write_url?: string
  vlogs_ingest_url?: string
  remote_write?: AgentWriteUrlStatus
  vlogs_ingest?: AgentWriteUrlStatus
}

export interface LinkHealthResponse {
  overall: 'green' | 'yellow' | 'red'
  checks: LinkHealthCheck[]
  engine?: Record<string, unknown>
  agent_write_urls?: AgentWriteUrls
}

export interface GrafanaSsoLink {
  url: string
  expires_at: string
}

// ── RBAC ──
export interface PermissionItem {
  key: string
  group: string
  title: string
  description: string
  path: string
}

export interface RBACMetadata {
  permissions: PermissionItem[]
  role_templates: Array<{
    role: string
    title: string
    description: string
    permissions: Record<string, boolean>
    is_system: boolean
  }>
}

// ── 风险等级 ──
export type RiskLevels = Record<string, 'high' | 'medium' | 'low'>

/** GET /auth/risk-levels 的响应信封：{"levels": {...}} */
export interface RiskLevelsResponse {
  levels: RiskLevels
}

// ── 高危确认票据（POST /auth/risk-challenge） ──
export interface RiskChallengeRequest {
  /** 短键形式的风险动作（如 "clusters:delete"），与 permission_key/action_id 二选一 */
  action_key?: string
  /** RBAC 权限键（如 "action.assets.credentials.export"） */
  permission_key?: string
  /** 同一权限键下的动作标识（如 "export"） */
  action_id?: string
  /** 可选：操作目标资源 ID */
  resource_id?: string
}

export interface RiskChallengeResponse {
  /** 一次性票据，随高危请求以 X-Risk-Ticket 头发送 */
  ticket: string
  /** 过期时间，后端返回 "YYYY-MM-DD HH:MM:SS" 格式字符串 */
  expires_at: string
  risk_level: 'high' | 'medium' | 'low'
}

// ── 通知 SSE 短时效票据（POST /notifications/stream/ticket） ──
export interface NotificationSseTicketResponse {
  ticket: string
  /** 票据有效期（秒） */
  expires_in: number
}

// ── 通知消息模板（GET/PUT /monitoring/notifications/templates） ──
export interface NotificationTemplateItem {
  id?: string
  name?: string
  title_template?: string
  body_template?: string
  [key: string]: unknown
}

// ── Ingress TLS 状态（GET /clusters/{id}/ingress/{ns}/{name}/tls-status） ──
export interface IngressTlsStatusItem {
  status?: string
  secret_name?: string
  days_remaining?: number
  hosts?: unknown[]
}

// ── 云同步 ──
export interface CloudSyncRequest {
  provider: 'aliyun' | 'tencent' | 'aws'
  region: string
  credential_id?: string
  access_key?: string
  secret_key?: string
  instance_ids?: string[] | null
}

export interface CloudInstanceItem {
  instance_id: string
  name: string
  ip: string
  private_ip: string
  status: string
  provider: string
  region: string
  cloud_resource_type: string
  os: string
  cpu_cores: number
  memory_gb: number
  system_disk_gb: number
  is_imported: boolean
}

export interface CloudSyncPreviewResponse {
  success: boolean
  preview: boolean
  instances: CloudInstanceItem[]
}

export interface CloudSyncImportResponse {
  success: boolean
  message: string
}

export interface CloudRegionsResponse {
  success: boolean
  regions: string[]
  message?: string
}

// ── 应用管理 ──
export interface ApplicationResponse {
  app_id: string
  name: string
  description: string
  category: string
  owner: string
  status: string
  environment: string
  entry_url: string
  tags: string[]
  health_status: string
  health_label: string
  health_reasons: string[]
  resource_count: number
  component_count: number
  link_count: number
  created_at: string
  updated_at: string
  remark: string
}

export interface ApplicationPayload {
  name: string
  description?: string
  category?: string
  owner?: string
  status?: string
  environment?: string
  entry_url?: string
  tags?: string[]
  remark?: string
}

export interface ApplicationResourceItem {
  id: number
  resource_type: string
  resource_id: string
  resource_name: string
  created_at: string
}

export interface ApplicationComponentResponse {
  component_id: string
  name: string
  component_type: string
  deployment_type: string
  control_method: string
  control_ref: string
  execution_channel: string
  runtime: string
  owner: string
  env: string
  status: string
  resource_refs: string[]
  description: string
  last_control_action: string
  last_control_result: string
  last_control_at: string
  last_control_output: string
  created_at: string
  updated_at: string
}

export interface ApplicationLinkItem {
  id: number
  title: string
  url: string
  link_type: string
  created_at: string
}

export interface ApplicationDetailResponse {
  app: ApplicationResponse
  resources: ApplicationResourceItem[]
  components: ApplicationComponentResponse[]
  links: ApplicationLinkItem[]
}

export interface ApplicationDependenciesResponse {
  app_id: string
  app_name: string
  resources: Array<{ resource_type: string; resource_id: string; name: string }>
  components: Array<{ component_id: string; name: string; deployment_type: string; resource_refs: string[] }>
  release_count: number
}

export interface ApplicationOverviewResponse {
  app: ApplicationResponse
  resource_stats: Record<string, number>
  host_stats: { total: number; online: number; offline: number }
  vm_stats: { total: number; running: number; stopped: number }
  expiry_stats: { domain_expiring: number; certificate_expiring: number; active_alerts: number }
  recent_releases: Array<{
    release_id: string
    release_type: string
    status: string
    created_by: string
    created_at: string
    message: string
  }>
  recent_configs: Array<{
    config_id: string
    key: string
    value_type: string
    updated_at: string
  }>
}

export interface ApplicationRuntimeDiscoveryCandidate {
  asset_id: string
  asset_name: string
  deployment_type: string
  name: string
  control_ref: string
  status: string
  description: string
  project_name: string
  management_status: 'new' | 'managed'
  managed_component_id: string
}

export interface ApplicationRuntimeDiscoveryResponse {
  scanned_count: number
  candidates: ApplicationRuntimeDiscoveryCandidate[]
  errors: string[]
  filtered_count: number
}

export interface ApplicationComponentPayload {
  name: string
  component_type?: string
  deployment_type: string
  control_ref: string
  execution_channel?: string
  runtime?: string
  owner?: string
  env?: string
  status?: string
  resource_refs?: string[]
  description?: string
  create_mode?: 'manage' | 'deploy'
  deploy_config?: {
    image_ref?: string
    docker_run_args?: string
    compose_content?: string
  }
  compose_content?: string
  compose_env_filename?: string
  compose_env_content?: string
  pull_image?: boolean
}

export interface ApplicationComposePreflightRequest {
  resource_id: string
  project_name: string
  deploy_dir: string
  compose_filename: string
  compose_content: string
  env_filename: string
  env_content: string
}

export interface ApplicationComposePreflightResponse {
  ok: boolean
  checks: Array<{ key: string; label: string; status: 'success' | 'warning' | 'failed'; message: string }>
  compose_file: string
  cli: string
  stdout: string
  stderr: string
}

export interface ApplicationRuntimeActionResponse {
  ok: boolean
  action: string
  runtime_type: string
  target_count: number
  duration_ms: number
  code: number
  execution_channel: string
  stdout: string
  stderr: string
  command_preview: string
  notice: string
}

// ── 应用配置管理 ──
export interface ApplicationConfigResponse {
  config_id: string
  app_id: string
  key: string
  value: string
  value_type: 'plain' | 'credential_ref'
  credential_id?: string
  credential_name?: string
  env: string
  description: string
  created_at: string
  updated_at: string
  updated_by: string
}

export interface ApplicationConfigPayload {
  key: string
  value?: string
  value_type: 'plain' | 'credential_ref'
  credential_id?: string
  env?: string
  description?: string
}

export interface ApplicationConfigRevisionResponse {
  revision_id: string
  config_id: string
  key: string
  old_value: string
  new_value: string
  old_value_type: string
  new_value_type: string
  old_credential_id?: string
  new_credential_id?: string
  change_type: string
  created_at: string
  created_by: string
  remark: string
}

export interface ApplicationConfigImportPayload {
  content: string
  env?: string
  conflict_mode?: 'skip' | 'overwrite' | 'ask'
}

export interface ApplicationConfigImportResponse {
  imported_count: number
  skipped_count: number
  overwritten_count: number
  conflicts: Array<{
    key: string
    existing_value: string
    new_value: string
    action: 'skip' | 'overwrite'
  }>
}

export interface CredentialRefOption {
  cred_id: string
  name: string
  type: string
}

// ── 应用部署管理 ──
export interface ApplicationReleaseResponse {
  release_id: string
  app_id: string
  app_name: string
  component_id: string
  component_name: string
  version: string
  env: string
  release_type: string
  external_url: string
  template_id: string
  template_name: string
  artifact_ref: string
  rollback_release_id: string
  target_resource_ids: string[]
  target_names: string[]
  status: string
  operator: string
  started_at: string
  finished_at: string
  duration_ms: number
  summary: string
  output: string
}

export interface ApplicationReleasePayload {
  component_id?: string
  version: string
  env?: string
  release_type?: string
  template_id?: string
  artifact_ref?: string
  rollback_release_id?: string
  target_resource_ids?: string[]
  summary?: string
}

export interface AnsibleTemplateOption {
  template_id: string
  kind: string
  name: string
  description: string
  module: string
}

// ── Finance 费用管理 ──

export interface CloudAccountItem {
  id: number
  name: string
  provider: string
  credential_ref: string
  credential_name: string
  vendor_account_id: string
  status: 'pending' | 'active' | 'error' | string
  status_message: string
  last_sync_at: string
  enabled: boolean
  created_at: string
}

export interface CloudAccountPayload {
  name: string
  provider?: string
  credential_ref: string
  enabled?: boolean
}

export interface CredentialOption {
  cred_id: string
  name: string
  type: string
}

export interface BillSyncRunItem {
  id: number
  account_id: number
  billing_cycle: string
  status: 'running' | 'success' | 'failed' | string
  trigger: 'manual' | 'scheduled' | string
  rows_synced: number
  error_message: string
  started_at: string
  finished_at: string
}

export interface FinanceDimensionItem {
  name: string
  amount: number
  ratio: number
}

export interface FinanceTrendPoint {
  period: string
  amount: number
}

export interface FinanceOverview {
  month: string
  total: number
  prev_total: number
  mom_delta: number
  by_account: FinanceDimensionItem[]
  by_product: FinanceDimensionItem[]
  by_subscription: FinanceDimensionItem[]
  daily_trend: FinanceTrendPoint[]
  monthly_trend: FinanceTrendPoint[]
  last_synced_at: string
}

export interface CloudBillItem {
  id: number
  account_id: number
  account_name: string
  billing_cycle: string
  billing_date: string
  provider: string
  region: string
  product_code: string
  product_name: string
  product_detail: string
  subscription_type: string
  billing_item_code: string
  billing_item_name: string
  instance_id: string
  resource_name: string
  resource_group: string
  cost_unit: string
  pretax_amount: number
  payment_amount: number
  cash_amount: number
  deducted_by_coupons: number
  currency: string
}

export interface CloudBillListResponse {
  items: CloudBillItem[]
  total: number
  page: number
  page_size: number
  grand_total: number
}

export interface CloudBillGroupResponse {
  items: FinanceDimensionItem[]
}

// ── AI 平台扩展（模型配置 / 编排 / 观测） ──

export interface LLMConfigDetail {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  temperature: number
  max_output_tokens: number
  timeout_seconds: number
  system_prompt: string
  has_api_key: boolean
  using_env_fallback: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface LLMConfigListResponse {
  items: LLMConfigDetail[]
  active_id: string
}

export interface LLMConfigPayload {
  name: string
  provider: string
  base_url?: string
  model: string
  api_key?: string
  temperature?: number
  max_output_tokens?: number
  timeout_seconds?: number
  system_prompt?: string
  activate?: boolean
}

export interface LLMConfigTestResult {
  ok: boolean
  provider: string
  base_url: string
  model: string
  latency_ms: number
  detail: string
  checked_at: string
}

export interface SkillRegistryResponse {
  skills: AISkillItem[]
  updated_at: string
}

export interface OrchestrationPolicy {
  approval_enabled: boolean
  budget_guard_enabled: boolean
  max_budget_cost_index: number
  timeout_seconds: number
  max_steps: number
  required_approvers: number
  ticket_required: boolean
  high_risk_actions: string[]
  updated_at: string
}

export interface OrchestrationPolicyPayload {
  approval_enabled: boolean
  budget_guard_enabled: boolean
  max_budget_cost_index: number
  timeout_seconds: number
  max_steps: number
  required_approvers: number
  ticket_required: boolean
}

export interface OrchestrationPreviewPayload {
  operation_name: string
  estimated_cost_index?: number
  expected_steps?: number
  expected_duration_seconds?: number
  action_keys?: string[]
  has_ticket?: boolean
  requester?: string
}

export interface OrchestrationPreviewResult {
  allowed: boolean
  requires_approval: boolean
  reasons: string[]
  matched_high_risk_actions: string[]
  policy: OrchestrationPolicy
}

export interface AIObservabilityStats {
  total_sessions: number
  total_messages: number
  today_messages: number
  active_users: number
  user_messages: number
  assistant_messages: number
  chat_trend_7d: number[]
}

export interface AIAuditLogItem {
  id: string
  time: string
  user: string
  agent: string
  intent: string
  risk: string
  status: string
}

// ── 脚本执行（Ansible） ──

export interface AnsibleConfig {
  enabled: boolean
  executable: string
  forks: number
  timeout_seconds: number
  host_key_checking: boolean
  ansible_available: boolean
  ansible_version: string
  last_error: string
  updated_at: string
}

export interface AnsibleConfigPayload {
  enabled: boolean
  executable: string
  forks: number
  timeout_seconds: number
  host_key_checking: boolean
}

export interface AnsibleTarget {
  asset_id: string
  name: string
  ip: string
  ssh_user: string
  ssh_port: number
  provider: string
  region: string
  online: boolean
  auth_mode: string
  ansible_ready: boolean
  auth_notice: string
}

export interface AnsibleRunResult {
  ok: boolean
  run_id: string
  module: string
  target_count: number
  duration_ms: number
  code: number
  stdout: string
  stderr: string
  command_preview: string
  notice: string
}

export interface AnsibleHistoryEntry {
  run_id: string
  created_at: string
  user: string
  module: string
  target_count: number
  duration_ms: number
  ok: boolean
  code: number
  stdout: string
  stderr: string
  command_preview: string
  notice: string
}

export interface AnsibleTemplateItem {
  template_id: string
  kind: string
  name: string
  description: string
  module: string
  module_args: string
  become: boolean
  become_user: string
  run_all: boolean
  asset_ids: string[]
  allowed_roles: string[]
  created_at: string
  updated_at: string
}

export interface AnsibleTemplatePayload {
  kind?: string
  name: string
  description?: string
  module?: string
  module_args?: string
  become?: boolean
  become_user?: string
  run_all?: boolean
  asset_ids?: string[]
  allowed_roles?: string[]
}

export interface AnsibleRoleOption {
  role: string
  title: string
  description: string
}

// ── 预算与实例费用联动 ──

export interface BudgetConfig {
  monthly_budget: number
  warn_ratio: number
  critical_ratio: number
  enabled: boolean
  updated_at: string
  month: string
  spent: number
  usage_ratio: number
}

export interface BudgetPayload {
  monthly_budget: number
  warn_ratio: number
  critical_ratio: number
  enabled: boolean
}

export interface InstanceCostPoint {
  month: string
  amount: number
}

export interface InstanceCostSummary {
  instance_id: string
  current_month: string
  current_month_amount: number
  previous_month_amount: number
  monthly: InstanceCostPoint[]
  by_billing_item: FinanceDimensionItem[]
  matched: boolean
}

// ── 实时大盘（/monitoring/dashboard 全局聚合） ──

export interface LiveDashboardFileSystemRow {
  instance?: string
  host?: string
  mountpoint?: string
  device?: string
  fstype?: string
  used_pct?: number | null
  size_bytes?: number | null
  used_bytes?: number | null
}

export interface LiveDashboardRecentAlert {
  name?: string
  severity?: string
  instance?: string
  time?: string
  status?: string
  id?: string
}

export interface MonitoringDashboardResponse {
  configured: boolean
  message?: string
  metrics: Array<Record<string, number | string | null>>
  services: Array<{ ip?: string; name?: string; status?: string; provider?: string; region?: string }>
  alerts: Array<Record<string, unknown>>
  top_hosts: Array<{ ip?: string; name?: string; cpu?: number; memory?: number } & Record<string, unknown>>
  /** 挂载点占用率 Top5（只增不减） */
  top_filesystems?: LiveDashboardFileSystemRow[]
  /** 最近告警切片（只增不减） */
  recent_alerts?: LiveDashboardRecentAlert[]
  summary: {
    targets: number
    up: number
    down: number
    alerts: number
    /** 全局 avg(node_load1)，查询失败为 null */
    load1?: number | null
  }
  range: { minutes: number; step_seconds: number }
}

// ── K8s 集群详情（节点 / 工作负载 / Pod / 通用资源） ──

export interface K8sNode {
  name: string
  ip: string
  role: string
  status: string
  unschedulable: boolean
  diagnosis: string
  cpu_usage: number
  mem_usage: number
  age: string
  /** parse_k8s_cpu 解析后的核数（float）；历史版本可能是 quantity 字符串 */
  cpu_capacity: string | number
  /** parse_k8s_memory_to_gib 解析后的 GiB（float）；历史版本可能是 quantity 字符串 */
  mem_capacity: string | number
}

export interface K8sNodeDrainPreview {
  node: string
  pods: Array<{ name: string; namespace: string; controller?: string; reason?: string }>
  total: number
  [key: string]: unknown
}

export interface K8sWorkload {
  kind: string
  namespace: string
  name: string
  ready: string
  status: string
  diagnosis: string
  restarts: number
  age: string
  image: string
}

export interface K8sPod {
  name: string
  namespace: string
  status: string
  ready: string
  restarts: number
  diagnosis: string
  node: string
  age: string
  image: string
}

export interface K8sResourceItem {
  name: string
  namespace: string
  kind: string
  scope: string
  status: string
  detail: string
  age: string
  image?: string
  ready?: string
}

export interface K8sResourceListResponse {
  items: K8sResourceItem[]
  total?: number
  continue_token?: string
  [key: string]: unknown
}

export interface K8sResourceEvent {
  type: string
  reason: string
  message: string
  count: number
  last_timestamp: string
  object?: string
}

/**
 * 工作负载滚动发布状态（GET /clusters/{cid}/workloads/.../rollout-status）。
 * 后端 _rollout_status_summary 的真实字段，进度按 desired/ready 副本数计算。
 */
export interface K8sRolloutStatus {
  kind?: string
  namespace?: string
  name?: string
  generation?: number
  observed_generation?: number
  progressing?: boolean
  desired_replicas?: number
  ready_replicas?: number
  updated_replicas?: number
  available_replicas?: number
  unavailable_replicas?: number
  /** progressing / completed / stopped */
  rollout_status?: string
  rollout_message?: string
  [key: string]: unknown
}

/** 工作负载发布历史条目（GET /clusters/{cid}/workloads/.../rollout-history）。 */
export interface K8sRolloutRevision {
  revision?: number
  image?: string
  change_cause?: string
  [key: string]: unknown
}

/** CronJob 执行记录（GET /clusters/{cid}/cronjobs/{ns}/{name}/jobs）。 */
export interface K8sCronJobRun {
  name?: string
  namespace?: string
  status?: string
  ready?: string
  age?: string
  start_time?: string
  [key: string]: unknown
}

/** 节点详情（GET /clusters/{cid}/nodes/{name}）：含标签/污点/Pod 分布，非关键字段以后端实际返回为准。 */
export interface K8sNodeDetail {
  labels?: Record<string, string>
  taints?: Array<{ key?: string; value?: string; effect?: string }>
  pods?: Array<{ name?: string; namespace?: string; status?: string }>
  pod_distribution?: Array<{ name?: string; namespace?: string; status?: string }>
  [key: string]: unknown
}

/** 跨资源搜索命中项（GET /clusters/{cid}/search）。 */
export interface K8sSearchHit {
  namespace?: string
  kind?: string
  name?: string
  status?: string
  detail?: string
  [key: string]: unknown
}

export interface K8sComponentHealth {
  name: string
  component: string
  status: 'healthy' | 'unhealthy' | 'not_found' | string
  phase: string
}

export interface K8sHealthIssue {
  level: 'critical' | 'warning' | 'info' | string
  type: string
  kind?: string
  namespace?: string
  name?: string
  reason?: string
  message?: string
  detail?: string
  suggestion?: string
  count?: number
}

export interface K8sHealthSummary {
  api_reachable: boolean
  api_latency_ms: number
  api_error?: string
  node_stats: { total: number; ready: number; not_ready: number; unschedulable: number }
  // 后端按 Kind 分组统计，每个 Kind 是 {total, degraded} 对象而非数字。
  workload_stats: {
    total: number
    degraded: number
    deployments: { total: number; degraded: number }
    statefulsets: { total: number; degraded: number }
    daemonsets: { total: number; degraded: number }
    [k: string]: unknown
  }
  pod_stats: { running: number; abnormal: number; pending: number; crash_loop: number; namespace_count?: number }
  recent_warning_events: Array<{ type?: string; reason?: string; message?: string; last_timestamp?: string; [k: string]: unknown }>
  top_issues: K8sHealthIssue[]
  summary: string
  error?: string
}

// ── WireGuard 虚拟组网 ──

export interface WgNetwork {
  id: number
  name: string
  cidr: string
  server_asset_id: string
  server_name: string
  endpoint: string
  port: number
  server_public_key: string
  forward_iface: string
  full_tunnel: boolean
  dns: string
  remark: string
  created_at: string
  peer_count: number
}

export interface WgNetworkPayload {
  name: string
  cidr: string
  server_asset_id?: string
  server_name?: string
  endpoint: string
  port: number
  forward_iface?: string
  full_tunnel?: boolean
  dns?: string
  remark?: string
}

export interface WgPeer {
  id: number
  network_id: number
  name: string
  asset_id: string
  allowed_ip: string
  public_key: string
  enabled: boolean
  remark: string
  created_at: string
}

export interface WgPeerPayload {
  name: string
  asset_id?: string
  allowed_ip?: string
  remark?: string
}

export interface WgPeerBundle {
  peer: WgPeer
  config: string
  qr: string
  install_script: string
}

// ── 数据库管理（SQL 工单平台） ──

export interface DbInstance {
  id: number
  name: string
  db_type: 'mysql' | 'postgresql' | string
  host: string
  port: number
  database: string
  username: string
  has_password: boolean
  remark: string
  /** 允许使用的角色列表（空/缺省 = 全员可用）。 */
  allowed_roles?: string[]
  status: 'ok' | 'error' | 'unknown' | string
  status_message: string
  created_at: string
}

export interface DbAuditFinding {
  rule: string
  level: 'block' | 'warn' | string
  message: string
  statement_index: number
}

export interface DbExecResultItem {
  index: number
  sql: string
  ok: boolean
  rows_affected?: number
  error?: string
  /** 失败时的中文可操作提示。 */
  hint?: string
  duration_ms: number
}

/** 逐级审批记录（多级审批链的每一步）。 */
export interface DbApprovalLogItem {
  step: number
  role: string
  reviewer: string
  decision: 'approve' | 'reject' | string
  note: string
  at: string
}

/** 查询授权申请明细（仅 query 工单）。 */
export interface DbGrantDetail {
  /** 表范围（空 = 全部表，审批参考用）。 */
  tables: string[]
  /** 授权时长（小时）。 */
  hours: number
  reason: string
}

/** 提交工单载荷：change 工单必填 sql_text；query 工单不携带 sql_text，改带 grant_* 授权字段（后端按 ticket_type 校验）。 */
export interface DbTicketCreatePayload {
  instance_id: number
  ticket_type: string
  title?: string
  sql_text?: string
  /** 目标库（空 = 实例默认库）。 */
  database?: string
  /** 定时执行时间（空 = 批准后手动执行）。 */
  execute_after?: string
  /** 查询授权申请字段（ticket_type=query 时后端读取）。 */
  grant_tables?: string[]
  grant_hours?: number
  grant_reason?: string
}

export interface DbTicket {
  id: number
  instance_id: number
  instance_name: string
  title: string
  sql_text: string
  /** 目标库（空 = 实例默认库），连接级切库；查询工单 = 授权目标库。 */
  database: string
  ticket_type: 'change' | 'query' | string
  status: 'pending' | 'approved' | 'executing' | 'success' | 'failed' | 'rejected' | string
  audit_result: DbAuditFinding[]
  audit_passed: boolean
  exec_result: DbExecResultItem[]
  submitter: string
  reviewer: string
  executor: string
  review_note: string
  /** 审批链快照（角色名数组，有序）；空数组 = 单级模式。 */
  approval_steps: string[]
  /** 当前审批级（1 起）；链空时恒为 1。 */
  current_step: number
  approval_log: DbApprovalLogItem[]
  grant_detail: DbGrantDetail
  /** 定时执行时间（空 = 批准后手动执行）。 */
  execute_after: string
  /** 是否已自动备份受影响行 / 是否可下载回滚语句。 */
  has_backup: boolean
  has_rollback: boolean
  created_at: string
  executed_at: string
}

/** 查询授权记录（工单终审自动生成或管理员手动授权）。 */
export interface DbQueryGrant {
  id: number
  ticket_id: number
  instance_id: number
  instance_name: string
  username: string
  display_name: string
  database: string
  tables: string[]
  reason: string
  expires_at: string
  granted_by: string
  status: 'active' | 'expired' | 'revoked' | string
  created_at: string
}

/** 审批流配置（按工单类型的角色链；steps 空 = 单级模式）。 */
export interface DbApprovalFlows {
  change: { steps: string[] }
  query: { steps: string[] }
  roles: string[]
}

/** 表结构（数据字典）。 */
export interface DbTableSchema {
  database: string
  table: string
  columns: Array<{
    name: string
    type: string
    nullable: boolean
    key: string
    default: string | null
    extra: string
    comment: string
  }>
  indexes: Array<{ name: string; column: string; unique: boolean; type: string }>
  /** 建表 DDL（MySQL 原版；PG 为空串）。 */
  ddl: string
}

export interface DbQueryResult {
  ok: boolean
  columns: string[]
  rows: Record<string, unknown>[]
  results: DbExecResultItem[]
  duration_ms: number
  /** 实际连接的目标库（查询面板可临时切库，与实例默认库可能不同）。 */
  database?: string
  instance_name?: string
  /** 失败时的中文可操作提示（后端把驱动报错翻过来的）。 */
  hint?: string
}

export interface DbQueryLogItem {
  id: number
  instance_name: string
  username: string
  sql_text: string
  /** 实际连接的目标库，空表示老数据（该字段是后加的）。 */
  database: string
  row_count: number
  duration_ms: number
  ok: boolean
  error: string
  created_at: string
}
