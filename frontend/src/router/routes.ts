/**
 * 全局唯一路由元数据源。
 *
 * - 页面组件以懒加载工厂（load）声明，由 router/index.tsx 统一经 React.lazy 包装，
 *   按路由代码分割；
 * - 菜单树（menuConfig）、面包屑、全局搜索、路由表统一从 appRoutes 派生，
 *   新增页面只需在这里加一条记录；
 * - permission 引用 backend/app/core/rbac.py 中真实存在的模块权限键，
 *   没有对应键的页面省略该字段（不设防，登录即可访问）。
 */
import type { ComponentType } from 'react'

/** 页面懒加载工厂（传给 React.lazy） */
export type LoadPage = () => Promise<{ default: ComponentType }>

/** 单条路由元数据 */
export interface AppRouteMeta {
  /** 完整路径（以 / 开头，'/' 表示首页/仪表盘） */
  path: string
  /** 页面懒加载工厂 */
  load: LoadPage
  /** 菜单 / 面包屑 / 搜索显示名 */
  label: string
  /** 所属分组中文名（如「资产管理」），一级页面为空串 */
  group: string
  /** 所属分组 key（如 'assets'，与菜单图标映射、面包屑分组对应） */
  groupKey?: string
  /** 是否出现在侧边栏菜单（详情页等深链路由为 false） */
  menu: boolean
  /** RBAC 模块权限键（backend/app/core/rbac.py），缺省表示不设防 */
  permission?: string
}

// ── 页面懒加载工厂 ──
const loadLogin: LoadPage = () => import('@/pages/login')
const loadDashboard: LoadPage = () => import('@/pages/dashboard')
const loadHosts: LoadPage = () => import('@/pages/assets/hosts')
const loadVms: LoadPage = () => import('@/pages/assets/vms')
const loadCredentials: LoadPage = () => import('@/pages/assets/credentials')
const loadDomains: LoadPage = () => import('@/pages/assets/domains')
const loadCertificates: LoadPage = () => import('@/pages/assets/certificates')
const loadCmdb: LoadPage = () => import('@/pages/assets/cmdb')
const loadClusters: LoadPage = () => import('@/pages/assets/clusters')
const loadClusterDetail: LoadPage = () => import('@/pages/assets/clusters/detail')
const loadWireGuard: LoadPage = () => import('@/pages/assets/wireguard')
const loadDatabases: LoadPage = () => import('@/pages/assets/databases')
const loadNotifications: LoadPage = () => import('@/pages/notifications')
const loadUsers: LoadPage = () => import('@/pages/settings/users')
const loadPermissions: LoadPage = () => import('@/pages/settings/permissions')
const loadAudit: LoadPage = () => import('@/pages/settings/audit')
const loadSystem: LoadPage = () => import('@/pages/settings/system')
const loadPersonalIm: LoadPage = () => import('@/pages/settings/im')
const loadScheduledTasks: LoadPage = () => import('@/pages/settings/scheduled-tasks')
const loadAppsList: LoadPage = () => import('@/pages/apps')
const loadAppDetail: LoadPage = () => import('@/pages/apps/detail')
const loadAppConfig: LoadPage = () => import('@/pages/apps/config')
const loadConfigCenter: LoadPage = () => import('@/pages/apps/config-center')
const loadDeployment: LoadPage = () => import('@/pages/apps/deployment')
const loadMonitoringDashboard: LoadPage = () => import('@/pages/monitoring/dashboard')
const loadMetricsQuery: LoadPage = () => import('@/pages/monitoring/metrics')
const loadAlertRules: LoadPage = () => import('@/pages/monitoring/alert-rules')
const loadAlerts: LoadPage = () => import('@/pages/monitoring/alerts')
const loadLogsQuery: LoadPage = () => import('@/pages/monitoring/logs')
const loadCollectors: LoadPage = () => import('@/pages/monitoring/collectors')
const loadAiWorkbench: LoadPage = () => import('@/pages/ai/workbench')
const loadAIDashboard: LoadPage = () => import('@/pages/ai/dashboard')
const loadAISkills: LoadPage = () => import('@/pages/ai/skills')

const loadLLMConfig: LoadPage = () => import('@/pages/ai/llm-config')
const loadScriptExecute: LoadPage = () => import('@/pages/tools/script')
const loadChangeTickets: LoadPage = () => import('@/pages/tools/changes')
const loadFinanceOverview: LoadPage = () => import('@/pages/finance/overview')
const loadFinanceBills: LoadPage = () => import('@/pages/finance/bills')
const loadFinanceAccounts: LoadPage = () => import('@/pages/finance/accounts')

/**
 * 全部路由（顺序即菜单顺序）。
 * 权限键均取自 backend/app/core/rbac.py 的 MODULE_PERMISSION_CATALOG。
 */
