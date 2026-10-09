import client from './client'
import type { AxiosRequestConfig } from 'axios'
import type { LoginRequest, TokenResponse, AssetResponse, AssetCreate, GroupResponse, GroupCreate, DashboardOverview, RBACMetadata, RiskLevelsResponse, RiskChallengeRequest, RiskChallengeResponse, NotificationSseTicketResponse, NotificationTemplateItem, IngressTlsStatusItem, VMResponse, VMCreate, VMNodeItem, VMStorageItem, VMIsoItem, VMBridgeItem, VMTemplateItem, CredentialResponse, CredentialCreate, CredentialUpdate, DomainResponse, DomainCreate, DomainUpdate, DomainDNSRecordResponse, DomainDNSRecordCreate, DomainProviderItem, DomainAccountItem, CertificateResponse, CertificateIssueRequest, CertificateUploadRequest, CertificateUpdateRequest, CertificateDeployRequest, CertificateAccountItem, NotificationListResponse, ClusterResponse, ClusterCreate, UserResponse, UserCreateRequest, UserAccessResponse, UserAccessUpdate, RoleTemplateItem, RoleTemplateCreateRequest, RoleTemplateUpdateRequest, AuditLogResponse, NotificationChannel, FeatureToggles, LoginPageConfig, MonitoringConfig, MonitoringTargetsResponse, LogTargetsResponse, AlertRuleItem, AlertRulesResponse, HostMetricsResponse, CloudSyncRequest, CloudSyncPreviewResponse, CloudSyncImportResponse, CloudRegionsResponse, K8sHealthSummary, K8sComponentHealth, K8sNode, K8sNodeDrainPreview, K8sWorkload, K8sPod, K8sResourceEvent, K8sResourceItem, K8sNodeDetail, K8sRolloutRevision, K8sRolloutStatus, K8sCronJobRun, K8sSearchHit, ClusterAuditLog, ImBinding, FeishuImConfig, ImUser, ScheduledTaskStatus } from '@/types/api'

// ── 个人 IM 渠道（飞书群 @ 通知） ──
export const imApi = {
  // 我的绑定
  getBinding: () => client.get<ImBinding>('/im/feishu/binding').then((r) => r.data),
  bind: (data: { identity: string; webhook_url?: string; notify_mode: string }) =>
    client.put<ImBinding>('/im/feishu/binding', data).then((r) => r.data),
  unbind: () => client.delete('/im/feishu/binding').then((r) => r.data),
  test: (webhook_url = '') => client.post('/im/feishu/test', { webhook_url }).then((r) => r.data),
  // 可选通知用户（告警规则选人）
  listUsers: () => client.get<ImUser[]>('/im/users').then((r) => r.data),
  // 管理员：飞书应用配置
  getFeishuConfig: () => client.get<FeishuImConfig>('/im/feishu/config').then((r) => r.data),
  saveFeishuConfig: (data: { app_id: string; app_secret?: string; default_webhook_url: string }) =>
    client.put<FeishuImConfig>('/im/feishu/config', data).then((r) => r.data),
}

// ── Auth ──
export const authApi = {
  login: (data: LoginRequest) => client.post<TokenResponse>('/auth/login', data).then((r) => r.data),
  getMe: () => client.get('/auth/me').then((r) => r.data),
  /** 风险等级表（后端返回 {"levels": {...}} 信封，调用方取 .levels） */
  getRiskLevels: () => client.get<RiskLevelsResponse>('/auth/risk-levels').then((r) => r.data),
  /** 高危操作取一次性票据（随业务请求以 X-Risk-Ticket 头发送，后端一次性消费） */
  riskChallenge: (data: RiskChallengeRequest) =>
    client.post<RiskChallengeResponse>('/auth/risk-challenge', data).then((r) => r.data),
}

// ── Dashboard ──
export const dashboardApi = {
  overview: () => client.get<DashboardOverview>('/dashboard/overview').then((r) => r.data),
}

// ── Assets ──
export const assetsApi = {
  listHostKeys: (host?: string): Promise<Array<{
    id: number; host: string; port: number; key_type: string
    fingerprint: string; first_seen_at?: string; last_seen_at?: string
  }>> =>
    client.get('/assets/host-keys', { params: host ? { host } : {} }).then((r) => r.data),
  resetHostKey: (assetId: string) =>
    client.post(`/assets/${assetId}/reset-host-key`).then((r) => r.data),
  list: () => client.get<AssetResponse[]>('/assets').then((r) => r.data),
  /** 服务端分页/筛选列表（GET /assets 的 limit/offset/q/status/source/group_id 参数）。
   *  返回当前页数据与 X-Total-Count 筛选总数；limit=0 等价全量（向后兼容）。 */
  listPaged: (params: {
    limit: number; offset: number; q?: string; status?: string; source?: string; group_id?: number
  }) =>
    client.get<AssetResponse[]>('/assets', { params }).then((r) => ({
      items: r.data,
      total: Number(r.headers?.['x-total-count'] ?? r.data.length),
    })),
  getById: (id: string) => client.get<AssetResponse>(`/assets/detail/${id}`).then((r) => r.data),
  create: (data: AssetCreate) => client.post<AssetResponse>('/assets', data).then((r) => r.data),
  update: (id: string, data: AssetCreate) =>
    client.put<{ message: string; config_collecting: boolean; asset: AssetResponse }>(`/assets/${id}`, data).then((r) => r.data),
  delete: (id: string) => client.delete(`/assets/${id}`).then((r) => r.data),
  batchDelete: (ids: string[]) => client.post('/assets/batch-delete', { ids }).then((r) => r.data),
  ping: (data: { ip: string; port: number; user: string; password?: string; credential_id?: string; pkey_content?: string; asset_id?: string }) =>
    client.post('/assets/ping', data).then((r) => r.data),
  sync: (data: CloudSyncRequest) =>
    client.post<CloudSyncPreviewResponse & CloudSyncImportResponse>('/assets/sync', data).then((r) => r.data),
  getCloudRegions: (data: { provider: string; credential_id?: string; access_key?: string; secret_key?: string }) =>
    client.post<CloudRegionsResponse>('/assets/cloud/regions', data).then((r) => r.data),
  refreshConfig: (id: string) => client.post(`/assets/${id}/refresh-config`).then((r) => r.data),
}

// ── 集群 ──
export const clustersApi = {
  list: () => client.get<ClusterResponse[]>('/clusters').then((r) => r.data),
  create: (data: ClusterCreate) => client.post<ClusterResponse>('/clusters', data).then((r) => r.data),
  update: (id: string, data: { name?: string; provider?: string }) => client.put<ClusterResponse>(`/clusters/${id}`, data).then((r) => r.data),
  delete: (id: string) => client.delete(`/clusters/${id}`).then((r) => r.data),
  healthSummary: (id: string) => client.get(`/clusters/${id}/health-summary`).then((r) => r.data),
  scan: (kubeconfig_yaml: string) => client.post('/clusters/scan', { kubeconfig_yaml }).then((r) => r.data),
}

// ── 用户管理 ──
export const usersApi = {
  list: () => client.get<UserResponse[]>('/settings/users').then((r) => r.data),
  create: (data: UserCreateRequest) => client.post<UserResponse>('/settings/users', data).then((r) => r.data),
  getAccess: (id: number) => client.get<UserAccessResponse>(`/settings/users/${id}/access`).then((r) => r.data),
  updateAccess: (id: number, data: UserAccessUpdate) => client.put<UserAccessResponse>(`/settings/users/${id}/access`, data).then((r) => r.data),
  updateStatus: (id: number, status: string) => client.put(`/settings/users/${id}/status`, { status }).then((r) => r.data),
  delete: (id: number, ticket: string) =>
    client.delete(`/settings/users/${id}`, { headers: { 'X-Risk-Ticket': ticket } }).then((r) => r.data),
  resetPassword: (id: number, new_password: string) => client.post(`/settings/users/${id}/reset-password`, { new_password }).then((r) => r.data),
}

