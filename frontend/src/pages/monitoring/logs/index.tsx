/**
 * 日志查询页（/monitoring/logs）。
 *
 * 对接 VictoriaLogs（后端代理），字段契约与后端 _vlogs_get 归一化结果对齐：
 * `{id, time, message, fields}`（+ level/service/host 便捷冗余）。
 * - 常用查询书签（内置 5 例 + 自定义，localStorage）
 * - Grafana 式标签筛选（host/service/job/level 多选，自动拼 LogsQL）
 * - 直方图与时间窗联动（点柱子收窄到该桶）
 * - 表格行「上下文」按钮入口更明显
 * - Live Tail：fetch-SSE 消费后端 /logs/tail 流
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  Card, Input, Button, Space, Select, Alert, Typography, Flex, Tag, Drawer, Spin, App, Tooltip, Table,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  SearchOutlined, ReloadOutlined, PlayCircleOutlined, PauseCircleOutlined, ClearOutlined, NodeIndexOutlined,
  BookOutlined, StarOutlined, LinkOutlined, ColumnHeightOutlined, ThunderboltOutlined, FilterOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { logsApi, systemApi, type LogLine, type NormalizedLogEntry } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import { authenticatedFetch } from '@/lib/fetch'
import EmptyState from '@/components/monitoring/EmptyState'
import GrafanaLink from '@/components/monitoring/GrafanaLink'
import { GRAFANA_DS_LOGS } from '@/components/monitoring/grafanaDatasources'
import { PageHeader, FilterBar, MainSplit, SideCard } from '@/components/monitoring/PageShell'
import { cssColor } from '@/components/monitoring/chartTheme'
import { normalizeLog, formatLogTime } from './normalizeLog'
import { composeLogsQuery, type LabelFilters } from './labelFilter'
import {
  appendBookmark, loadBookmarks, removeBookmark, saveBookmarks,
  type LogBookmark,
} from './bookmarks'

const { Text } = Typography

/** 级别 → 标签色（未知级别降级为灰）。 */
const LEVEL_COLOR: Record<string, string> = {
  ERROR: 'red', FATAL: 'red', CRITICAL: 'red',
  WARN: 'orange', WARNING: 'orange',
  INFO: 'blue', DEBUG: 'default', TRACE: 'default',
}

const RANGE_OPTIONS = [
  { label: '15 分钟', value: 15 },
  { label: '1 小时', value: 60 },
  { label: '6 小时', value: 360 },
  { label: '24 小时', value: 1440 },
  { label: '7 天', value: 10080 },
]

/** 固定展示的标签字段（下拉多选）。 */
const LABEL_FIELDS = [
  { key: 'host', label: '主机' },
  { key: 'service', label: '服务' },
  { key: 'job', label: '任务' },
  { key: 'level', label: '级别' },
] as const

const HISTOGRAM_STEP_SEC = (rangeMinutes: number) => (rangeMinutes > 360 ? 1800 : 300)

