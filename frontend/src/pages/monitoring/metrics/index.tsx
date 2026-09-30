/**
 * 指标查询页（/monitoring/metrics）。
 *
 * 面向 VictoriaMetrics 的 PromQL 直查控制台（后端代理，禁止前端直连 VM）：
 * - 页头 + 筛选条（常用指标快捷按钮 / 模式 / 时间窗）
 * - 主次分区：查询框 + 结果图（MetricChart）约 2/3，指标浏览器可折叠侧栏 1/3（默认收起）
 * - 双模式：「区间」画多序列折线 /「即时」出表格快照
 * - VM 未配置（configured=false）时整体降级提示
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Input, Button, Space, Select, Alert, Table, Tag, Typography, Spin, App, Tooltip, Collapse, Flex,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  SearchOutlined, ReloadOutlined, LineChartOutlined, TableOutlined, TagsOutlined, CopyOutlined, LinkOutlined,
} from '@ant-design/icons'
import { metricsQueryApi, systemApi, type VMSeries } from '@/api'
import MetricChart from '@/components/monitoring/MetricChart'
import { RANGE_OPTIONS, CHART_COLORS } from '@/components/monitoring/chartTheme'
import GrafanaLink from '@/components/monitoring/GrafanaLink'
import EmptyState from '@/components/monitoring/EmptyState'
import { PageHeader, FilterBar, MainSplit, SideCard } from '@/components/monitoring/PageShell'
import { QUICK_METRICS } from './quickQueries'

const { Text } = Typography
const { TextArea } = Input

type QueryMode = 'range' | 'instant'

/** 把 metric 标签集序列化成 PromQL selector 形式，用于表格展示与序列命名。 */
function seriesLabel(metric: Record<string, string>): string {
  const entries = Object.entries(metric || {}).filter(([k]) => k !== '__name__' || Object.keys(metric).length === 1)
  if (!entries.length) return '{}'
  return `{${entries.map(([k, v]) => `${k}="${v}"`).join(', ')}}`
}

function seriesName(metric: Record<string, string>): string {
  const keys = Object.entries(metric || {})
  if (!keys.length) return 'scalar'
  const name = metric.__name__
  const rest = keys.filter(([k]) => k !== '__name__').map(([k, v]) => `${k}="${v}"`)
  return name ? `${name}{${rest.join(', ')}` + (rest.length ? '}' : '}') : seriesLabel(metric)
}