// ── Groups ──
export const groupsApi = {
  list: () => client.get<GroupResponse[]>('/groups').then((r) => r.data),
  create: (data: GroupCreate) => client.post<GroupResponse>('/groups', data).then((r) => r.data),
  update: (id: number, data: Partial<GroupCreate>) => client.put<GroupResponse>(`/groups/${id}`, data).then((r) => r.data),
  delete: (id: number) => client.delete(`/groups/${id}`).then((r) => r.data),
  assign: (asset_ids: string[], group_id: number | null, resource_type?: string) =>
    client.post('/groups/assign', { asset_ids, group_id, resource_type: resource_type ?? 'host' }).then((r) => r.data),
}

// ── RBAC ──
export const rbacApi = {
  metadata: () => client.get<RBACMetadata>('/settings/rbac/metadata').then((r) => r.data),
  getUserAccess: (userId: number) => client.get(`/settings/users/${userId}/access`).then((r) => r.data),
  createRole: (data: RoleTemplateCreateRequest) => client.post<RoleTemplateItem>('/settings/rbac/role-templates', data).then((r) => r.data),
  updateRole: (role: string, data: RoleTemplateUpdateRequest) => client.put<RoleTemplateItem>(`/settings/rbac/role-templates/${role}`, data).then((r) => r.data),
  deleteRole: (role: string) => client.delete(`/settings/rbac/role-templates/${role}`).then((r) => r.data),
}

// ── 审计日志 ──
export const auditApi = {
  list: () => client.get<AuditLogResponse[]>('/settings/audits').then((r) => r.data),
}

// ── Login Page Config (免鉴权) ──
export const loginPageApi = {
  getConfig: () => client.get('/settings/login-page').then((r) => r.data),
}

// ── 虚拟机 ──
export const vmsApi = {
  list: (q?: string) => client.get<VMResponse[]>('/vms', { params: { q } }).then((r) => r.data),
  create: (data: VMCreate) => client.post<VMResponse>('/vms', data).then((r) => r.data),
  update: (id: string, data: VMCreate) => client.put<VMResponse>(`/vms/${id}`, data).then((r) => r.data),
  delete: (id: string) => client.delete(`/vms/${id}`).then((r) => r.data),
  batchDelete: (ids: string[]) => client.post('/vms/batch-delete', { ids }).then((r) => r.data),
  power: (id: string, action: string) => client.post(`/vms/${id}/power`, { action }).then((r) => r.data),
  clone: (id: string, name: string) => client.post(`/vms/${id}/clone`, { name }).then((r) => r.data),
  sync: (provider?: string) => client.post('/vms/sync', null, { params: { provider } }).then((r) => r.data),
  getNodes: () => client.get<VMNodeItem[]>('/vms/nodes').then((r) => r.data),
  getStorages: (node = '') => client.get<VMStorageItem[]>('/vms/storages', { params: { node } }).then((r) => r.data),
  getIsos: (node = '') => client.get<VMIsoItem[]>('/vms/isos', { params: { node } }).then((r) => r.data),
  getBridges: (node = '') => client.get<VMBridgeItem[]>('/vms/bridges', { params: { node } }).then((r) => r.data),
  getTemplates: () => client.get<VMTemplateItem[]>('/vms/templates').then((r) => r.data),
  selfCheck: () => client.get('/vms/self-check').then((r) => r.data),
}

// ── 凭证 ──
export const credentialsApi = {
  list: () => client.get<CredentialResponse[]>('/credentials').then((r) => r.data),
  create: (data: CredentialCreate) => client.post<CredentialResponse>('/credentials', data).then((r) => r.data),
  update: (id: string, data: CredentialUpdate) => client.put<CredentialResponse>(`/credentials/${id}`, data).then((r) => r.data),
  delete: (id: string) => client.delete(`/credentials/${id}`).then((r) => r.data),
  batchDelete: (ids: string[]) => client.post('/credentials/batch-delete', { ids }).then((r) => r.data),
  /** 导出单条凭证明文附件（需 X-Risk-Ticket 票据）。返回完整响应，文件名在 Content-Disposition 头 */
  exportFile: (id: string) => client.get<Blob>(`/credentials/${id}/export`, { responseType: 'blob' }),
  /** 批量导出凭证明文 ZIP 附件（需 X-Risk-Ticket 票据）。返回完整响应，文件名在 Content-Disposition 头 */
  batchExportFile: (ids: string[]) => client.post<Blob>('/credentials/batch-export', { ids }, { responseType: 'blob' }),
  accessOptions: () => client.get<{ role: string; title: string; description: string }[]>('/credentials/access-options').then((r) => r.data),
}

// ── 域名 ──
export const domainsApi = {
  list: (q?: string) => client.get<DomainResponse[]>('/domains', { params: { q } }).then((r) => r.data),
  create: (data: DomainCreate) => client.post<DomainResponse>('/domains', data).then((r) => r.data),
  update: (id: number, data: DomainUpdate) => client.put<DomainResponse>(`/domains/${id}`, data).then((r) => r.data),
  delete: (id: number) => client.delete(`/domains/${id}`).then((r) => r.data),
  batchDelete: (ids: number[]) => client.post('/domains/batch-delete', { ids }).then((r) => r.data),
  providers: () => client.get<DomainProviderItem[]>('/domains/providers').then((r) => r.data),
  accounts: () => client.get<DomainAccountItem[]>('/domains/accounts').then((r) => r.data),
  listRecords: (domainId: number, q?: string) => client.get<DomainDNSRecordResponse[]>(`/domains/${domainId}/records`, { params: { q } }).then((r) => r.data),
  createRecord: (domainId: number, data: DomainDNSRecordCreate) => client.post<DomainDNSRecordResponse>(`/domains/${domainId}/records`, data).then((r) => r.data),
  updateRecord: (domainId: number, recordId: number, data: DomainDNSRecordCreate) => client.put<DomainDNSRecordResponse>(`/domains/${domainId}/records/${recordId}`, data).then((r) => r.data),
  deleteRecord: (domainId: number, recordId: number) => client.delete(`/domains/${domainId}/records/${recordId}`).then((r) => r.data),
}

// ── 站内通知 ──
export const notificationsApi = {
  list: (params?: { is_read?: boolean; category?: string; q?: string; limit?: number; offset?: number }) =>
    client.get<NotificationListResponse>('/notifications', { params }).then((r) => r.data),
  unreadCount: () => client.get<{ unread_count: number }>('/notifications/unread-count').then((r) => r.data),
  markRead: (id: string) => client.post(`/notifications/${id}/read`).then((r) => r.data),
  markAllRead: () => client.post('/notifications/read-all').then((r) => r.data),
  remove: (id: string) => client.delete(`/notifications/${id}`).then((r) => r.data),
  clear: () => client.post('/notifications/clear').then((r) => r.data),
  /** 取通知 SSE 短时效票据（EventSource 不支持自定义 header，票据走 query param） */
  sseTicket: () =>
    client.post<NotificationSseTicketResponse>('/notifications/stream/ticket').then((r) => r.data),
}

