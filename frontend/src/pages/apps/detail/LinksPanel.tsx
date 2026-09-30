import { Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { LinkOutlined } from '@ant-design/icons'
import type { ApplicationLinkItem } from '@/types/api'

export default function LinksPanel({ links }: { links: ApplicationLinkItem[] }) {
  const linkColumns: ColumnsType<ApplicationLinkItem> = [
    { title: '标题', dataIndex: 'title', width: 160, render: (v) => <span style={{ fontWeight: 500 }}>{v}</span> },
    { title: '类型', dataIndex: 'link_type', width: 100, render: (v) => <Tag>{v || 'external'}</Tag> },
    { title: '地址', dataIndex: 'url', render: (v) => <a href={v} target="_blank" rel="noreferrer">{v} <LinkOutlined /></a> },
    { title: '添加时间', dataIndex: 'created_at', width: 160, render: (v) => v?.slice(0, 16) || '-' },
  ]

  return (
    <Table<ApplicationLinkItem> rowKey="id" size="middle" dataSource={links} columns={linkColumns} pagination={false} locale={{ emptyText: '暂无快捷链接' }} />
  )
}