export default function LogsQueryPage() {
  const { message } = App.useApp()
  const [searchParams] = useSearchParams()
  const hostParam = searchParams.get('host') || ''
  const [query, setQuery] = useState('*')
  const [labels, setLabels] = useState<LabelFilters>(() => (
    hostParam ? { host: [hostParam] } : {} as LabelFilters
  ))
  const [rangeMinutes, setRangeMinutes] = useState(60)
  const [limit, setLimit] = useState(200)
  // 深链 ?host=IP：初始化时直接带上标签查询，避免 effect 里 setState
  const [submitted, setSubmitted] = useState<{ query: string; start: string; end: string; limit: number } | null>(() => {
    if (!hostParam) return null
    const end = new Date()
    const start = new Date(end.getTime() - 60 * 60 * 1000)
    return {
      query: composeLogsQuery('*', { host: [hostParam] }),
      start: start.toISOString(),
      end: end.toISOString(),
      limit: 200,
    }
  })
  const [contextLog, setContextLog] = useState<NormalizedLogEntry | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)
  const [bookmarks, setBookmarks] = useState<LogBookmark[]>(() => loadBookmarks())
  const [bucketWindow, setBucketWindow] = useState<{ start: string; end: string } | null>(null)
  const [nlText, setNlText] = useState('')
  const [suggestError, setSuggestError] = useState('')
  const suggestMut = useMutation({
    mutationFn: () => logsApi.suggestQuery(nlText.trim()),
    onSuccess: (res) => {
      if (res.ok && res.query) {
        setQuery(res.query)
        setSuggestError('')
        message.success('已生成查询，可直接执行')
      } else {
        setSuggestError(res.message || '未能生成查询')
      }
    },
    onError: (e: unknown) => setSuggestError(extractAxiosDetail(e) ?? '生成失败'),
  })

  // Live Tail 状态。
  const [tailing, setTailing] = useState(false)
  const [tailLines, setTailLines] = useState<NormalizedLogEntry[]>([])
  const tailAbort = useRef<AbortController | null>(null)

  const { data: configData, isError: configError, error: configErr } = useQuery({
    queryKey: ['monitoring-config'],
    queryFn: systemApi.getMonitoring,
    retry: false,
  })
  const isConfigured = !!configData?.vlogs_url
  const forbidden = configError && (configErr as { response?: { status?: number } })?.response?.status === 403

  const timeParams = useMemo(() => {
    const end = new Date()
    const start = new Date(end.getTime() - rangeMinutes * 60 * 1000)
    return { start: start.toISOString(), end: end.toISOString() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangeMinutes, submitted])

  // 标签取值：按当前时间窗 + 已选标签增量拉取（字段固定 4 个）。
  const { data: fieldMaps } = useQuery({
    queryKey: ['log-label-values', timeParams.start, timeParams.end, labels],
    queryFn: async () => {
      const entries = await Promise.all(
        LABEL_FIELDS.map(async (f) => {
          const res = await logsApi.fields({
            field: f.key,
            start: timeParams.start,
            end: timeParams.end,
          })
          return [f.key, res.values || []] as const
        }),
      )
      return Object.fromEntries(entries) as Record<string, string[]>
    },
    enabled: isConfigured,
    staleTime: 60_000,
    retry: false,
  })

  const composedQuery = useMemo(() => composeLogsQuery(query, labels), [query, labels])
  const activeLabelCount = Object.values(labels).reduce((n, v) => n + (v?.length || 0), 0)

  const { data: queryResult, isLoading, refetch, isError, error } = useQuery({
    queryKey: ['logs-query', submitted],
    queryFn: () => logsApi.query({ ...submitted!, limit: submitted!.limit }),
    enabled: isConfigured && !!submitted,
    refetchOnWindowFocus: false,
    retry: false,
  })

  const { data: histogram } = useQuery({
    queryKey: ['logs-histogram', submitted],
    queryFn: () => logsApi.histogram({
      query: submitted!.query,
      start: submitted!.start,
      end: submitted!.end,
      step: rangeMinutes > 360 ? '30m' : '5m',
    }),
    enabled: isConfigured && !!submitted,
    refetchOnWindowFocus: false,
    retry: false,
  })

  const { data: contextData, isLoading: loadingContext } = useQuery({
    queryKey: ['logs-context', contextLog?.stream_id, contextLog?.time],
    // 上下文抽屉用 id 作 stream_id
    queryFn: () => logsApi.context({ stream_id: contextLog!.stream_id || contextLog!.id, timestamp: contextLog!.time }),
    enabled: !!(contextLog?.stream_id || contextLog?.id) && !!contextLog?.time,
  })

  const rows = useMemo(
    () => (queryResult?.logs || []).map(normalizeLog).reverse(),
    [queryResult],
  )

  const histogramOption = useMemo(() => {
    const buckets = histogram?.buckets || []
    if (!buckets.length) return null
    const axisColor = cssColor('--ink-3', '#8c8c8c')
    const lineColor = cssColor('--line', '#f0f0f0')
    const barColor = cssColor('--chart-1', '#1677ff')
    // 后端已把时间戳归一为 Unix 秒；*1000 得到 ms
    return {
      grid: { left: 56, right: 16, top: 16, bottom: 44 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: buckets.map((b) => new Date(b.time * 1000).toLocaleTimeString('zh-CN', { hour12: false })),
        axisLabel: { color: axisColor, rotate: 24 },
      },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: lineColor } } },
      series: [{
        type: 'bar',
        name: '日志量',
        barMaxWidth: 12,
        cursor: 'pointer',
        data: buckets.map((b) => b.count),
        itemStyle: { color: barColor, borderRadius: [2, 2, 0, 0] },
      }],
    }
  }, [histogram])

  /** 停止 Live Tail 并释放底层连接。 */
  const stopTail = () => {
    tailAbort.current?.abort()
    tailAbort.current = null
    setTailing(false)
  }

  const startTail = async () => {
    if (tailing) return
    setTailLines([])
    setTailing(true)
    const controller = new AbortController()
    tailAbort.current = controller
    try {
      const resp = await authenticatedFetch(
        `/api/monitoring/logs/tail?query=${encodeURIComponent(composeLogsQuery(query, labels) || '*')}`,
        { signal: controller.signal },
      )
      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() || ''
        for (const chunk of chunks) {
          const dataLine = chunk.split('\n').find((l) => l.startsWith('data:'))
          if (!dataLine) continue
          try {
            const entry = normalizeLog(JSON.parse(dataLine.slice(5).trim()) as LogLine)
            setTailLines((prev) => [...prev.slice(-500), entry])
          } catch {
            // 非 JSON 行（心跳等）忽略。
          }
        }
      }
    } catch (err: unknown) {
      if ((err as Error)?.name !== 'AbortError') {
        message.error(`Live Tail 连接失败：${(err as Error)?.message || '未知错误'}`)
      }
    } finally {
      setTailing(false)
      tailAbort.current = null
    }
  }

  useEffect(() => () => tailAbort.current?.abort(), [])

  const executeQuery = (range?: { start: string; end: string }) => {
    stopTail()
    setQueryError(null)
    const r = range || timeParams
    if (!range) setBucketWindow(null)
    setSubmitted({ query: composeLogsQuery(query, labels), start: r.start, end: r.end, limit })
  }

  /** 点直方图柱子：时间窗收窄到该桶并重查。 */
  const onHistogramClick = (params: { dataIndex?: number }) => {
    const buckets = histogram?.buckets || []
    const b = buckets[params.dataIndex ?? -1]
    if (!b) return
    const step = HISTOGRAM_STEP_SEC(rangeMinutes)
    const win = {
      start: new Date(b.time * 1000).toISOString(),
      end: new Date((b.time + step) * 1000).toISOString(),
    }
    setBucketWindow(win)
    executeQuery(win)
  }

  const addBookmark = () => {
    const { list, added } = appendBookmark(bookmarks, query.trim().slice(0, 40), query)
    if (!added) {
      message.info('该查询已在书签中')
      return
    }
    setBookmarks(list)
    saveBookmarks(list)
    message.success('已保存查询书签')
  }

  const dropBookmark = (b: LogBookmark) => {
    const next = removeBookmark(bookmarks, b.query)
    setBookmarks(next)
    saveBookmarks(next)
  }

  const columns: ColumnsType<NormalizedLogEntry> = [
    {
      title: '时间',
      dataIndex: 'time',
      key: 'time',
      width: 170,
      fixed: 'left',
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{formatLogTime(v)}</Text>,
    },
    {
      title: '级别',
      dataIndex: 'level',
      key: 'level',
      width: 80,
      fixed: 'left',
      render: (level: string) => <Tag color={LEVEL_COLOR[level] || 'default'} style={{ margin: 0 }}>{level}</Tag>,
    },
    {
      title: '主机',
      dataIndex: 'host',
      key: 'host',
      width: 120,
      ellipsis: true,
    },
    {
      title: '服务',
      dataIndex: 'service',
      key: 'service',
      width: 110,
      ellipsis: true,
    },
    {
      title: '消息',
      dataIndex: 'message',
      key: 'message',
      ellipsis: true,
      render: (v: string) => v || <Text type="secondary">（空消息）</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      fixed: 'right',
      render: (_: unknown, row: NormalizedLogEntry) => (
        <Button
          size="small"
          icon={<ColumnHeightOutlined />}
          disabled={!(row.stream_id || row.id)}
          onClick={(e) => {
            e.stopPropagation()
            setContextLog(row)
          }}
        >
          上下文
        </Button>
      ),
    },
  ]

  const renderLogRows = (data: NormalizedLogEntry[], onRowClick?: (row: NormalizedLogEntry) => void) => (
    <Table
      size="small"
      rowKey={(r, i) => `${r.id || r.stream_id || 'row'}-${r.time}-${i}`}
      columns={columns}
      dataSource={data}
      pagination={false}
      scroll={{ x: 820, y: 420 }}
      onRow={onRowClick ? (row) => ({ onClick: () => onRowClick(row), style: { cursor: 'pointer' } }) : undefined}
      locale={{
        emptyText: (
          <EmptyState
            kind="no-result"
            title="暂无日志"
            description="换时间范围或放宽 LogsQL 条件再试；也可用 * 全量扫描。"
          />
        ),
      }}
    />
  )

  if (forbidden) {
    return <EmptyState kind="forbidden" />
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <PageHeader
        title="日志查询"
        subtitle={
          <Space size={12}>
            <span>{isConfigured ? 'VictoriaLogs 已接入' : 'VictoriaLogs 未配置'}</span>
            {submitted && <span>命中 <b style={{ color: 'var(--ink)' }}>{queryResult?.logs?.length ?? 0}</b> 行</span>}
            {tailing && <span style={{ color: 'var(--accent)' }}>Live Tail 中</span>}
          </Space>
        }
        extra={
          <>
            <GrafanaLink
              label="Grafana"
              from={`now-${rangeMinutes}m`}
              to="now"
              expr={composedQuery !== '*' ? composedQuery : '*'}
              datasource={GRAFANA_DS_LOGS}
            />
            {tailing ? (
              <Button danger icon={<PauseCircleOutlined />} onClick={stopTail}>停止</Button>
            ) : (
              <Tooltip title="实时跟踪新日志（最长 5 分钟）">
                <Button icon={<PlayCircleOutlined />} disabled={!isConfigured || !query} onClick={startTail}>
                  Live Tail
                </Button>
              </Tooltip>
            )}
            <Button
              type="primary"
              icon={<SearchOutlined />}
              loading={isLoading}
              disabled={!isConfigured}
              onClick={() => executeQuery()}
            >
              查询
            </Button>
          </>
        }
      />

      {!isConfigured && configData && (
        <Alert
          message="VictoriaLogs 未配置"
          description="请先在「系统设置 → 系统管理」中配置日志存储连接后再使用日志查询。"
          type="warning" showIcon style={{ marginBottom: 16 }}
          action={<Button size="small" onClick={() => window.location.assign('/settings/system')}>去配置</Button>}
        />
      )}

      <FilterBar
        left={
          <>
            <BookOutlined style={{ color: 'var(--ink-3)' }} />
            {bookmarks.map((b) => (
              <Tag
                key={b.query}
                closable
                style={{ cursor: 'pointer', marginInlineEnd: 4 }}
                onClick={() => { setQuery(b.query); message.info(`已填入：${b.name}`) }}
                onClose={(e) => {
                  e.preventDefault()
                  dropBookmark(b)
                }}
              >
                {b.name}
              </Tag>
            ))}
          </>
        }
        right={
          <>
            <Select
              value={rangeMinutes}
              onChange={(v) => { setBucketWindow(null); setRangeMinutes(v) }}
              style={{ width: 100 }}
              options={RANGE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
            />
            <Select
              value={limit}
              onChange={setLimit}
              style={{ width: 100 }}
              options={[100, 200, 500, 1000].map((n) => ({ value: n, label: `${n} 行` }))}
            />
            <Tooltip title="保存当前查询为书签">
              <Button icon={<StarOutlined />} disabled={!query.trim()} onClick={addBookmark} />
            </Tooltip>
            {submitted && (
              <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
            )}
            {tailLines.length > 0 && (
              <Button icon={<ClearOutlined />} onClick={() => setTailLines([])}>清空</Button>
            )}
          </>
        }
      />

      <MainSplit
        main={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Card size="small" title="LogsQL 查询">
              <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
                <Input
                  value={nlText}
                  onChange={(e) => setNlText(e.target.value)}
                  placeholder="用一句话描述要查什么，如：查 nginx 5xx 报错"
                  disabled={!isConfigured}
                  onPressEnter={() => suggestMut.mutate()}
                />
                <Button
                  icon={<ThunderboltOutlined />}
                  loading={suggestMut.isPending}
                  disabled={!isConfigured || !nlText.trim()}
                  onClick={() => suggestMut.mutate()}
                >
                  生成查询
                </Button>
              </Space.Compact>
              {suggestError && (
                <Alert type="warning" showIcon style={{ marginBottom: 8 }} message={suggestError} />
              )}
              <Flex gap={8} wrap style={{ marginBottom: 8 }} align="center">
                <Space size={4}>
                  <FilterOutlined style={{ color: 'var(--ink-3)' }} />
                  <Text type="secondary" style={{ fontSize: 12 }}>标签</Text>
                </Space>
                {LABEL_FIELDS.map((f) => (
                  <Select
                    key={f.key}
                    mode="multiple"
                    allowClear
                    showSearch
                    placeholder={f.label}
                    style={{ minWidth: 140 }}
                    maxTagCount={2}
                    disabled={!isConfigured}
                    value={labels[f.key] || []}
                    options={(fieldMaps?.[f.key] || []).map((v) => ({ value: v, label: v }))}
                    onChange={(vals) => {
                      setLabels((prev) => {
                        const next = { ...prev }
                        if (vals.length) next[f.key] = vals
                        else delete next[f.key]
                        return next
                      })
                    }}
                  />
                ))}
                {activeLabelCount > 0 && (
                  <Button size="small" type="text" onClick={() => setLabels({})}>清除标签</Button>
                )}
                {activeLabelCount > 0 && (
                  <Tag color="blue" style={{ marginInlineEnd: 0 }}>{activeLabelCount} 项已选</Tag>
                )}
              </Flex>
              {activeLabelCount > 0 && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={<span style={{ fontFamily: 'monospace', fontSize: 12 }}>实际查询：{composedQuery}</span>}
                />
              )}
              <Input.Search
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder='关键字 / LogsQL，如 * 或 level:ERROR（会与上方标签 AND）'
                disabled={!isConfigured}
                enterButton={
                  <Button type="primary" icon={<SearchOutlined />} loading={isLoading} disabled={!isConfigured}>
                    查询
                  </Button>
                }
                onSearch={() => executeQuery()}
                style={{ fontFamily: 'monospace' }}
              />
            </Card>

            {(isError || queryError) && (
              <Alert
                type="error"
                showIcon
                message="日志查询失败"
                description={
                  <div>
                    <div>{queryError || (error as Error)?.message || '未知错误'}</div>
                    <div style={{ marginTop: 8, color: 'var(--ink-2)' }}>
                      下一步：检查 VictoriaLogs 连接配置、确认查询语法（LogsQL），或缩短时间范围后重试。
                    </div>
                  </div>
                }
                action={<Button size="small" onClick={() => refetch()}>重试</Button>}
              />
            )}

            {tailing && (
              <Card title={<Space><PlayCircleOutlined style={{ color: 'var(--accent)' }} />Live Tail 实时输出</Space>} size="small">
                {renderLogRows(tailLines)}
              </Card>
            )}

            {histogramOption && (
              <Card
                title="时间分布"
                size="small"
                extra={
                  bucketWindow ? (
                    <Space>
                      <Tag color="blue">已选时间窗</Tag>
                      <Button size="small" onClick={() => executeQuery()}>还原整个范围</Button>
                    </Space>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>点柱子收窄时间窗</Text>
                  )
                }
              >
                <ReactECharts
                  option={histogramOption}
                  style={{ height: 160 }}
                  notMerge
                  onEvents={{ click: onHistogramClick }}
                />
              </Card>
            )}

            {submitted && (
              <Card
                title={
                  <Space>
                    <NodeIndexOutlined />
                    查询结果
                    <Tag>{queryResult?.logs?.length ?? 0} 行</Tag>
                  </Space>
                }
                size="small"
              >
                {isLoading ? (
                  <div style={{ textAlign: 'center', padding: '40px 0' }}><Spin tip="查询中..." /></div>
                ) : queryResult && queryResult.configured === false ? (
                  <EmptyState kind="not-connected" description={queryResult.message || '日志服务未配置'} />
                ) : rows.length === 0 && !isError ? (
                  <EmptyState kind="no-result" />
                ) : (
                  renderLogRows(rows, (row) => (row.stream_id || row.id) && setContextLog(row))
                )}
              </Card>
            )}
          </Space>
        }
        side={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <SideCard
              title="常用查询书签"
              extra={<BookOutlined />}
            >
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                {bookmarks.length === 0 && (
                  <Text type="secondary" style={{ fontSize: 12 }}>暂无书签，输入查询后点 ★ 保存。</Text>
                )}
                {bookmarks.map((b) => (
                  <Flex key={b.query} justify="space-between" align="center" gap={8}>
                    <Button
                      type="link"
                      size="small"
                      style={{ padding: 0, textAlign: 'left' }}
                      onClick={() => { setQuery(b.query); message.info(`已填入：${b.name}`) }}
                    >
                      {b.name}
                    </Button>
                    <Text type="secondary" style={{ fontSize: 11, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {b.query}
                    </Text>
                    <Button size="small" type="text" danger onClick={() => dropBookmark(b)}>删</Button>
                  </Flex>
                ))}
              </Space>
            </SideCard>

            <SideCard title="快捷入口">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/alerts">告警中心</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/live">实时看板</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/collectors">采集车队</Button>
              </Space>
            </SideCard>
          </Space>
        }
      />

      <Drawer
        title={`日志上下文${contextLog ? ` · ${formatLogTime(contextLog.time)}` : ''}`}
        open={!!contextLog}
        onClose={() => setContextLog(null)}
        width={760}
      >
        {loadingContext ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="加载上下文..." /></div>
        ) : contextLog ? (
          <>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              同一日志流（stream_id={contextLog.stream_id || contextLog.id || '-'}）中该行前后的记录；当前行以蓝色高亮。
            </Text>
            <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
              {(contextData?.before || []).map((line, idx) => {
                const n = normalizeLog(line)
                return (
                  <Flex key={`b-${idx}`} gap={8} style={{ padding: '3px 8px', borderBottom: '1px solid var(--line)', opacity: 0.75 }}>
                    <Text type="secondary" style={{ flexShrink: 0, minWidth: 150 }}>{formatLogTime(n.time)}</Text>
                    <Tag style={{ margin: 0 }}>{n.level}</Tag>
                    <Text style={{ flex: 1, wordBreak: 'break-all' }}>{n.message}</Text>
                  </Flex>
                )
              })}
              <Flex gap={8} style={{ padding: '6px 8px', background: 'color-mix(in srgb, var(--accent) 12%, transparent)', border: '1px solid var(--accent)', borderRadius: 4, margin: '4px 0' }}>
                <Text type="secondary" style={{ flexShrink: 0, minWidth: 150 }}>{formatLogTime(contextLog.time)}</Text>
                <Tag color="blue" style={{ margin: 0 }}>{contextLog.level}</Tag>
                <Text strong style={{ flex: 1, wordBreak: 'break-all' }}>{contextLog.message}</Text>
              </Flex>
              {(contextData?.after || []).map((line, idx) => {
                const n = normalizeLog(line)
                return (
                  <Flex key={`a-${idx}`} gap={8} style={{ padding: '3px 8px', borderBottom: '1px solid var(--line)', opacity: 0.75 }}>
                    <Text type="secondary" style={{ flexShrink: 0, minWidth: 150 }}>{formatLogTime(n.time)}</Text>
                    <Tag style={{ margin: 0 }}>{n.level}</Tag>
                    <Text style={{ flex: 1, wordBreak: 'break-all' }}>{n.message}</Text>
                  </Flex>
                )
              })}
            </div>
          </>
        ) : null}
      </Drawer>
    </div>
  )
}