// ── 证书 ──
export const certificatesApi = {
  list: (q?: string) => client.get<CertificateResponse[]>('/certificates', { params: { q } }).then((r) => r.data),
  getById: (id: string) => client.get<CertificateResponse>(`/certificates/${id}`).then((r) => r.data),
  update: (id: string, data: CertificateUpdateRequest) => client.put<CertificateResponse>(`/certificates/${id}`, data).then((r) => r.data),
  delete: (id: string) => client.delete(`/certificates/${id}`).then((r) => r.data),
  batchDelete: (ids: string[]) => client.post('/certificates/batch-delete', { ids }).then((r) => r.data),
  issue: (data: CertificateIssueRequest) => client.post<CertificateResponse>('/certificates/issue', data).then((r) => r.data),
  upload: (data: CertificateUploadRequest) => client.post<CertificateResponse>('/certificates/upload', data).then((r) => r.data),
  renew: (id: string) => client.post(`/certificates/${id}/renew`).then((r) => r.data),
  revoke: (id: string, reason?: string) => client.post(`/certificates/${id}/revoke`, { reason }).then((r) => r.data),
  autoRenew: (id: string, enabled: boolean) => client.post(`/certificates/${id}/auto-renew`, { enabled }).then((r) => r.data),
  refresh: (id: string) => client.post(`/certificates/${id}/refresh`).then((r) => r.data),
  deploy: (id: string, data: CertificateDeployRequest) => client.post(`/certificates/${id}/deploy`, data).then((r) => r.data),
  /** 下载证书文件包（cert/key/fullchain/ca + metadata 打包 zip，需 X-Risk-Ticket 票据）。返回完整响应，文件名在 Content-Disposition 头 */
  downloadFile: (id: string) => client.get<Blob>(`/certificates/${id}/download`, { responseType: 'blob' }),
  accounts: () => client.get<CertificateAccountItem[]>('/certificates/accounts').then((r) => r.data),
}

// ── 系统管理 ──
export const systemApi = {
  // 通知渠道
  getChannels: () => client.get<{ channels: NotificationChannel[]; updated_at: string }>('/monitoring/notifications').then((r) => r.data),
  saveChannels: (channels: NotificationChannel[]) => client.put('/monitoring/notifications', { channels }).then((r) => r.data),
  testChannel: (id: string) => client.post(`/monitoring/notifications/${id}/test`).then((r) => r.data),
  // 通知记录（按告警指纹过滤，供详情时间线；挂载在 /monitoring/alerts 前缀下）
  notificationLogs: (params: { fingerprint?: string; page?: number; page_size?: number; q?: string; success?: string }) =>
    client.get<{ logs: Array<Record<string, unknown>>; total: number }>('/monitoring/alerts/notifications/logs', { params }).then((r) => r.data),
  // 通知模板
  getTemplates: () => client.get<{ templates: NotificationTemplateItem[]; updated_at: string }>('/monitoring/notifications/templates').then((r) => r.data),
  saveTemplates: (templates: NotificationTemplateItem[]) => client.put('/monitoring/notifications/templates', { templates }).then((r) => r.data),
  // 功能开关
  getToggles: () => client.get<{ toggles: FeatureToggles; updated_at: string }>('/settings/feature-toggles').then((r) => r.data),
  saveToggles: (toggles: FeatureToggles) => client.put('/settings/feature-toggles', { toggles }).then((r) => r.data),
  // 登录页配置
  getLoginPage: () => client.get<LoginPageConfig>('/settings/login-page').then((r) => r.data),
  saveLoginPage: (data: Partial<LoginPageConfig>) => client.put('/settings/login-page', data).then((r) => r.data),
  // 监控配置
  getMonitoring: () => client.get<MonitoringConfig>('/monitoring/config').then((r) => r.data),
  saveMonitoring: (data: Partial<MonitoringConfig> & { secret?: string }) => client.put('/monitoring/config', data).then((r) => r.data),
  selfCheck: (data?: Partial<MonitoringConfig> & { secret?: string }) =>
    client.post('/monitoring/self-check', data ?? {}).then((r) => r.data),
  scheduledTasks: () => client.get<{ tasks: Array<ScheduledTaskStatus>; updated_at: string }>('/settings/scheduled-tasks').then((r) => r.data),
  updateScheduledTasks: (tasks: Record<string, { enabled: boolean; interval_seconds: number }>) =>
    client.put<{ tasks: Array<ScheduledTaskStatus>; updated_at: string }>('/settings/scheduled-tasks', { tasks }).then((r) => r.data),
}

// ── 监控指标 / 采集状态 ──
export const monitoringApi = {
  hostMetrics: (params: { host_name?: string; host_ip?: string; range_minutes?: number; step_seconds?: number }) =>
    client.get<HostMetricsResponse>('/monitoring/host-metrics', { params }).then((r) => r.data),
  targets: () => client.get<MonitoringTargetsResponse>('/monitoring/targets').then((r) => r.data),
  logTargets: () => client.get<LogTargetsResponse>('/monitoring/log-targets').then((r) => r.data),
  // 告警规则
  listRules: () => client.get<AlertRulesResponse>('/monitoring/rules').then((r) => r.data),
  saveRules: (rules: AlertRuleItem[]) =>
    client.put<AlertRulesResponse>('/monitoring/rules', { rules }).then((r) => r.data),
  /** 保存前命中预览：对 expression 即时试跑，返回命中序列 */
  testRule: (data: { expr: string; host_ids?: string[] }) =>
    client.post<import('@/types/api').RuleTestResponse>('/monitoring/rules/test', data).then((r) => r.data),
  /** 链路自检：采集心跳 / 规则评估 / 渠道连通 */
  linkHealth: () =>
    client.get<import('@/types/api').LinkHealthResponse>('/monitoring/link-health').then((r) => r.data),
  /** Grafana 免密跳转（可带 explore 条件） */
  grafanaSsoLink: () =>
    client.get<import('@/types/api').GrafanaSsoLink>('/monitoring/grafana/sso-link').then((r) => r.data),
  /** 历史告警（已恢复归档 + 仍活跃合并视图） */
  alertHistory: (params?: { state?: string; severity?: string; limit?: number; offset?: number }) =>
    client.get<{ alerts: import('@/types/api').HistoryAlertItem[]; total: number }>(
      '/monitoring/alerts/history',
      { params },
    ).then((r) => r.data),
  /** 静默规则列表 */
  listSilences: () =>
    client.get<{ silences: import('@/types/api').SilenceRuleItem[]; total: number }>('/monitoring/alerts/silence')
      .then((r) => r.data),
  /** SSH 安装 Alloy Agent */
  installAlloy: (assetId: string, payload?: { port?: number; job?: string }) =>
    client.post(`/monitoring/targets/${encodeURIComponent(assetId)}/install-alloy`, payload ?? {}).then((r) => r.data),
  /** 兼容旧入口：实际同样安装 Alloy */
  installNodeExporter: (assetId: string, payload?: { port?: number; job?: string }) =>
    client.post(`/monitoring/targets/${encodeURIComponent(assetId)}/install-node-exporter`, payload ?? {}).then((r) => r.data),
  /** 彻底卸载 Alloy Agent */
  uninstallAlloy: (assetId: string) =>
    client.post(`/monitoring/targets/${encodeURIComponent(assetId)}/uninstall-alloy`).then((r) => r.data),
}

// ── WebSSH 终端 ──
export const terminalApi = {
  createSession: (asset_id: string, cols?: number, rows?: number) =>
    client.post<{
      session_id: string
      session_token: string
      expires_at: number
      ws_path: string
    }>('/assets/terminal/session', { asset_id, cols: cols ?? 120, rows: rows ?? 36 }).then((r) => r.data),
  listFiles: (asset_id: string, path: string) =>
    client.get<{
      path: string
      parent: string
      items: Array<{ name: string; path: string; size: number; is_dir: boolean; mode: string; modified_at: string; is_symlink: boolean }>
    }>(`/assets/terminal/files/${encodeURIComponent(asset_id)}/list`, { params: { path } }).then((r) => r.data),
  mkdir: (asset_id: string, path: string) =>
    client.post(`/assets/terminal/files/${encodeURIComponent(asset_id)}/mkdir`, { path }).then((r) => r.data),
  deleteFile: (asset_id: string, path: string) =>
    client.delete(`/assets/terminal/files/${encodeURIComponent(asset_id)}`, { params: { path } }).then((r) => r.data),
  uploadFile: (asset_id: string, path: string, file: File) => {
    const form = new FormData()
    form.append('path', path)
    form.append('file', file)
    return client.post(`/assets/terminal/files/${encodeURIComponent(asset_id)}/upload`, form).then((r) => r.data)
  },
  downloadUrl: (asset_id: string, path: string) =>
    `/api/assets/terminal/files/${encodeURIComponent(asset_id)}/download?path=${encodeURIComponent(path)}`,
}

