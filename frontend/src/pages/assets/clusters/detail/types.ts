/** 命名空间行（GET /clusters/{cid}/namespaces 返回元素）。 */
export interface NamespaceRow {
  name: string
  status: string
  age: string
}

/** 集群操作审计行：定义在 @/types/api（api 层也要用），此处转出保持原有导入路径。 */
export type { ClusterAuditLog } from '@/types/api'

export const TAB_KEYS = ['overview', 'nodes', 'workloads', 'pods', 'resources', 'namespaces', 'audit'] as const

/** 资源浏览 Tab 提供的常用 Kind（后端 RESOURCE_MAP 支持约 40 种，这里挑高频的）。
 *  与搜索跳转的目标 Tab 判定共用一份，避免两处清单漂移。 */
export const BROWSABLE_KINDS = [
  'Service', 'Ingress', 'ConfigMap', 'Secret', 'PVC', 'PV', 'ServiceAccount',
  'NetworkPolicy', 'ResourceQuota', 'LimitRange', 'EndpointSlice', 'CRD',
]