export const appRoutes: AppRouteMeta[] = [
  // ── 工作台 ──
  { path: '/', load: loadDashboard, label: '仪表盘', group: '', menu: true, permission: 'module.dashboard' },

  // ── 资产管理 ──
  { path: '/assets/hosts', load: loadHosts, label: '主机管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.hosts' },
  { path: '/assets/vms', load: loadVms, label: '虚拟机管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.vms' },
  { path: '/assets/clusters', load: loadClusters, label: '集群管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.clusters' },
  { path: '/assets/clusters/:clusterId', load: loadClusterDetail, label: '集群详情', group: '资产管理', groupKey: 'assets', menu: false, permission: 'module.assets.clusters' },
  { path: '/assets/credentials', load: loadCredentials, label: '凭证管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.credentials' },
  { path: '/assets/domains', load: loadDomains, label: '域名管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.domains' },
  { path: '/assets/certificates', load: loadCertificates, label: '证书管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.certificates' },
  { path: '/assets/cmdb', load: loadCmdb, label: '关联图谱', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.apps.list' },
  { path: '/assets/wireguard', load: loadWireGuard, label: '虚拟组网', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.wireguard.manage' },
  { path: '/assets/databases', load: loadDatabases, label: '数据库管理', group: '资产管理', groupKey: 'assets', menu: true, permission: 'module.assets.databases' },

  // ── AI 能力 ──
  { path: '/ai/workbench', load: loadAiWorkbench, label: 'Agent 工作台', group: 'AI 能力', groupKey: 'ai', menu: true, permission: 'module.ai.ops' },
  // RBAC 无 module.ai.dashboard 键，不设防
  { path: '/ai/dashboard', load: loadAIDashboard, label: 'AI 全景看板', group: 'AI 能力', groupKey: 'ai', menu: true },
  { path: '/ai/skills', load: loadAISkills, label: '技能管理', group: 'AI 能力', groupKey: 'ai', menu: true, permission: 'module.ai.skills' },
  { path: '/ai/llm-config', load: loadLLMConfig, label: '模型配置', group: 'AI 能力', groupKey: 'ai', menu: true, permission: 'module.ai.llm' },

  // ── 应用管理 ──
  { path: '/apps', load: loadAppsList, label: '应用列表', group: '应用管理', groupKey: 'apps', menu: true, permission: 'module.apps.list' },
  { path: '/apps/:appId', load: loadAppDetail, label: '应用详情', group: '应用管理', groupKey: 'apps', menu: false, permission: 'module.apps.list' },
  { path: '/apps/:appId/config', load: loadAppConfig, label: '应用配置', group: '应用管理', groupKey: 'apps', menu: false, permission: 'module.apps.config' },
  { path: '/apps/deployment', load: loadDeployment, label: '部署管理', group: '应用管理', groupKey: 'apps', menu: true, permission: 'module.apps.deployment' },
  { path: '/apps/config-center', load: loadConfigCenter, label: '配置中心', group: '应用管理', groupKey: 'apps', menu: true, permission: 'module.apps.config' },

  // ── 监控中心 ──
  { path: '/monitoring/live', load: loadMonitoringDashboard, label: '实时看板', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.live' },
  { path: '/monitoring/metrics', load: loadMetricsQuery, label: '指标查询', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.metrics' },
  { path: '/monitoring/alert-rules', load: loadAlertRules, label: '告警规则', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.rules' },
  { path: '/monitoring/alerts', load: loadAlerts, label: '告警中心', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.alerts' },
  { path: '/monitoring/logs', load: loadLogsQuery, label: '日志查询', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.logs' },
  { path: '/monitoring/collectors', load: loadCollectors, label: '采集车队', group: '监控中心', groupKey: 'monitoring', menu: true, permission: 'module.monitoring.collector' },

  // ── 费用管理 ──
  { path: '/finance/overview', load: loadFinanceOverview, label: '费用总览', group: '费用管理', groupKey: 'finance', menu: true, permission: 'module.finance.overview' },
  { path: '/finance/bills', load: loadFinanceBills, label: '账单明细', group: '费用管理', groupKey: 'finance', menu: true, permission: 'module.finance.bills' },
  { path: '/finance/accounts', load: loadFinanceAccounts, label: '云账号', group: '费用管理', groupKey: 'finance', menu: true, permission: 'module.finance.accounts' },

  // ── 运维工具 ──
  { path: '/tools/script', load: loadScriptExecute, label: '脚本执行', group: '运维工具', groupKey: 'tools', menu: true, permission: 'module.tools.scripts' },
  { path: '/tools/changes', load: loadChangeTickets, label: '变更工单', group: '运维工具', groupKey: 'tools', menu: true, permission: 'module.tools.changes' },

  // ── 系统设置 ──
  { path: '/settings/system', load: loadSystem, label: '系统管理', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.system' },
  // 个人 IM / 定时任务 / 权限管理的 RBAC 键见 PERMISSION_CATALOG（个人 IM 为自助能力，模板默认全员开）
  { path: '/settings/im', load: loadPersonalIm, label: '个人 IM 通知', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.im' },
  { path: '/settings/scheduled-tasks', load: loadScheduledTasks, label: '定时任务', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.scheduled' },
  { path: '/settings/users', load: loadUsers, label: '用户管理', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.users' },
  { path: '/settings/permissions', load: loadPermissions, label: '权限管理', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.permissions' },
  { path: '/settings/audit', load: loadAudit, label: '审计日志', group: '系统设置', groupKey: 'settings', menu: true, permission: 'module.settings.audit' },

  // ── 站内通知（不在侧边栏，铃铛「查看全部」跳入） ──
  { path: '/notifications', load: loadNotifications, label: '通知中心', group: '', menu: false },
]

// ── 登录页单独导出（GuestGuard 包裹，不参与权限与菜单） ──
export const loginRoute: AppRouteMeta = {
  path: '/login',
  load: loadLogin,
  label: '登录',
  group: '',
  menu: false,
}