// ── 集群健康摘要 ──
export const clusterHealthApi = {
  summary: (cluster_id: string) => client.get(`/clusters/${cluster_id}/health-summary`).then((r) => r.data),
}

// ── AI Agent 智能工作台 ──
export const aiApi = {
  listSessions: () => client.get<{ sessions: import('@/types/api').AISessionItem[] }>('/ai/sessions').then((r) => r.data.sessions || []),
  createSession: (title?: string) =>
    client.post<import('@/types/api').AISessionItem>('/ai/sessions', { title: title || '新对话' }).then((r) => r.data),
  deleteSession: (session_id: string) => client.delete(`/ai/sessions/${session_id}`).then((r) => r.data),
  renameSession: (session_id: string, title: string) =>
    client.put(`/ai/sessions/${session_id}`, { title }).then((r) => r.data),
  getSessionDetail: (session_id: string) =>
    client.get<{ session_id: string; title: string; messages: import('@/types/api').AIMessageItem[] }>(`/ai/sessions/${session_id}`).then((r) => r.data),
  appendMessage: (session_id: string, role: 'user' | 'assistant', content: string, meta?: Record<string, unknown>) =>
    client.post(`/ai/sessions/${session_id}/messages`, { role, content, meta }).then((r) => r.data),
  listSkills: () => client.get<{ skills: import('@/types/api').AISkillItem[] }>('/ai/skills').then((r) => r.data.skills || []),
  updateSkills: (skills: Array<{ key: string; enabled: boolean }>) =>
    client.put('/ai/skills', { skills }).then((r) => r.data),
  listConfigs: () => client.get<{ items: import('@/types/api').LLMConfigItem[]; active_id?: string }>('/ai/configs').then((r) => r.data.items || []),
  getStreamUrl: (scenario: string = 'ops') => `/api/ai/${scenario}/query/stream`,
  generateCommand: (requirement: string, os_hint = 'Linux') =>
    client
      .post<{ ok: boolean; command: string; mode: string; provider: string; model: string; notice: string }>(
        '/ai/command/generate',
        { requirement, os_hint },
      )
      .then((r) => r.data),
}

// ── 应用管理 ──
export const applicationsApi = {
  list: (params?: { q?: string; group_id?: number; category?: string; status?: string; environment?: string }) =>
    client.get<import('@/types/api').ApplicationResponse[]>('/applications', { params }).then((r) => r.data),
  getById: (appId: string) =>
    client.get<import('@/types/api').ApplicationResponse>(`/applications/${appId}`).then((r) => r.data),
  create: (data: import('@/types/api').ApplicationPayload) =>
    client.post<import('@/types/api').ApplicationResponse>('/applications', data).then((r) => r.data),
  update: (appId: string, data: import('@/types/api').ApplicationPayload) =>
    client.put<import('@/types/api').ApplicationResponse>(`/applications/${appId}`, data).then((r) => r.data),
  delete: (appId: string) =>
    client.delete(`/applications/${appId}`).then((r) => r.data),
  bindResources: (appId: string, resources: Array<{ resource_type: string; resource_id: string }>) =>
    client.put(`/applications/${appId}/resources`, resources).then((r) => r.data),
  getDetail: (appId: string) =>
    client.get<import('@/types/api').ApplicationDetailResponse>(`/applications/${appId}`).then((r) => r.data),
  getOverview: (appId: string) =>
    client.get<import('@/types/api').ApplicationOverviewResponse>(`/applications/${appId}/overview`).then((r) => r.data),
  createComponent: (appId: string, data: import('@/types/api').ApplicationComponentPayload) =>
    client.post<import('@/types/api').ApplicationComponentResponse>(`/applications/${appId}/components`, data).then((r) => r.data),
  preflightCompose: (appId: string, data: import('@/types/api').ApplicationComposePreflightRequest) =>
    client.post<import('@/types/api').ApplicationComposePreflightResponse>(`/applications/${appId}/compose/preflight`, data).then((r) => r.data),
  deleteComponent: (appId: string, componentId: string) =>
    client.delete(`/applications/${appId}/components/${componentId}`).then((r) => r.data),
  batchDeleteComponents: (appId: string, componentIds: string[]) =>
    client.post<{ success: boolean; deleted_count: number }>(`/applications/${appId}/components/batch-delete`, { component_ids: componentIds }).then((r) => r.data),
  controlComponent: (appId: string, componentId: string, action: string) =>
    client.post<import('@/types/api').ApplicationRuntimeActionResponse>(`/applications/${appId}/components/${componentId}/actions`, { action }).then((r) => r.data),
  discoverComponents: (appId: string, assetIds: string[], types: string[], includeSystemServices = false) =>
    client.post<import('@/types/api').ApplicationRuntimeDiscoveryResponse>(`/applications/${appId}/runtime-discovery`, {
      asset_ids: assetIds,
      types,
      include_system_services: includeSystemServices,
    }).then((r) => r.data),
  // 配置管理
  listConfigs: (appId: string, params?: { env?: string; q?: string }) =>
    client.get<import('@/types/api').ApplicationConfigResponse[]>(`/applications/${appId}/configs`, { params }).then((r) => r.data),
  createConfig: (appId: string, data: import('@/types/api').ApplicationConfigPayload) =>
    client.post<import('@/types/api').ApplicationConfigResponse>(`/applications/${appId}/configs`, data).then((r) => r.data),
  updateConfig: (appId: string, configId: string, data: import('@/types/api').ApplicationConfigPayload) =>
    client.put<import('@/types/api').ApplicationConfigResponse>(`/applications/${appId}/configs/${configId}`, data).then((r) => r.data),
  deleteConfig: (appId: string, configId: string) =>
    client.delete(`/applications/${appId}/configs/${configId}`).then((r) => r.data),
  batchDeleteConfigs: (appId: string, configIds: string[]) =>
    client.post(`/applications/${appId}/configs/batch-delete`, { config_ids: configIds }).then((r) => r.data),
  importConfigs: (appId: string, data: import('@/types/api').ApplicationConfigImportPayload) =>
    client.post<import('@/types/api').ApplicationConfigImportResponse>(`/applications/${appId}/configs/import`, data).then((r) => r.data),
  listConfigRevisions: (appId: string, configId: string) =>
    client.get<import('@/types/api').ApplicationConfigRevisionResponse[]>(`/applications/${appId}/configs/${configId}/revisions`).then((r) => r.data),
  /** 配置版本对比（ConfigDiffViewer 用，替代裸 fetch 走鉴权与票据通道） */
  compareConfig: (appId: string, configId: string, targetRevisionId: number) =>
    client.get(`/applications/${appId}/configs/${configId}/compare`, { params: { target_revision_id: targetRevisionId } }).then((r) => r.data),
  /** 配置回滚到指定修订（config:rollback 高危确认后调用） */
  rollbackConfig: (appId: string, configId: string, targetRevisionId: number) =>
    client.post(`/applications/${appId}/configs/${configId}/rollback`, { target_revision_id: targetRevisionId }).then((r) => r.data),
  getCredentialRefs: () =>
    client.get<import('@/types/api').CredentialRefOption[]>('/applications/config-credential-options').then((r) => r.data),
  // 发布管理
  listReleases: (params?: { app_id?: string; env?: string; status?: string; limit?: number; offset?: number }) =>
    client.get<import('@/types/api').ApplicationReleaseResponse[]>('/applications/releases', { params }).then((r) => r.data),
  createRelease: (appId: string, data: import('@/types/api').ApplicationReleasePayload) =>
    client.post<import('@/types/api').ApplicationReleaseResponse>(`/applications/${appId}/releases`, data).then((r) => r.data),
  getReleaseDetail: (appId: string, releaseId: string) =>
    client.get<import('@/types/api').ApplicationReleaseResponse>(`/applications/${appId}/releases/${releaseId}`).then((r) => r.data),
  retryRelease: (appId: string, releaseId: string) =>
    client.post<import('@/types/api').ApplicationReleaseResponse>(`/applications/${appId}/releases/${releaseId}/retry`).then((r) => r.data),
  getDependencies: (appId: string) =>
    client.get<import('@/types/api').ApplicationDependenciesResponse>(`/applications/${appId}/dependencies`).then((r) => r.data),
  getAnsibleTemplates: () =>
    client.get<import('@/types/api').AnsibleTemplateOption[]>('/ansible/templates').then((r) => r.data),
}

