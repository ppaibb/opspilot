// ───────────────────────── 自助查询 Tab ─────────────────────────
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Input,
  App, Tooltip, Empty, Alert, Collapse, Row, Col,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ThunderboltOutlined, CopyOutlined, DownloadOutlined,
  HistoryOutlined, TableOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import { dbApi } from '@/api'
import { usePermission } from '@/hooks/usePermission'
import DbObjectBrowser from '@/components/db/DbObjectBrowser'
import type { DbInstance, DbQueryLogItem } from '@/types/api'

const { Text } = Typography
const { TextArea } = Input

export function QueryTab({ instances }: { instances: DbInstance[] }) {
  const { message } = App.useApp()
  const { has } = usePermission()
  const canSubmit = has('action.db.submit')
  const canManage = has('action.db.manage')
  const [instanceId, setInstanceId] = useState<number | null>(null)
  const [sqlText, setSqlText] = useState('SELECT 1;')
  const [queryDatabase, setQueryDatabase] = useState('')

  const currentInstance = instances.find((i) => i.id === instanceId)

  const { data: result, isFetching, refetch, error: queryError } = useQuery({
    queryKey: ['db-query', instanceId, sqlText, queryDatabase],
    queryFn: () => dbApi.runQuery({ instance_id: instanceId!, sql_text: sqlText, database: queryDatabase || undefined }),
    enabled: false,
  })

  const { data: logs = [] } = useQuery({
    queryKey: ['db-query-logs'],
    queryFn: () => dbApi.listQueryLogs(30),
  })

  const { data: myGrants = [] } = useQuery({
    queryKey: ['db-query-grants', 'mine'],
    queryFn: () => dbApi.listQueryGrants('mine'),
  })
  const activeGrantsForInstance = useMemo(
    () => myGrants.filter((g) => g.status === 'active' && g.instance_id === instanceId),
    [myGrants, instanceId],
  )
  const grantedDatabases = useMemo(() => new Set(activeGrantsForInstance.map((g) => g.database)), [activeGrantsForInstance])
  const nearestExpiry = useMemo(() => {
    const hits = activeGrantsForInstance
      .filter((g) => g.database === queryDatabase)
      .map((g) => g.expires_at)
      .sort()
    return hits[0] || ''
  }, [activeGrantsForInstance, queryDatabase])

  const { data: dbList } = useQuery({
    queryKey: ['db-databases', instanceId],
    queryFn: () => dbApi.listDatabases(instanceId!),
    enabled: !!instanceId,
    staleTime: 60 * 1000,
  })
  const defaultDatabase = dbList?.default_database ?? ''

  const pickInstance = (id: number) => {
    setInstanceId(id)
    setQueryDatabase(instances.find((i) => i.id === id)?.database || '')
  }

  const run = () => {
    if (!instanceId) { message.warning('请先在左侧选择实例'); return }
    if (!sqlText.trim()) { message.warning('请输入 SQL'); return }
    refetch()
  }

  const queryErrorText = useMemo(() => {
    if (!queryError) return ''
    const data = axios.isAxiosError(queryError)
      ? (queryError.response?.data as { detail?: { message?: string; findings?: string[] } | string } | undefined)
      : undefined
    const detail = data?.detail
    if (detail && typeof detail === 'object' && detail.findings) return `${detail.message}：${detail.findings.join('；')}`
    return typeof detail === 'string' ? detail : '查询请求失败，请检查实例配置与查询授权'
  }, [queryError])

  const rowColumns = useMemo(() => {
    if (!result?.columns?.length) return []
    return result.columns.map((col) => ({
      title: col,
      dataIndex: col,
      ellipsis: true,
      render: (v: unknown) => (
        <Text style={{ fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }} type={v == null ? 'secondary' : undefined}>
          {v == null ? 'NULL' : String(v)}
        </Text>
      ),
    }))
  }, [result])

  const failedHint = result && !result.ok
    ? (result.hint || result.results?.find((r) => !r.ok)?.hint || '')
    : ''
  const rawError = result && !result.ok
    ? (result.results?.find((r) => !r.ok)?.error || '')
    : ''

  const copyResult = async () => {
    if (!result?.rows?.length) return
    const cols = result.columns || []
    const tsv = [
      cols.join('\t'),
      ...result.rows.map((r: Record<string, unknown>) => cols.map((c) => (r[c] == null ? '' : String(r[c]))).join('\t')),
    ].join('\n')
    try {
      await navigator.clipboard.writeText(tsv)
      message.success('结果已复制（TSV）')
    } catch {
      message.error('复制失败')
    }
  }

  const exportCsv = () => {
    if (!result?.rows?.length) return
    const cols = result.columns || []
    const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [cols.map(esc).join(','), ...result.rows.map((r: Record<string, unknown>) => cols.map((c) => esc(r[c])).join(','))].join('\n')
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `query-result-${Date.now()}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const logColumns: ColumnsType<DbQueryLogItem> = [
    { title: '时间', dataIndex: 'created_at', width: 150 },
    { title: '实例', dataIndex: 'instance_name', width: 130, ellipsis: true },
    {
      title: '库', dataIndex: 'database', width: 100, ellipsis: true,
      render: (v: string) => v ? <Text code style={{ fontSize: 11 }}>{v}</Text> : <Text type="secondary">-</Text>,
    },
    { title: '查询人', dataIndex: 'username', width: 90 },
    { title: 'SQL', dataIndex: 'sql_text', ellipsis: true, render: (v: string) => <Text code style={{ fontSize: 11 }}>{v}</Text> },
    { title: '行数', dataIndex: 'row_count', width: 70, align: 'right' },
    { title: '耗时', dataIndex: 'duration_ms', width: 80, render: (v: number) => `${v}ms` },
    {
      title: '结果', dataIndex: 'ok', width: 80,
      render: (v: boolean, r) => v
        ? <Tag color="green">成功</Tag>
        : <Tooltip title={r.error || '执行失败'}><Tag color="red">失败</Tag></Tooltip>,
    },
  ]

  return (
    <Row gutter={12} style={{ height: 'calc(100vh - 175px)', minHeight: 480 }}>
      <Col span={7} style={{ height: '100%', overflow: 'auto' }}>
        <DbObjectBrowser
          instances={instances}
          instanceId={instanceId}
          onInstanceChange={pickInstance}
          database={queryDatabase}
          onDatabaseChange={setQueryDatabase}
          onPickTable={(db, table) => {
            const quote = currentInstance?.db_type === 'postgresql' ? '"' : '`'
            const qualified = `${quote}${db}${quote}.${quote}${table}${quote}`
            setSqlText((prev) => (prev.trim() ? `${prev}\n${qualified}` : `SELECT * FROM ${qualified} LIMIT 100;`))
          }}
        />
      </Col>
      <Col span={17} style={{ height: '100%', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0 }}>
          {instanceId && !canManage && (
            <Alert
              type={grantedDatabases.has(queryDatabase) ? 'success' : 'warning'}
              showIcon={false}
              style={{ padding: '6px 12px', borderRadius: 8 }}
              message={
                grantedDatabases.has(queryDatabase) ? (
                  <Text style={{ fontSize: 12 }}>
                    「{queryDatabase || '默认库'}」查询授权生效中{nearestExpiry ? `，${nearestExpiry} 到期` : ''}
                  </Text>
                ) : (
                  <Text style={{ fontSize: 12 }}>
                    当前库暂无生效授权——到「SQL 工单」提查询授权申请，终审通过后可查
                  </Text>
                )
              }
            />
          )}

          {/* 编辑器卡片：工具条 + SQL */}
          <Card
            size="small"
            style={{ flex: '0 0 auto' }}
            styles={{ body: { padding: 0 } }}
            title={
              <Flex align="center" gap={8}>
                <TableOutlined style={{ color: '#1677ff' }} />
                <span>SQL</span>
                {currentInstance && (
                  <Tag style={{ marginRight: 0 }}>
                    {currentInstance.name}
                    {queryDatabase || defaultDatabase ? ` · ${queryDatabase || defaultDatabase}` : ''}
                  </Tag>
                )}
              </Flex>
            }
            extra={
              <Space size={8}>
                <Tooltip title="Ctrl / Cmd + Enter">
                  <Text type="secondary" style={{ fontSize: 12 }}>只读 · 500 行 / 60s</Text>
                </Tooltip>
                <Button
                  type="primary"
                  size="small"
                  icon={<ThunderboltOutlined />}
                  disabled={!canSubmit}
                  loading={isFetching}
                  onClick={run}
                >
                  执行
                </Button>
              </Space>
            }
          >
            <TextArea
              rows={8}
              value={sqlText}
              onChange={(e) => setSqlText(e.target.value)}
              spellCheck={false}
              disabled={!canSubmit}
              placeholder="SELECT * FROM orders WHERE created_at > '2026-09-01' LIMIT 100;"
              variant="borderless"
              style={{
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                fontSize: 13,
                lineHeight: 1.6,
                background: '#fafafa',
                borderRadius: '0 0 8px 8px',
                resize: 'vertical',
              }}
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                  e.preventDefault()
                  run()
                }
              }}
            />
          </Card>

          {queryErrorText && (
            <Alert type="error" showIcon message="查询失败" description={<Text style={{ fontSize: 13 }}>{queryErrorText}</Text>} />
          )}

          {/* 结果卡片 */}
          <Card
            size="small"
            style={{ flex: '1 1 auto', minHeight: 0, overflow: 'hidden' }}
            styles={{ body: { height: 'calc(100% - 56px)', overflow: 'auto', paddingTop: 8 } }}
            title={
              <Flex align="center" gap={8}>
                <span>结果</span>
                {result?.ok && (
                  <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                    {result.database || '默认库'} · {(result.duration_ms / 1000).toFixed(2)}s · {result.rows.length} 行
                    {result.rows.length >= 500 ? '（达上限）' : ''}
                  </Text>
                )}
                {!result?.ok && result && (
                  <Tag color="red">失败</Tag>
                )}
              </Flex>
            }
            extra={
              result?.ok && result.rows?.length ? (
                <Space>
                  <Button size="small" icon={<CopyOutlined />} onClick={copyResult}>复制</Button>
                  <Button size="small" icon={<DownloadOutlined />} onClick={exportCsv}>CSV</Button>
                </Space>
              ) : null
            }
          >
            {result && !result.ok && (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 12 }}
                message={failedHint || '查询失败'}
                description={
                  rawError ? (
                    <Collapse size="small" ghost items={[{
                      key: 'raw',
                      label: <Text type="secondary" style={{ fontSize: 12 }}>查看数据库返回的原始错误</Text>,
                      children: <Text code style={{ fontSize: 11, whiteSpace: 'pre-wrap' }}>{rawError}</Text>,
                    }]} />
                  ) : undefined
                }
              />
            )}
            {result?.ok && !result.rows?.length ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<Text type="secondary">查询成功，0 行返回</Text>}
              />
            ) : null}
            {result?.rows?.length ? (
              <Table
                rowKey={(_, i) => String(i)}
                size="small"
                columns={rowColumns}
                dataSource={result.rows}
                pagination={{ pageSize: 20, showSizeChanger: false, size: 'small' }}
                scroll={{ x: 'max-content' }}
              />
            ) : null}
            {!result && !queryError && (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<Text type="secondary">执行后结果出现在这里</Text>}
              />
            )}
          </Card>

          {/* 审计默认收起 */}
          <Collapse
            ghost
            items={[{
              key: 'logs',
              label: (
                <Space>
                  <HistoryOutlined />
                  <Text strong style={{ fontSize: 13 }}>查询审计</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>最近 {logs.length} 条</Text>
                </Space>
              ),
              children: (
                <Table rowKey="id" size="small" columns={logColumns} dataSource={logs} pagination={false} />
              ),
            }]}
          />
      </Col>
    </Row>
  )
}