export default function MetricsQueryPage() {
  const { message } = App.useApp()
  const [query, setQuery] = useState('up')
  const [rangeKey, setRangeKey] = useState('1h')
  const [mode, setMode] = useState<QueryMode>('range')
  const [submitted, setSubmitted] = useState<{ mode: QueryMode; query: string } | null>(null)
  const [browserLabel, setBrowserLabel] = useState<string>('__name__')
  const [browserOpen, setBrowserOpen] = useState(false)

  const { data: configData } = useQuery({
    queryKey: ['monitoring-config'],
    queryFn: systemApi.getMonitoring,
  })
  const isConfigured = !!configData?.vm_url

  const rangeOption = RANGE_OPTIONS.find((o) => o.value === rangeKey) || RANGE_OPTIONS[1]

  // 提交后才发查询（submitted 同时充当缓存 key 的一部分，避免输入过程反复打 VM）。
  const { data: rangeResult, isLoading: loadingRange } = useQuery({
    queryKey: ['metrics-range', submitted?.query, rangeOption.minutes, rangeOption.step],
    queryFn: () => metricsQueryApi.range({
      query: submitted!.query,
      range_minutes: rangeOption.minutes,
      step_seconds: rangeOption.step,
    }),
    enabled: submitted?.mode === 'range' && isConfigured && !!submitted?.query,
    refetchOnWindowFocus: false,
  })

  const { data: instantResult, isLoading: loadingInstant } = useQuery({
    queryKey: ['metrics-instant', submitted?.query],
    queryFn: () => metricsQueryApi.instant({ query: submitted!.query }),
    enabled: submitted?.mode === 'instant' && isConfigured && !!submitted?.query,
    refetchOnWindowFocus: false,
  })

  // 指标浏览器：label 名列表 + 选中 label 的取值（展开时才拉）。
  const { data: labelsData } = useQuery({
    queryKey: ['metrics-labels'],
    queryFn: metricsQueryApi.labels,
    enabled: isConfigured && browserOpen,
    staleTime: 5 * 60 * 1000,
  })
  const { data: valuesData, isLoading: loadingValues } = useQuery({
    queryKey: ['metrics-label-values', browserLabel],
    queryFn: () => metricsQueryApi.labelValues(browserLabel),
    enabled: isConfigured && browserOpen && !!browserLabel,
    staleTime: 5 * 60 * 1000,
  })

  const insertSelector = (value: string) => {
    setQuery((prev) => {
      // 已含 selector 时替换 __name__ 值，否则追加。保持简单可预期。
      if (prev.includes('{')) return prev.replace(/(__name__=")[^"]*(")/, `$1${value}$2`)
      return `${value}`
    })
    message.info(`已插入：${value}`)
  }

  const chartData = useMemo(() => {
    const series = rangeResult?.data?.result || []
    if (!series.length) return null
    // 对齐所有序列的时间戳（以最长序列为骨架，缺失点置 null 断线）。
    const stamps = new Set<number>()
    for (const s of series) for (const [ts] of s.values || []) stamps.add(ts)
    const sorted = [...stamps].sort((a, b) => a - b)
    return {
      times: sorted.map((ts) => new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })),
      series: series.map((s, idx) => {
        const map = new Map(s.values || [])
        return {
          name: seriesName(s.metric),
          values: sorted.map((ts) => {
            const v = map.get(ts)
            return v == null ? null : Number(v)
          }),
          color: CHART_COLORS[idx % CHART_COLORS.length],
        }
      }),
    }
  }, [rangeResult])

  const instantColumns: ColumnsType<{ key: string; metric: Record<string, string>; value: string; ts: number }> = [
    {
      title: '序列', dataIndex: 'key', ellipsis: true,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '值', dataIndex: 'value', width: 160, align: 'right',
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '时间戳', dataIndex: 'ts', width: 190,
      render: (v: number) => new Date(v * 1000).toLocaleString('zh-CN', { hour12: false }),
    },
  ]

  const instantRows = useMemo(() => {
    const result = instantResult?.data?.result || []
    return result.map((s: VMSeries, idx: number) => ({
      key: `${seriesName(s.metric)}#${idx}`,
      metric: s.metric,
      value: s.value?.[1] ?? '-',
      ts: s.value?.[0] ?? 0,
    }))
  }, [instantResult])

  const loading = mode === 'range' ? loadingRange : loadingInstant
  const hasResult = submitted?.mode === 'range'
    ? (rangeResult?.data?.result?.length ?? 0) > 0
    : (instantResult?.data?.result?.length ?? 0) > 0

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <PageHeader
        title="指标查询"
        subtitle={isConfigured ? 'PromQL 直查 · 结果图 MetricChart' : 'VictoriaMetrics 未配置'}
        extra={
          <>
            <Button
              type="primary"
              icon={<SearchOutlined />}
              disabled={!isConfigured || !query.trim()}
              loading={loading}
              onClick={() => setSubmitted({ mode, query: query.trim() })}
            >
              执行查询
            </Button>
          </>
        }
      />

      {!isConfigured && configData && (
        <Alert
          message="VictoriaMetrics 未配置"
          description="请先在「系统设置 → 系统管理」中配置监控连接信息后再使用指标查询。"
          type="warning" showIcon style={{ marginBottom: 16 }}
          action={<Button size="small" onClick={() => window.location.assign('/settings/system')}>去配置</Button>}
        />
      )}

      <FilterBar
        left={
          <>
            <Text type="secondary" style={{ fontSize: 12 }}>常用指标</Text>
            {QUICK_METRICS.map((t) => (
              <Button key={t.label} size="small" onClick={() => setQuery(t.value)} disabled={!isConfigured}>
                {t.label}
              </Button>
            ))}
          </>
        }
        right={
          <>
            <Select
              value={mode}
              onChange={(v) => setMode(v)}
              style={{ width: 120 }}
              options={[
                { value: 'range', label: '区间' },
                { value: 'instant', label: '即时' },
              ]}
            />
            {mode === 'range' && (
              <Select
                value={rangeKey}
                onChange={setRangeKey}
                style={{ width: 110 }}
                options={RANGE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
              />
            )}
            {submitted && (
              <Button icon={<ReloadOutlined />} onClick={() => setSubmitted({ ...submitted })} disabled={!isConfigured}>
                刷新
              </Button>
            )}
          </>
        }
      />

      <MainSplit
        main={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Card title="PromQL 查询" size="small">
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <TextArea
                  rows={3}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="输入 PromQL，如 up"
                  disabled={!isConfigured}
                  style={{ fontFamily: 'monospace' }}
                  onPressEnter={(e) => {
                    if (e.ctrlKey || e.metaKey) setSubmitted({ mode, query: query.trim() })
                  }}
                />
                <Flex justify="space-between" wrap gap="small">
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {mode === 'range'
                      ? `步长 ${rangeOption.step}s · ${rangeOption.minutes} 分钟窗口 · Ctrl+Enter 执行`
                      : '返回当前时刻的即时快照'}
                  </Text>
                  <Button
                    type="primary"
                    size="small"
                    icon={<SearchOutlined />}
                    disabled={!isConfigured || !query.trim()}
                    loading={loading}
                    onClick={() => setSubmitted({ mode, query: query.trim() })}
                  >
                    执行查询
                  </Button>
                </Flex>
              </Space>
            </Card>

            {submitted && (
              <Card
                title={
                  <Space>
                    {submitted.mode === 'range' ? <LineChartOutlined /> : <TableOutlined />}
                    查询结果
                  </Space>
                }
                size="small"
                extra={
                  <Space>
                    <Tooltip title={submitted.query}>
                      <Tag style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {submitted.query.length > 40 ? `${submitted.query.slice(0, 40)}…` : submitted.query}
                      </Tag>
                    </Tooltip>
                    <GrafanaLink
                      expr={submitted.query}
                      from={submitted.mode === 'range' ? `now-${rangeOption.minutes}m` : 'now-5m'}
                      to="now"
                    />
                  </Space>
                }
              >
                {loading ? (
                  <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="查询中..." /></div>
                ) : !hasResult ? (
                  <EmptyState
                    kind="no-result"
                    title={submitted.mode === 'range' ? '区间查询无数据' : '即时查询无数据'}
                    description="调整表达式或时间窗后重试；也可展开右侧指标浏览器点选指标名快速填入。"
                  />
                ) : submitted.mode === 'range' && chartData ? (
                  <MetricChart
                    times={chartData.times}
                    series={chartData.series}
                    height={380}
                    subtitle={`PromQL：${submitted.query} · step ${rangeOption.step}s`}
                  />
                ) : (
                  <Table
                    rowKey="key"
                    size="small"
                    columns={instantColumns}
                    dataSource={instantRows}
                    pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 个序列` }}
                  />
                )}
              </Card>
            )}

            {!submitted && (
              <Card size="small">
                <EmptyState
                  kind="no-result"
                  title="开始查询"
                  description="点上方常用指标快捷按钮填入 PromQL，或手写表达式后执行查询。"
                />
              </Card>
            )}
          </Space>
        }
        side={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <SideCard title="说明">
              <Text type="secondary" style={{ fontSize: 12 }}>
                区间模式画多序列折线（MetricChart），即时模式出表格快照。
                常用指标一键填 PromQL；指标浏览器默认收起，展开后点取值填入查询框。
              </Text>
            </SideCard>

            <Card size="small" title="指标浏览器" extra={<TagsOutlined />}>
              <Collapse
                ghost
                activeKey={browserOpen ? ['browser'] : []}
                onChange={(keys) => setBrowserOpen(Array.isArray(keys) ? keys.includes('browser') : false)}
                items={[
                  {
                    key: 'browser',
                    label: browserOpen ? '收起浏览器' : '展开浏览器',
                    children: !isConfigured ? (
                      <EmptyState kind="not-connected" />
                    ) : (
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        <Select
                          value={browserLabel}
                          onChange={setBrowserLabel}
                          style={{ width: '100%' }}
                          showSearch
                          options={(labelsData?.labels || ['__name__']).map((l) => ({ value: l, label: l }))}
                        />
                        {loadingValues ? (
                          <Spin size="small" />
                        ) : (
                          <div style={{ maxHeight: 280, overflow: 'auto', padding: 4 }}>
                            <Space wrap>
                              {(valuesData?.values || []).slice(0, 500).map((value) => (
                                <Tooltip key={value} title="点击填入查询框">
                                  <Tag
                                    style={{ cursor: 'pointer', margin: 2 }}
                                    onClick={() => insertSelector(value)}
                                  >
                                    <CopyOutlined /> {value.length > 36 ? `${value.slice(0, 36)}…` : value}
                                  </Tag>
                                </Tooltip>
                              ))}
                              {(valuesData?.values || []).length === 0 && (
                                <Text type="secondary">该 Label 暂无取值</Text>
                              )}
                            </Space>
                          </div>
                        )}
                      </Space>
                    ),
                  },
                ]}
              />
            </Card>

            <SideCard title="快捷入口">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/live">实时看板</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/alerts">告警中心</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/logs">日志查询</Button>
              </Space>
            </SideCard>
          </Space>
        }
      />
    </div>
  )
}