// ── Finance 费用管理 ──
export const financeApi = {
  // 云账号
  listAccounts: () => client.get<import('@/types/api').CloudAccountItem[]>('/finance/accounts').then((r) => r.data),
  createAccount: (data: import('@/types/api').CloudAccountPayload) =>
    client.post<import('@/types/api').CloudAccountItem>('/finance/accounts', data).then((r) => r.data),
  updateAccount: (id: number, data: import('@/types/api').CloudAccountPayload) =>
    client.put<import('@/types/api').CloudAccountItem>(`/finance/accounts/${id}`, data).then((r) => r.data),
  deleteAccount: (id: number) => client.delete(`/finance/accounts/${id}`).then((r) => r.data),
  verifyAccount: (id: number) => client.post<import('@/types/api').CloudAccountItem>(`/finance/accounts/${id}/verify`).then((r) => r.data),
  syncAccount: (id: number, data?: { billing_cycle?: string; include_previous?: boolean }) =>
    client.post(`/finance/accounts/${id}/sync`, data ?? {}).then((r) => r.data),
  listSyncRecords: (id: number, limit = 20) =>
    client.get<import('@/types/api').BillSyncRunItem[]>(`/finance/accounts/${id}/sync-records`, { params: { limit } }).then((r) => r.data),
  listCredentialOptions: () =>
    client.get<import('@/types/api').CredentialOption[]>('/finance/credentials-options').then((r) => r.data),
  // 总览与账单
  overview: (params?: { month?: string; account_id?: number }) =>
    client.get<import('@/types/api').FinanceOverview>('/finance/overview', { params }).then((r) => r.data),
  exportBillsUrl: (params?: Record<string, string | number | undefined>) => {
    const q = new URLSearchParams()
    Object.entries(params ?? {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') q.set(k, String(v))
    })
    const qs = q.toString()
    return `/api/finance/bills/export${qs ? `?${qs}` : ''}`
  },
  listBills: (params?: {
    month?: string
    account_id?: number
    product?: string
    keyword?: string
    subscription_type?: string
    page?: number
    page_size?: number
  }) => client.get<import('@/types/api').CloudBillListResponse>('/finance/bills', { params }).then((r) => r.data),
  groupBills: (params?: { dimension?: string; month?: string; account_id?: number }) =>
    client.get<import('@/types/api').CloudBillGroupResponse>('/finance/bills/group-by', { params }).then((r) => r.data),
}

// ── AI 平台扩展（模型配置 / 编排 / 观测） ──
export const aiPlatformApi = {
  // LLM 模型配置
  listLLMConfigs: () =>
    client.get<import('@/types/api').LLMConfigListResponse>('/ai/configs').then((r) => r.data),
  createLLMConfig: (data: import('@/types/api').LLMConfigPayload) =>
    client.post<import('@/types/api').LLMConfigDetail>('/ai/configs', data).then((r) => r.data),
  updateLLMConfig: (configId: string, data: import('@/types/api').LLMConfigPayload) =>
    client.put<import('@/types/api').LLMConfigDetail>(`/ai/configs/${configId}`, data).then((r) => r.data),
  deleteLLMConfig: (configId: string) => client.delete(`/ai/configs/${configId}`).then((r) => r.data),
  activateLLMConfig: (configId: string) =>
    client.post(`/ai/configs/${configId}/activate`).then((r) => r.data),
  testLLMConfig: (data: import('@/types/api').LLMConfigPayload) =>
    client.post<import('@/types/api').LLMConfigTestResult>('/ai/config/test', data).then((r) => r.data),
  // 编排前置策略与预检
  getOrchestrationPolicy: () =>
    client.get<import('@/types/api').OrchestrationPolicy>('/ai/orchestration/policy').then((r) => r.data),
  updateOrchestrationPolicy: (data: import('@/types/api').OrchestrationPolicyPayload) =>
    client.put<import('@/types/api').OrchestrationPolicy>('/ai/orchestration/policy', data).then((r) => r.data),
  previewOrchestration: (data: import('@/types/api').OrchestrationPreviewPayload) =>
    client.post<import('@/types/api').OrchestrationPreviewResult>('/ai/orchestration/preview', data).then((r) => r.data),
  // AI 观测大盘
  observabilityStats: () =>
    client.get<import('@/types/api').AIObservabilityStats>('/ai/observability/stats').then((r) => r.data),
  observabilityAudit: () =>
    client.get<{ logs: import('@/types/api').AIAuditLogItem[] }>('/ai/observability/audit').then((r) => r.data),
  // 技能注册表（含 updated_at 的完整响应）
  getSkillRegistry: () =>
    client.get<import('@/types/api').SkillRegistryResponse>('/ai/skills').then((r) => r.data),
  // 保存技能开关（后端 PUT /ai/skills，需 action.ai.skills.manage）
  updateSkills: (skills: Array<{ key: string; enabled: boolean }>) =>
    client.put<import('@/types/api').SkillRegistryResponse>('/ai/skills', { skills }).then((r) => r.data),
}

// ── 指标直查（VictoriaMetrics PromQL 代理） ──
export interface VMSeries {
  metric: Record<string, string>
  values?: [number, string][]
  value?: [number, string]
}
export const metricsQueryApi = {
  instant: (params: { query: string; time?: string }) =>
    client.get<{ configured: boolean; message?: string; data?: { resultType: string; result: VMSeries[] } }>(
      '/monitoring/metrics/query', { params }).then((r) => r.data),
  range: (params: { query: string; range_minutes?: number; step_seconds?: number }) =>
    client.get<{ configured: boolean; message?: string; data?: { result: VMSeries[] }; range?: { minutes: number; step_seconds: number } }>(
      '/monitoring/metrics/query_range', { params }).then((r) => r.data),
  labels: () =>
    client.get<{ configured: boolean; message?: string; labels: string[] }>('/monitoring/metrics/labels').then((r) => r.data),
  labelValues: (label: string) =>
    client.get<{ configured: boolean; message?: string; values: string[] }>('/monitoring/metrics/label-values', { params: { label } }).then((r) => r.data),
}

// ── 日志查询（VictoriaLogs 代理） ──
// 契约与后端 _vlogs_get 归一化结果对齐：{id, time, message, fields} + 便捷字段
export interface LogLine {
  /** stream 标识（后端归一字段） */
  id?: string
  time?: string
  message?: string
  fields?: Record<string, unknown>
  // 便捷冗余字段（后端一并返回，tail 流也带）
  level?: string
  service?: string
  host?: string
  /** 兼容旧客户端：等价于 id */
  stream_id?: string
  /** 兼容原始 VLogs 行 */
  _time?: string
  _msg?: string
  _stream_id?: string
  [key: string]: unknown
}
export interface NormalizedLogEntry {
  id: string
  time: string
  level: string
  service: string
  host: string
  message: string
  /** 与 id 相同，上下文抽屉用 */
  stream_id: string
  fields?: Record<string, unknown>
}
export const logsApi = {
  query: (data: { query?: string; start?: string; end?: string; limit?: number; host_ips?: string[]; host_names?: string[] }) =>
    client.post<{ configured: boolean; message?: string; logs: LogLine[] }>('/monitoring/logs/query', data).then((r) => r.data),
  histogram: (data: { query?: string; start?: string; end?: string; step?: string; host_ips?: string[] }) =>
    // time 已由后端归一为 Unix 秒
    client.post<{ configured: boolean; buckets: { time: number; count: number }[] }>('/monitoring/logs/histogram', data).then((r) => r.data),
  context: (data: { stream_id: string; timestamp: string; before?: number; after?: number }) =>
    client.post<{ configured: boolean; before: LogLine[]; after: LogLine[] }>('/monitoring/logs/context', data).then((r) => r.data),
  /** 枚举标签字段名或字段取值（Grafana 式筛选） */
  fields: (data: { field?: string; start?: string; end?: string; query?: string }) =>
    client.post<{ configured: boolean; fields: string[]; values: string[] }>('/monitoring/logs/fields', data).then((r) => r.data),
  /** 中文描述 → LogsQL 建议（需已配置 LLM） */
  suggestQuery: (text: string) =>
    client.post<{ ok: boolean; query: string; message: string }>('/monitoring/logs/suggest-query', { text }).then((r) => r.data),
}

