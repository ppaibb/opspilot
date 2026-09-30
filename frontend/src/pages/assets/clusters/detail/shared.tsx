import type { ReactNode } from 'react'
import { Tag } from 'antd'

export function statusTag(status: string): ReactNode {
  const s = (status || '').toLowerCase()
  const color = ['running', 'healthy', 'ready', 'complete', 'active', 'bound'].includes(s) ? 'green'
    : ['failed', 'error', 'crashloopbackoff', 'degraded', 'unhealthy', 'evicted'].includes(s) ? 'red'
      : ['pending', 'terminating', 'containercreating', 'init'].includes(s) ? 'orange' : 'default'
  return <Tag color={color}>{status || '-'}</Tag>
}
