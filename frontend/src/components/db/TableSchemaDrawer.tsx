/**
 * 表结构抽屉（数据字典）——字段 / 索引 / 建表 DDL。
 *
 * 供库表浏览器与自助查询页复用：审批人核对字段语义、提交人写 SQL 时
 * 不用再开 Navicat。MySQL 展示原版 DDL；PG 无 SHOW CREATE TABLE，只给
 * 字段与索引定义。
 */
import { useQuery } from '@tanstack/react-query'
import { App, Button, Drawer, Empty, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { CopyOutlined } from '@ant-design/icons'
import { dbApi } from '@/api'

const { Text } = Typography

interface Props {
  open: boolean
  onClose: () => void
  instanceId: number | null
  database: string
  table: string
}

export default function TableSchemaDrawer({ open, onClose, instanceId, database, table }: Props) {
  const { message } = App.useApp()
  const enabled = open && !!instanceId && !!database && !!table

  const { data, isLoading } = useQuery({
    queryKey: ['db-table-schema', instanceId, database, table],
    queryFn: () => dbApi.getTableSchema(instanceId!, database, table),
    enabled,
    staleTime: 60 * 1000,
  })

  const copyDdl = async () => {
    if (!data?.ddl) return
    try {
      await navigator.clipboard.writeText(data.ddl)
      message.success('DDL 已复制')
    } catch {
      message.warning('浏览器不支持自动复制，请手动选择')
    }
  }

  const columnColumns: ColumnsType<NonNullable<typeof data>['columns'][number]> = [
    {
      title: '字段', dataIndex: 'name', width: 160,
      render: (v: string, r) => (
        <Space size={4}>
          <Text code style={{ fontSize: 12 }}>{v}</Text>
          {r.key === 'PRI' && <Tag color="gold" style={{ marginInlineEnd: 0 }}>PK</Tag>}
        </Space>
      ),
    },
    { title: '类型', dataIndex: 'type', width: 140, render: (v: string) => <Text style={{ fontSize: 12 }}>{v}</Text> },
    { title: '注释', dataIndex: 'comment', ellipsis: true, render: (v: string) => v || <Text type="secondary">-</Text> },
    { title: '可空', dataIndex: 'nullable', width: 70, render: (v: boolean) => (v ? <Text type="secondary">YES</Text> : 'NO') },
    { title: '默认值', dataIndex: 'default', width: 110, ellipsis: true, render: (v: string | null) => (v == null || v === '' ? <Text type="secondary">-</Text> : <Text code style={{ fontSize: 11 }}>{String(v)}</Text>) },
    { title: '额外', dataIndex: 'extra', width: 90, render: (v: string) => v || '-' },
  ]

  const indexColumns: ColumnsType<NonNullable<typeof data>['indexes'][number]> = [
    { title: '索引名', dataIndex: 'name', width: 180, render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text> },
    { title: '定义 / 字段', dataIndex: 'column', ellipsis: true, render: (_v, r) => <Text style={{ fontSize: 12 }}>{r.column || r.type}</Text> },
    { title: '唯一', dataIndex: 'unique', width: 70, render: (v: boolean) => (v ? <Tag color="blue">UNIQUE</Tag> : <Text type="secondary">否</Text>) },
  ]

  return (
    <Drawer
      title={
        <Space size={8}>
          <span>表结构</span>
          {table && <Text code>{database && `${database}.`}{table}</Text>}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={760}
    >
      {!enabled ? (
        <Empty description="未选择表" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : isLoading ? (
        <div style={{ textAlign: 'center', padding: 40 }}><Spin /></div>
      ) : data ? (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Table
            rowKey="name" size="small" columns={columnColumns} dataSource={data.columns}
            pagination={false} scroll={{ y: 320 }}
            title={() => <Text strong>字段（{data.columns.length}）</Text>}
          />
          {data.indexes.length > 0 && (
            <Table
              rowKey="name" size="small" columns={indexColumns} dataSource={data.indexes}
              pagination={false}
              title={() => <Text strong>索引（{data.indexes.length}）</Text>}
            />
          )}
          {data.ddl && (
            <div>
              <Space style={{ marginBottom: 6 }}>
                <Text strong>建表 DDL</Text>
                <Tooltip title="复制 CREATE TABLE 语句">
                  <Button size="small" type="text" icon={<CopyOutlined />} onClick={copyDdl}>复制</Button>
                </Tooltip>
              </Space>
              <pre
                style={{
                  margin: 0, padding: 12, maxHeight: 300, overflow: 'auto', borderRadius: 6,
                  background: '#f6f6f6', fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre',
                }}
              >
                {data.ddl}
              </pre>
            </div>
          )}
        </Space>
      ) : (
        <Empty description="表不存在或读取失败" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
    </Drawer>
  )
}