// ── 脚本执行（Ansible） ──
export const changesApi = {
  types: () => client.get('/changes/types').then((r) => r.data),
  list: (params?: { status?: string; change_type?: string; q?: string; page?: number; page_size?: number }) =>
    client.get('/changes', { params }).then((r) => r.data),
  get: (id: number) => client.get(`/changes/${id}`).then((r) => r.data),
  create: (data: {
    title: string
    change_type: string
    risk_level?: string
    resource_type?: string
    resource_id?: string
    resource_name?: string
    impact?: string
    rollback_plan?: string
    detail?: Record<string, unknown>
    window_start?: string
    window_end?: string
    approval_steps?: string[]
  }) => client.post('/changes', data).then((r) => r.data),
  approve: (id: number, note?: string) =>
    client.post(`/changes/${id}/approve`, { note: note || '' }).then((r) => r.data),
  reject: (id: number, note?: string) =>
    client.post(`/changes/${id}/reject`, { note: note || '' }).then((r) => r.data),
  cancel: (id: number, note?: string) =>
    client.post(`/changes/${id}/cancel`, { note: note || '' }).then((r) => r.data),
  execute: (id: number, manualResult?: boolean) =>
    client.post(`/changes/${id}/execute`, { manual_result: manualResult ?? null }).then((r) => r.data),
}

export const toolsApi = {
  getConfig: () => client.get<import('@/types/api').AnsibleConfig>('/tools/ansible/config').then((r) => r.data),
  updateConfig: (data: import('@/types/api').AnsibleConfigPayload) =>
    client.put<import('@/types/api').AnsibleConfig>('/tools/ansible/config', data).then((r) => r.data),
  listTargets: () =>
    client.get<{ targets: import('@/types/api').AnsibleTarget[] }>('/tools/ansible/targets').then((r) => r.data.targets),
  listHistory: () =>
    client.get<{ items: import('@/types/api').AnsibleHistoryEntry[] }>('/tools/ansible/history').then((r) => r.data.items),
  listTemplates: () =>
    client.get<{ items: import('@/types/api').AnsibleTemplateItem[] }>('/tools/ansible/templates').then((r) => r.data.items),
  listRoleOptions: () =>
    client.get<import('@/types/api').AnsibleRoleOption[]>('/tools/ansible/role-options').then((r) => r.data),
  createTemplate: (data: import('@/types/api').AnsibleTemplatePayload) =>
    client.post<import('@/types/api').AnsibleTemplateItem>('/tools/ansible/templates', data).then((r) => r.data),
  updateTemplate: (templateId: string, data: import('@/types/api').AnsibleTemplatePayload) =>
    client.put<import('@/types/api').AnsibleTemplateItem>(`/tools/ansible/templates/${templateId}`, data).then((r) => r.data),
  deleteTemplate: (templateId: string) => client.delete(`/tools/ansible/templates/${templateId}`).then((r) => r.data),
  runTask: (data: {
    run_all?: boolean
    asset_ids?: string[]
    module?: string
    module_args?: string
    become?: boolean
    become_user?: string
  }) => client.post<import('@/types/api').AnsibleRunResult>('/tools/ansible/run', data).then((r) => r.data),
  runPlaybook: (data: { run_all?: boolean; asset_ids?: string[]; playbook: string }) =>
    client.post<import('@/types/api').AnsibleRunResult>('/tools/ansible/run-playbook', data).then((r) => r.data),
  runTemplate: (data: { template_id: string; run_all_override?: boolean; asset_ids_override?: string[] }) =>
    client.post<import('@/types/api').AnsibleRunResult>('/tools/ansible/run-template', data).then((r) => r.data),
}

// ── 首页驾驶舱聚合 ──
export interface AlertStatsBucket { key: string; count: number }
export const cockpitApi = {
  alertStats: (windowHours = 24) =>
    client.get<{
      window_hours: number
      active_count: number
      triggered_count: number
      resolved_count: number
      by_severity: AlertStatsBucket[]
      hourly_trend: number[]
    }>('/monitoring/alerts/stats', { params: { window_hours: windowHours } }).then((r) => r.data),
}

// ── 预算 / 实例费用 / 实时大盘 ──
export const financeExtraApi = {
  getBudget: () => client.get<import('@/types/api').BudgetConfig>('/finance/budget').then((r) => r.data),
  updateBudget: (data: import('@/types/api').BudgetPayload) =>
    client.put<import('@/types/api').BudgetConfig>('/finance/budget', data).then((r) => r.data),
  getInstanceCost: (instanceId: string, months = 6) =>
    client.get<import('@/types/api').InstanceCostSummary>(`/finance/instance/${encodeURIComponent(instanceId)}/cost`, { params: { months } }).then((r) => r.data),
}

export const liveDashboardApi = {
  overview: (params?: { host_ips?: string; host_names?: string; range_minutes?: number; step_seconds?: number }) =>
    client.get<import('@/types/api').MonitoringDashboardResponse>('/monitoring/dashboard', { params }).then((r) => r.data),
}

