import {
  Flex, Table, Button, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined } from '@ant-design/icons'
import type { ApplicationResourceItem } from '@/types/api'
import { RESOURCE_TYPE_LABEL } from './constants'

export default function ResourcesPanel({
  resources,
  onOpenBindDrawer,
  onUnbindResource,
}: {
  resources: ApplicationResourceItem[]
  onOpenBindDrawer: () => void
  onUnbindResource: (resourceType: string, resourceId: string) => void
}) {
  const resourceColumns: ColumnsType<ApplicationResourceItem> = [
    { title: '资源类型', dataIndex: 'resource_type', width: 100, render: (v) => { const m = RESOURCE_TYPE_LABEL[v]; return m ? <Tag color={m.color} icon={m.icon}>{m.label}</Tag> : <Tag>{v}</Tag> } },
    { title: '资源名称', dataIndex: 'resource_name', render: (v, r) => v || r.resource_id },
    { title: '资源 ID', dataIndex: 'resource_id', render: (v) => <code style={{ fontSize: 12 }}>{v}</code> },
    { title: '绑定时间', dataIndex: 'created_at', width: 160, render: (v) => v?.slice(0, 16) || '-' },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_: unknown, record) => (
        <Button
          type="link"
          danger
          size="small"
          onClick={() => onUnbindResource(record.resource_type, record.resource_id)}
        >
          解绑
        </Button>
      ),
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" align="center">
        <span style={{ color: '#64748b', fontSize: 13 }}>共 {resources.length} 个绑定资源</span>
        <Button type="primary" icon={<PlusOutlined />} onClick={onOpenBindDrawer}>
          绑定资源
        </Button>
      </Flex>
      <Table<ApplicationResourceItem> rowKey="id" size="middle" dataSource={resources} columns={resourceColumns} pagination={false} locale={{ emptyText: '暂未绑定任何资源' }} />
    </Flex>
  )
}
