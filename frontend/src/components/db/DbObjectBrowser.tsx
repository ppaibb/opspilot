/**
 * 数据库对象浏览器 —— 选实例 → 带出库列表 → 选库 → 带出表列表。
 *
 * 点击表名回调 onPickTable(db, table)，由宿主页面把 `db`.`table` 插入 SQL 编辑器；
 * 表名右侧按钮打开「表结构」抽屉（数据字典）。紧凑模式用于 Drawer 内并排布局。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Select, Tree, Typography, Spin, Empty, Input, Space, Tooltip } from 'antd'
import { DatabaseOutlined, TableOutlined, FileTextOutlined } from '@ant-design/icons'
import { dbApi } from '@/api'
import TableSchemaDrawer from './TableSchemaDrawer'

const { Text } = Typography

interface Props {
  instances: Array<{ id: number; name: string; db_type: string; database?: string }>
  instanceId: number | null
  onInstanceChange: (id: number) => void
  /** 当前选中库（受控可选；不传则组件内部自管）。 */
  database?: string
  onDatabaseChange?: (db: string) => void
  onPickTable: (db: string, table: string) => void
  compact?: boolean
}

export default function DbObjectBrowser({
  instances, instanceId, onInstanceChange, database, onDatabaseChange, onPickTable, compact,
}: Props) {
  const [innerDb, setInnerDb] = useState('')
  const [tableFilter, setTableFilter] = useState('')
  // 表结构抽屉：选中表后展示字段/索引/DDL（数据字典）。
  const [schemaTable, setSchemaTable] = useState('')
  const currentDb = database ?? innerDb

  const { data: dbList, isLoading: loadingDbs } = useQuery({
    queryKey: ['db-databases', instanceId],
    queryFn: () => dbApi.listDatabases(instanceId!),
    enabled: !!instanceId,
    staleTime: 60 * 1000,
  })
  const databases = dbList?.databases ?? []
  const defaultDatabase = dbList?.default_database ?? ''

  const { data: tables = [], isFetching: loadingTables } = useQuery({
    queryKey: ['db-tables', instanceId, currentDb],
    queryFn: () => dbApi.listTables(instanceId!, currentDb),
    enabled: !!instanceId && !!currentDb,
    staleTime: 60 * 1000,
  })

  const filteredTables = tables.filter((t) => t.name.toLowerCase().includes(tableFilter.toLowerCase()))

  return (
    <Card
      size="small"
      title={<Space size={6}><DatabaseOutlined style={{ color: '#1677ff' }} /><span>库与表</span></Space>}
      styles={{ body: { padding: compact ? 8 : 12 } }}
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Select
          value={instanceId ?? undefined}
          onChange={(v) => { onInstanceChange(v); setInnerDb(''); onDatabaseChange?.('') }}
          placeholder="选择实例"
          style={{ width: '100%' }}
          showSearch optionFilterProp="label"
          loading={false}
          options={instances.map((i) => ({ value: i.id, label: `${i.name}（${i.db_type}）` }))}
        />
        <Select
          value={currentDb || undefined}
          onChange={(v) => { setInnerDb(v); onDatabaseChange?.(v) }}
          placeholder={instanceId ? '选择库' : '先选实例'}
          style={{ width: '100%' }}
          showSearch optionFilterProp="value"
          loading={loadingDbs}
          disabled={!instanceId}
          options={databases.map((d) => ({
            value: d,
            // 标出实例默认库：自助查询/工单不显式选库时走的就是它，
            // 用户知道哪个是"本来的库"，才不会误连到旁边的库上去。
            label: d === defaultDatabase ? `${d}（默认）` : d,
          }))}
        />
        {currentDb && (
          <Input.Search
            size="small" placeholder="过滤表名" allowClear value={tableFilter}
            onChange={(e) => setTableFilter(e.target.value)}
          />
        )}
        {currentDb ? (
          loadingTables ? (
            <div style={{ textAlign: 'center', padding: 12 }}><Spin size="small" /></div>
          ) : filteredTables.length === 0 ? (
            <Empty description="无匹配表" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '12px 0' }} />
          ) : (
            <div style={{ maxHeight: compact ? 260 : 380, overflow: 'auto' }}>
              <Tree
                showIcon
                defaultExpandAll={false}
                treeData={[{
                  title: <Text type="secondary" style={{ fontSize: 12 }}>{currentDb}（{filteredTables.length} 表）</Text>,
                  key: 'root',
                  selectable: false,
                  children: filteredTables.map((t) => ({
                    title: (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                        <span onClick={() => onPickTable(currentDb, t.name)} style={{ cursor: 'pointer' }}>
                          <TableOutlined style={{ marginRight: 4, color: '#8c8c8c' }} />
                          {t.name}
                          {t.rows > 0 && <Text type="secondary" style={{ fontSize: 11 }}> · {t.rows}</Text>}
                        </span>
                        <Tooltip title="查看表结构">
                          <Button
                            type="text" size="small"
                            icon={<FileTextOutlined style={{ color: '#8c8c8c', fontSize: 12 }} />}
                            style={{ width: 20, height: 20, minWidth: 0, padding: 0 }}
                            onClick={(e) => { e.stopPropagation(); setSchemaTable(t.name) }}
                          />
                        </Tooltip>
                      </span>
                    ),
                    key: t.name,
                    icon: <TableOutlined />,
                    selectable: false,
                  })),
                }]}
              />
            </div>
          )
        ) : (
          <Empty description="选择库后展示表" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '12px 0' }} />
        )}
      </Space>
      <TableSchemaDrawer
        open={!!schemaTable}
        onClose={() => setSchemaTable('')}
        instanceId={instanceId}
        database={currentDb}
        table={schemaTable}
      />
    </Card>
  )
}
