import { useQuery } from '@tanstack/react-query'
import { Card, Table, Button, Space, Tag, Typography, Empty } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, FileProtectOutlined } from '@ant-design/icons'
import { clusterOpsApi } from '@/api'
import type { ClusterAuditLog } from './types'

const { Text } = Typography

// ───────────────────────── 操作审计 Tab ─────────────────────────

export default function AuditTab({ clusterId }: { clusterId: string }) {
  const { data: logs = [], isLoading, refetch } = useQuery({
    queryKey: ['cluster-audit', clusterId],
    queryFn: () => clusterOpsApi.auditLogs(clusterId, 100),
  })

  const RISK_COLOR: Record<string, string> = { high: 'red', medium: 'orange', low: 'default' }

  const columns: ColumnsType<ClusterAuditLog> = [
    { title: '时间', dataIndex: 'time', width: 175 },
    { title: '操作人', dataIndex: 'user', width: 100, render: (v: string) => <Text strong>{v || '-'}</Text> },
    { title: '动作', dataIndex: 'action', width: 110 },
    { title: '对象', dataIndex: 'resource', ellipsis: true },
    {
      title: '结果', dataIndex: 'result', width: 90,
      render: (v: string) => (v === 'success' || v === '成功' ? <Tag color="green">成功</Tag> : <Tag color="red">{v || '失败'}</Tag>),
    },
    {
      title: '风险', dataIndex: 'risk_level', width: 90,
      render: (v: string) => <Tag color={RISK_COLOR[v] || 'default'}>{v || 'low'}</Tag>,
    },
  ]

  return (
    <Card
      size="small"
      title={<Space><FileProtectOutlined />集群操作审计（本集群的全部管理操作）</Space>}
      extra={<Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>}
    >
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={logs}
        loading={isLoading}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: <Empty description="暂无审计记录" /> }}
      />
    </Card>
  )
}