// ── K8s 集群详情 ──
export const clusterDetailApi = {
  refresh: (cid: string) => client.post<ClusterResponse>(`/clusters/${cid}/refresh`).then((r) => r.data),
  healthSummary: (cid: string) => client.get<K8sHealthSummary>(`/clusters/${cid}/health-summary`).then((r) => r.data),
  componentsHealth: (cid: string) =>
    client.get<K8sComponentHealth[]>(`/clusters/${cid}/components/health`).then((r) => r.data),
  // 节点
  nodes: (cid: string) => client.get<K8sNode[]>(`/clusters/${cid}/nodes`).then((r) => r.data),
  nodeDrainPreview: (cid: string, name: string) =>
    client.get<K8sNodeDrainPreview>(`/clusters/${cid}/nodes/${name}/drain-preview`).then((r) => r.data),
  nodeDrain: (cid: string, name: string, data?: { force?: boolean; delete_emptydir_data?: boolean; grace_period?: number }) =>
    client.post(`/clusters/${cid}/nodes/${name}/drain`, data ?? {}).then((r) => r.data),
  nodeCordon: (cid: string, name: string) => client.post(`/clusters/${cid}/nodes/${name}/cordon`).then((r) => r.data),
  nodeUncordon: (cid: string, name: string) => client.post(`/clusters/${cid}/nodes/${name}/uncordon`).then((r) => r.data),
  nodeMetrics: (cid: string) =>
    client.get<{ available: boolean; message?: string; items: Array<{ name: string; cpu_millicores: number; memory_bytes: number }> }>(`/clusters/${cid}/nodes/metrics`).then((r) => r.data),
  nodeDetail: (cid: string, name: string) => client.get<K8sNodeDetail>(`/clusters/${cid}/nodes/${name}`).then((r) => r.data),
  // 工作负载
  workloads: (cid: string, namespace = '_all') =>
    client.get<K8sWorkload[]>(`/clusters/${cid}/workloads`, { params: { namespace } }).then((r) => r.data),
  scaleWorkload: (cid: string, kind: string, ns: string, name: string, replicas: number) =>
    client.patch(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/scale`, { replicas }).then((r) => r.data),
  restartWorkload: (cid: string, kind: string, ns: string, name: string) =>
    client.patch(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/restart`).then((r) => r.data),
  rolloutHistory: (cid: string, kind: string, ns: string, name: string) =>
    client.get<K8sRolloutRevision[]>(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/rollout-history`).then((r) => r.data),
  rolloutUndo: (cid: string, kind: string, ns: string, name: string, data?: { revision?: number }) =>
    client.post(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/rollout-undo`, data ?? {}).then((r) => r.data),
  deleteWorkload: (cid: string, kind: string, ns: string, name: string) =>
    client.delete(`/clusters/${cid}/workloads/${kind}/${ns}/${name}`).then((r) => r.data),
  // Pod
  pods: (cid: string, namespace = '_all') =>
    client.get<K8sPod[]>(`/clusters/${cid}/pods`, { params: { namespace } }).then((r) => r.data),
  namespaces: (cid: string) =>
    client.get<Array<{ name: string; status: string; age: string }>>(`/clusters/${cid}/namespaces`).then((r) => (Array.isArray(r.data) ? r.data : (r.data as { items?: Array<{ name: string; status: string; age: string }> }).items || [])),
  podDetail: (cid: string, ns: string, name: string) => client.get<Record<string, unknown>>(`/clusters/${cid}/pods/${ns}/${name}`).then((r) => r.data),
  createLogsSession: (cid: string, ns: string, name: string, params: { container?: string; tail?: number; previous?: boolean }) =>
    client.post<{ session_id: string; session_token: string; ws_path: string }>(
      `/clusters/${cid}/pods/${ns}/${name}/logs/session`, null, { params }).then((r) => r.data),
  podDiagnostics: (cid: string, ns: string, name: string) =>
    client.get(`/clusters/${cid}/pods/${ns}/${name}/diagnostics`).then((r) => r.data),
  deletePod: (cid: string, ns: string, name: string) =>
    client.delete(`/clusters/${cid}/pods/${ns}/${name}`).then((r) => r.data),
  // 通用资源
  resources: (cid: string, kind: string, namespace = '_all', limit = 200) =>
    client.get<K8sResourceItem[]>(`/clusters/${cid}/resources`, { params: { kind, namespace, limit } })
      .then((r) => (Array.isArray(r.data) ? r.data : (r.data as { items?: K8sResourceItem[] }).items || [])),
  // continue_token 分页版（paginated=true envelope），供资源浏览 Tab 无限翻页。
  resourcesPage: (cid: string, kind: string, namespace = '_all', limit = 100, continueToken = '') =>
    client.get<{ items: K8sResourceItem[]; continue_token: string; limit: number; cached?: boolean }>(
      `/clusters/${cid}/resources`,
      { params: { kind, namespace, limit, continue_token: continueToken || undefined, paginated: true } },
    ).then((r) => (
      Array.isArray(r.data)
        ? { items: r.data, continue_token: '', limit: r.data.length }
        : { items: r.data?.items ?? [], continue_token: r.data?.continue_token ?? '', limit: r.data?.limit ?? 0 }
    )),
  resourceYaml: (cid: string, ns: string, kind: string, name: string) =>
    client.get<{ yaml?: string; content?: string } | string>(`/clusters/${cid}/resources/${ns}/${kind}/${name}/yaml`).then((r) => r.data),
  resourceEvents: (cid: string, ns: string, kind: string, name: string) =>
    client.get<K8sResourceEvent[]>(`/clusters/${cid}/resources/${ns}/${kind}/${name}/events`).then((r) => (Array.isArray(r.data) ? r.data : (r.data as { events?: K8sResourceEvent[] }).events || [])),
  deleteResource: (cid: string, ns: string, kind: string, name: string) =>
    client.delete(`/clusters/${cid}/resources/${ns}/${kind}/${name}`).then((r) => r.data),
}

// ── K8s 集群管理补全（exec/YAML/命名空间/审计/搜索/镜像/污点等） ──
export const clusterOpsApi = {
  // Pod 终端
  createExecSession: (cid: string, ns: string, name: string, params: { container?: string; command?: string }) =>
    client.post<{ session_id: string; session_token: string; ws_path: string }>(
      `/clusters/${cid}/pods/${ns}/${name}/exec/session`, null, { params }).then((r) => r.data),
  // YAML 工作台
  dryRun: (cid: string, yamlContent: string) =>
    client.post(`/clusters/${cid}/dry-run`, { yaml_content: yamlContent }).then((r) => r.data),
  apply: (cid: string, yamlContent: string) =>
    client.post(`/clusters/${cid}/apply`, { yaml_content: yamlContent }).then((r) => r.data),
  // 命名空间
  createNamespace: (cid: string, data: { name: string; labels?: Record<string, string>; quota_preset?: string | null }) =>
    client.post(`/clusters/${cid}/namespaces`, data).then((r) => r.data),
  namespaceUsage: (cid: string, ns: string) =>
    client.get<unknown>(`/clusters/${cid}/namespaces/${ns}/usage`).then((r) => r.data),
  deleteNamespace: (cid: string, ns: string) =>
    client.delete<{ message: string }>(`/clusters/${cid}/namespaces/${ns}`).then((r) => r.data),
  patchNamespaceLabels: (cid: string, ns: string, labels: Record<string, string | null>) =>
    client.patch<{ message: string; applied: Record<string, string> }>(`/clusters/${cid}/namespaces/${ns}/labels`, { labels }).then((r) => r.data),
  // 审计与搜索
  auditLogs: (cid: string, params: {
    limit?: number
    offset?: number
    user?: string
    action?: string
    result?: string
    risk_level?: string
    q?: string
    time_from?: string
    time_to?: string
  } = {}) =>
    client.get<{ items: ClusterAuditLog[]; total: number }>(
      `/clusters/${cid}/audit-logs`, { params }).then((r) => (
        Array.isArray(r.data)
          ? { items: r.data, total: r.data.length }
          : { items: r.data?.items ?? [], total: r.data?.total ?? 0 }
      )),
  search: (cid: string, q: string, namespace = '_all', kinds?: string[]) =>
    client.get<K8sSearchHit[]>(`/clusters/${cid}/search`, {
      params: { q, namespace, kinds: kinds?.length ? kinds.join(',') : undefined },
    }).then((r) => r.data),
  // 节点标签/污点（labels 值为 null 表示删除；taints 为替换式全量）。
  // 两条 PATCH 对应后端两个守卫动作（patch_labels / patch_taints），票据一次性消费，
  // 保存时通过 config 显式透传各自的 X-Risk-Ticket 头（见 NodeMetaEditor）。
  patchNodeLabels: (cid: string, name: string, labels: Record<string, string | null>, config?: AxiosRequestConfig) =>
    client.patch(`/clusters/${cid}/nodes/${name}/labels`, { labels }, config).then((r) => r.data),
  patchNodeTaints: (cid: string, name: string, taints: Array<{ key: string; value?: string; effect: string }>, config?: AxiosRequestConfig) =>
    client.patch(`/clusters/${cid}/nodes/${name}/taints`, { taints }, config).then((r) => r.data),
  // 工作负载增强
  setWorkloadImage: (cid: string, kind: string, ns: string, name: string, images: Array<{ name: string; image: string }>) =>
    client.patch(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/set-image`, { images }).then((r) => r.data),
  toggleWorkloadPause: (cid: string, kind: string, ns: string, name: string, paused: boolean) =>
    client.patch(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/pause`, { paused }).then((r) => r.data),
  rolloutStatus: (cid: string, kind: string, ns: string, name: string) =>
    client.get<K8sRolloutStatus>(`/clusters/${cid}/workloads/${kind}/${ns}/${name}/rollout-status`).then((r) => r.data),
  // CronJob
  triggerCronjob: (cid: string, ns: string, name: string) =>
    client.post(`/clusters/${cid}/cronjobs/${ns}/${name}/trigger`).then((r) => r.data),
  suspendCronjob: (cid: string, ns: string, name: string, suspend: boolean) =>
    client.patch(`/clusters/${cid}/cronjobs/${ns}/${name}/suspend`, null, { params: { suspend } }).then((r) => r.data),
  cronjobJobs: (cid: string, ns: string, name: string) =>
    client.get<K8sCronJobRun[]>(`/clusters/${cid}/cronjobs/${ns}/${name}/jobs`).then((r) => r.data),
  // 资源增强
  resizePvc: (cid: string, ns: string, name: string, newSize: string) =>
    client.patch(`/clusters/${cid}/pvc/${ns}/${name}/resize`, null, { params: { new_size: newSize } }).then((r) => r.data),
  ingressTlsStatus: (cid: string, ns: string, name: string) =>
    client.get<IngressTlsStatusItem>(`/clusters/${cid}/ingress/${ns}/${name}/tls-status`).then((r) => r.data),
  resourceEditForm: (cid: string, ns: string, kind: string, name: string) =>
    client.get(`/clusters/${cid}/resources/${ns}/${kind}/${name}/edit`).then((r) => r.data),
  patchResourceEdit: (cid: string, ns: string, kind: string, name: string, body: Record<string, unknown>) =>
    client.patch(`/clusters/${cid}/resources/${ns}/${kind}/${name}/edit`, body).then((r) => r.data),
  resourceRelated: (cid: string, ns: string, kind: string, name: string) =>
    client.get<unknown>(`/clusters/${cid}/resources/${ns}/${kind}/${name}/related`).then((r) => r.data),
  // SSE 实时 Watch（token 走 URL，流由组件自行 fetch）
  createWatchSession: (cid: string) =>
    client.post<{ token: string; expires_at: number }>(`/clusters/${cid}/watch/session`).then((r) => r.data),
}

