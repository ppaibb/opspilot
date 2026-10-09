import type { ReactNode } from 'react'
import { Tag } from 'antd'

/**
 * 集群核心命名空间，禁止删除。与后端 `app/api/clusters/namespaces.py` 的
 * PROTECTED_NAMESPACES 保持一致；后端是权威校验，这里是前置拦截，
 * 避免用户连点两次确认后才被 403 拒绝。
 */
export const PROTECTED_NAMESPACES = ['kube-system', 'kube-public', 'kube-node-lease', 'default']

export function statusTag(status: string): ReactNode {
  const s = (status || '').toLowerCase()
  const color = ['running', 'healthy', 'ready', 'complete', 'active', 'bound'].includes(s) ? 'green'
    : ['failed', 'error', 'crashloopbackoff', 'degraded', 'unhealthy', 'evicted'].includes(s) ? 'red'
      : ['pending', 'terminating', 'containercreating', 'init'].includes(s) ? 'orange' : 'default'
  return <Tag color={color}>{status || '-'}</Tag>
}
