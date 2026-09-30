/** 命名空间行（GET /clusters/{cid}/namespaces 返回元素）。 */
export interface NamespaceRow {
  name: string
  status: string
  age: string
}

/** 集群操作审计行（GET /clusters/{cid}/audit-logs 返回元素）。 */
export interface ClusterAuditLog {
  id: number
  time: string
  user: string
  action: string
  resource: string
  result: string
  risk_level: string
}

export const TAB_KEYS = ['overview', 'nodes', 'workloads', 'pods', 'resources', 'namespaces', 'audit'] as const