// ── WireGuard 虚拟组网 ──
export const wireguardApi = {
  listNetworks: () => client.get<import('@/types/api').WgNetwork[]>('/wireguard/networks').then((r) => r.data),
  createNetwork: (data: import('@/types/api').WgNetworkPayload) =>
    client.post<import('@/types/api').WgNetwork>('/wireguard/networks', data).then((r) => r.data),
  updateNetwork: (id: number, data: Partial<import('@/types/api').WgNetworkPayload>) =>
    client.put<import('@/types/api').WgNetwork>(`/wireguard/networks/${id}`, data).then((r) => r.data),
  deleteNetwork: (id: number) => client.delete(`/wireguard/networks/${id}`).then((r) => r.data),
  byAsset: (assetId: string) =>
    client.get<Record<string, unknown>>(`/wireguard/by-asset/${encodeURIComponent(assetId)}`).then((r) => r.data),
  networkStatus: (id: number) =>
    // 页面侧 statusMap 依赖 any 推断（Object.fromEntries 兜底 {}），此处保留动态结构
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    client.get<any>(`/wireguard/networks/${id}/status`).then((r) => r.data),
  serverConfig: (id: number) =>
    client.get<{ network: import('@/types/api').WgNetwork; config: string }>(`/wireguard/networks/${id}/server-config`).then((r) => r.data),
  serverInstallScript: (id: number) =>
    client.get<{ script: string }>(`/wireguard/networks/${id}/server-install-script`).then((r) => r.data),
  listPeers: (networkId: number) =>
    client.get<import('@/types/api').WgPeer[]>(`/wireguard/networks/${networkId}/peers`).then((r) => r.data),
  createPeer: (networkId: number, data: import('@/types/api').WgPeerPayload) =>
    client.post<import('@/types/api').WgPeer>(`/wireguard/networks/${networkId}/peers`, data).then((r) => r.data),
  updatePeer: (peerId: number, data: { enabled?: boolean; remark?: string }) =>
    client.patch<import('@/types/api').WgPeer>(`/wireguard/peers/${peerId}`, data).then((r) => r.data),
  deletePeer: (peerId: number) => client.delete(`/wireguard/peers/${peerId}`).then((r) => r.data),
  peerBundle: (peerId: number) =>
    client.get<import('@/types/api').WgPeerBundle>(`/wireguard/peers/${peerId}/config`).then((r) => r.data),
}

// ── 数据库管理（SQL 工单平台） ──
export const dbApi = {
  listInstances: () => client.get<import('@/types/api').DbInstance[]>('/db/instances').then((r) => r.data),
  createInstance: (data: Record<string, unknown>) =>
    client.post<import('@/types/api').DbInstance>('/db/instances', data).then((r) => r.data),
  updateInstance: (id: number, data: Record<string, unknown>) =>
    client.put<import('@/types/api').DbInstance>(`/db/instances/${id}`, data).then((r) => r.data),
  deleteInstance: (id: number) => client.delete(`/db/instances/${id}`).then((r) => r.data),
  testInstance: (id: number) => client.post<{ ok: boolean; message: string }>(`/db/instances/${id}/test`).then((r) => r.data),
  listTickets: (params?: { status?: string; instance_id?: number; limit?: number }) =>
    client.get<import('@/types/api').DbTicket[]>('/db/tickets', { params }).then((r) => r.data),
  createTicket: (data: import('@/types/api').DbTicketCreatePayload) =>
    client.post<import('@/types/api').DbTicket>('/db/tickets', data).then((r) => r.data),
  reviewTicket: (id: number, data: { decision: 'approve' | 'reject'; note?: string }) =>
    client.post<import('@/types/api').DbTicket>(`/db/tickets/${id}/review`, data).then((r) => r.data),
  executeTicket: (id: number) => client.post<import('@/types/api').DbTicket>(`/db/tickets/${id}/execute`).then((r) => r.data),
  runQuery: (data: { instance_id: number; sql_text: string; database?: string }) =>
    client.post<import('@/types/api').DbQueryResult>('/db/query', data).then((r) => r.data),
  listQueryLogs: (limit = 50) =>
    client.get<import('@/types/api').DbQueryLogItem[]>('/db/query-logs', { params: { limit } }).then((r) => r.data),
  // 库表浏览（选数据源带出库/表）
  listDatabases: (id: number) =>
    client.get<{ databases: string[]; default_database?: string }>(`/db/instances/${id}/databases`)
      .then((r) => r.data),
  listTables: (id: number, database: string) =>
    client.get<{ database: string; tables: Array<{ name: string; rows: number }> }>(`/db/instances/${id}/tables`, { params: { database } }).then((r) => r.data.tables || []),
  // 审核规则配置
  getAuditRules: () =>
    client.get<{ rules: Array<{ rule: string; name: string; default_level: string; enabled: boolean; level: string }> }>('/db/audit-rules').then((r) => r.data),
  updateAuditRules: (rules: Array<{ rule: string; enabled: boolean; level: string }>) =>
    client.put('/db/audit-rules', { rules }).then((r) => r.data),
  // 审批流配置（多级审批链，按工单类型）
  getApprovalFlows: () =>
    client.get<import('@/types/api').DbApprovalFlows>('/db/approval-flows').then((r) => r.data),
  updateApprovalFlows: (data: { change?: { steps: string[] }; query?: { steps: string[] } }) =>
    client.put<import('@/types/api').DbApprovalFlows>('/db/approval-flows', data).then((r) => r.data),
  // 查询授权（工单终审自动生成 / 管理员手动授权）
  listQueryGrants: (scope: 'mine' | 'all' = 'mine') =>
    client.get<import('@/types/api').DbQueryGrant[]>('/db/query-grants', { params: { scope } }).then((r) => r.data),
  createQueryGrant: (data: { username: string; instance_id: number; database: string; tables?: string[]; hours: number; reason?: string }) =>
    client.post<import('@/types/api').DbQueryGrant>('/db/query-grants', data).then((r) => r.data),
  revokeQueryGrant: (id: number) =>
    client.post<import('@/types/api').DbQueryGrant>(`/db/query-grants/${id}/revoke`).then((r) => r.data),
  // 数据字典：表结构 / 索引 / DDL
  getTableSchema: (id: number, database: string, table: string) =>
    client.get<import('@/types/api').DbTableSchema>(`/db/instances/${id}/tables/${encodeURIComponent(table)}/schema`, { params: { database } }).then((r) => r.data),
}
