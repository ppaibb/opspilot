/**
 * 实时看板（/monitoring/live）。
 *
 * 双模式升级版（替换原单主机版）：
 * - **全局模式**（默认）：消费后端 /monitoring/dashboard 聚合接口——
 *   目标/在线/告警/Load 概要、平台级 CPU/内存/磁盘/网络/磁盘 IO 时序、
 *   文件系统 Top5、最近告警、资源占用 Top 主机、在线目标列表，30 秒自动刷新
 * - **对比模式**：多选主机（上限 4 台，避免序列过密）并行拉取单机指标，
 *   CPU/内存/Load/网络/磁盘 IO 多序列对比 + 当前值摘要表
 *
 * VM 未配置时整体降级提示（configured=false）。
 * PromQL 失败时后端字段为空，前端以空态降级，不整页报错。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Row, Col, Select, Space, Typography, Flex, Empty, Button, Tag, Table, Badge,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { assetsApi, liveDashboardApi, monitoringApi } from '@/api'
import type { HostMetricPoint, LiveDashboardFileSystemRow, LiveDashboardRecentAlert } from '@/types/api'
import OnboardingCard from '@/components/monitoring/OnboardingCard'
import GrafanaLink from '@/components/monitoring/GrafanaLink'
import SeverityTag from '@/components/monitoring/SeverityTag'

const { Text } = Typography

/** 主机清单行（资产列表后端附带 asset_id/asset_type，供对比模式筛选与选型）。 */
type DashboardHost = import('@/types/api').AssetResponse & { asset_id?: string; asset_type?: string }

const RANGE_OPTIONS = [
  { label: '1 小时', value: 60, step: 30 },
  { label: '6 小时', value: 360, step: 60 },
  { label: '24 小时', value: 1440, step: 120 },
  { label: '7 天', value: 10080, step: 1800 },
  { label: '30 天', value: 43200, step: 7200 },
]

/** 对比模式最多同时展示的主机数（多序列可读性上限）。 */
const COMPARE_MAX_HOSTS = 4

type ViewMode = 'global' | 'compare'

/** 对比模式支持的指标字段（CPU/内存/Load/网络/磁盘 IO）。 */
type CompareField = 'cpu' | 'memory' | 'load1' | 'networkIn' | 'networkOut' | 'diskRead' | 'diskWrite'

const COMPARE_CHARTS: { field: CompareField; title: string; unit: string; max?: number }[] = [
  { field: 'cpu', title: 'CPU 使用率对比', unit: '%', max: 100 },
  { field: 'memory', title: '内存使用率对比', unit: '%', max: 100 },
  { field: 'load1', title: 'Load1 对比', unit: '' },
  { field: 'networkIn', title: '网络入站对比', unit: 'MB/s' },
  { field: 'networkOut', title: '网络出站对比', unit: 'MB/s' },
  { field: 'diskRead', title: '磁盘读吞吐对比', unit: 'MB/s' },
  { field: 'diskWrite', title: '磁盘写吞吐对比', unit: 'MB/s' },
]

/** 深浅主题色：优先 CSS 变量，失败回退固定色。 */
const cssColor = (name: string, fallback: string) => {
  if (typeof window === 'undefined' || !window.getComputedStyle) return fallback
  const v = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

const AXIS_INK = () => cssColor('--ink-3', '#8c8c8c')
const SPLIT_LINE = () => cssColor('--line', '#f0f0f0')
const INK = () => cssColor('--ink', '#262626')
const CHART_PALETTE = () => [
  cssColor('--chart-1', '#2563eb'),
  cssColor('--chart-2', '#14b8a6'),
  cssColor('--chart-3', '#f59e0b'),
  cssColor('--chart-4', '#a855f7'),
]

function formatBytes(n?: number | null) {
  if (n == null || Number.isNaN(Number(n))) return '-'
  const bytes = Number(n)
  if (bytes >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(2)} TB`
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${bytes} B`
}

/** 取序列最后一个非空点（对比摘要用「当前值」）。 */
function lastNumber(points: HostMetricPoint[], field: string): number | null {
  for (let i = points.length - 1; i >= 0; i -= 1) {
    const raw = points[i]?.[field]
    if (raw != null && !Number.isNaN(Number(raw))) return Number(raw)
  }
  return null
}

export default function MonitoringDashboard() {
  const [mode, setMode] = useState<ViewMode>('global')
  const [rangeMinutes, setRangeMinutes] = useState(60)
  const [compareIds, setCompareIds] = useState<string[]>([])

  const rangeOption = RANGE_OPTIONS.find((o) => o.value === rangeMinutes) || RANGE_OPTIONS[1]

  // ── 全局模式数据 ──
  const { data: globalData, isLoading: loadingGlobal, refetch: refetchGlobal } = useQuery({
    queryKey: ['live-dashboard-global', rangeMinutes, rangeOption.step],
    queryFn: () => liveDashboardApi.overview({ range_minutes: rangeMinutes, step_seconds: rangeOption.step }),
    enabled: mode === 'global',
    refetchInterval: 30 * 1000,
    refetchOnWindowFocus: false,
  })

  // ── 对比模式数据：主机清单 + 每台主机一份指标 ──
  const { data: assets = [] } = useQuery({
    queryKey: ['hosts'],
    queryFn: assetsApi.list,
    enabled: mode === 'compare',
  })

  const hosts = (assets as DashboardHost[]).filter((a) => a.asset_type === 'host' || !a.asset_type)
  // 后端业务 ID 是 id（可选兼容旧 asset_id）
  const hostKey = (h: DashboardHost) => String(h.id ?? h.asset_id ?? h.ip ?? h.name ?? '')

  // react-query 不适合动态键集合的并行查询，这里按选择串行触发单查询；
  // 上限 4 台 + 30s 刷新，请求量可控。
  const [compareSeries, setCompareSeries] = useState<{ name: string; points: HostMetricPoint[] }[]>([])
  const selectedHosts = hosts.filter((h) => compareIds.includes(hostKey(h)))

  const { isFetching: fetchingCompare, refetch: refetchCompare } = useQuery({
    queryKey: ['live-dashboard-compare', compareIds.join(','), rangeMinutes],
    queryFn: async () => {
      const results = await Promise.all(
        selectedHosts.map(async (host) => {
          const data = await monitoringApi.hostMetrics({
            host_name: host.hostname || host.name,
            host_ip: host.ip || '',
            range_minutes: rangeMinutes,
            step_seconds: rangeOption.step,
          })
          return { name: host.name || host.ip, points: data.metrics || [] }
        }),
      )
      setCompareSeries(results)
      return results
    },
    enabled: mode === 'compare' && compareIds.length > 0,
    refetchInterval: 30 * 1000,
    refetchOnWindowFocus: false,
  })

  /** 全局时序图 option 构造器（key 对应后端 DASHBOARD_RANGE_QUERIES）。 */
  const buildGlobalOption = (keys: { field: string; name: string; dashed?: boolean }[], yAxisUnit = '%') => ({
    grid: { left: 48, right: 16, top: 32, bottom: 32 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number | null) => (v == null ? '-' : `${v.toFixed(2)}${yAxisUnit}`) },
    legend: { top: 4, textStyle: { fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: (globalData?.metrics || []).map((row) => String(row.time)),
      axisLabel: { color: AXIS_INK() },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: AXIS_INK(), formatter: (v: number) => `${v}` },
      splitLine: { lineStyle: { color: SPLIT_LINE() } },
    },
    series: keys.map((k, idx) => ({
      name: k.name,
      type: 'line',
      showSymbol: false,
      smooth: true,
      color: CHART_PALETTE()[idx % 4],
      lineStyle: { width: 1.6, type: k.dashed ? 'dashed' : 'solid' as const },
      data: (globalData?.metrics || []).map((row) => (row[k.field] == null ? null : Number(row[k.field]))),
      connectNulls: true,
    })),
  })

  /** 对比模式图 option：多主机同图。 */
  const buildCompareOption = (field: CompareField, unit: string, max?: number) => ({
    grid: { left: 48, right: 16, top: 36, bottom: 32 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: number | null) => (v == null ? '-' : `${v.toFixed(2)}${unit || ''}`),
    },
    legend: { top: 4, type: 'scroll', textStyle: { fontSize: 11 } },
    xAxis: {
      type: 'category',
      data: compareSeries[0]?.points.map((p) => p.time) || [],
      axisLabel: { color: AXIS_INK() },
    },
    yAxis: {
      type: 'value',
      max,
      axisLabel: { color: AXIS_INK(), formatter: unit === '%' ? '{value}%' : `{value}${unit || ''}` },
      splitLine: { lineStyle: { color: SPLIT_LINE() } },
    },
    series: compareSeries.map((s, idx) => ({
      name: s.name,
      type: 'line',
      showSymbol: false,
      smooth: true,
      color: CHART_PALETTE()[idx % 4],
      connectNulls: true,
      data: s.points.map((p) => (p[field] == null ? null : Number(p[field]))),
    })),
  })

  const summary = globalData?.summary

  const topHostColumns: ColumnsType<Record<string, unknown>> = [
    {
      title: '主机', dataIndex: 'name', width: 160,
      render: (v: string, row) => <Text strong>{v || String(row.ip || '-')}</Text>,
    },
    { title: 'IP', dataIndex: 'ip', width: 130 },
    {
      title: 'CPU', dataIndex: 'cpu', width: 90, align: 'right',
      render: (v: number) => (
        <Text type={Number(v) > 80 ? 'danger' : undefined}>{v == null ? '-' : `${Number(v).toFixed(1)}%`}</Text>
      ),
    },
    {
      title: '内存', dataIndex: 'memory', width: 90, align: 'right',
      render: (v: number) => (
        <Text type={Number(v) > 85 ? 'danger' : undefined}>{v == null ? '-' : `${Number(v).toFixed(1)}%`}</Text>
      ),
    },
  ]

  const fsColumns: ColumnsType<LiveDashboardFileSystemRow> = [
    {
      title: '挂载点', dataIndex: 'mountpoint', width: 120, ellipsis: true,
      render: (v: string, row) => <Text strong>{v || row.device || '-'}</Text>,
    },
    {
      title: '主机', dataIndex: 'host', width: 110, ellipsis: true,
      render: (v: string, row) => <Text type="secondary">{v || row.instance || '-'}</Text>,
    },
    {
      title: '总容量', dataIndex: 'size_bytes', width: 90, align: 'right',
      render: (v: number | null) => formatBytes(v),
    },
    {
      title: '已用%', dataIndex: 'used_pct', width: 80, align: 'right',
      render: (v: number | null) => (
        <Text type={Number(v) >= 85 ? 'danger' : Number(v) >= 70 ? 'warning' : undefined}>
          {v == null ? '-' : `${Number(v).toFixed(1)}%`}
        </Text>
      ),
    },
  ]

  const alertColumns: ColumnsType<LiveDashboardRecentAlert> = [
    {
      title: '名称', dataIndex: 'name', width: 140, ellipsis: true,
      render: (v: string) => <Text strong>{v || '-'}</Text>,
    },
    {
      title: '级别', dataIndex: 'severity', width: 80,
      render: (v: string) => <SeverityTag severity={v} />,
    },
    {
      title: '实例', dataIndex: 'instance', width: 120, ellipsis: true,
      render: (v: string) => <Text type="secondary">{v || '-'}</Text>,
    },
    {
      title: '时间', dataIndex: 'time', width: 130, ellipsis: true,
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v || '-'}</Text>,
    },
  ]

  const compareSummaryColumns: ColumnsType<{ name: string; points: HostMetricPoint[] }> = [
    {
      title: '主机', dataIndex: 'name', width: 140,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: 'CPU', key: 'cpu', width: 80, align: 'right',
      render: (_, row) => {
        const v = lastNumber(row.points, 'cpu')
        return <Text type={v != null && v > 80 ? 'danger' : undefined}>{v == null ? '-' : `${v.toFixed(1)}%`}</Text>
      },
    },
    {
      title: '内存', key: 'memory', width: 80, align: 'right',
      render: (_, row) => {
        const v = lastNumber(row.points, 'memory')
        return <Text type={v != null && v > 85 ? 'danger' : undefined}>{v == null ? '-' : `${v.toFixed(1)}%`}</Text>
      },
    },
    {
      title: 'Load', key: 'load1', width: 80, align: 'right',
      render: (_, row) => {
        const v = lastNumber(row.points, 'load1')
        return v == null ? '-' : v.toFixed(2)
      },
    },
    {
      title: '网络 in·out', key: 'net', width: 140, align: 'right',
      render: (_, row) => {
        const i = lastNumber(row.points, 'networkIn')
        const o = lastNumber(row.points, 'networkOut')
        const fmt = (n: number | null) => (n == null ? '-' : n.toFixed(2))
        return <Text type="secondary">{`${fmt(i)} · ${fmt(o)} MB/s`}</Text>
      },
    },
  ]

  return (
    <div style={{ padding: '0 0 16px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 8 }} wrap gap="small">
        <Space size={12}>
          <Typography.Title level={5} style={{ margin: 0, color: 'var(--ink)' }}>实时看板</Typography.Title>
          <Text type="secondary" style={{ fontSize: 12 }}>30 秒自动刷新</Text>
        </Space>
        <Space size={12} wrap>
          {/* 概要数字并入页头，省掉整行卡片 */}
          {mode === 'global' && (
            <Space size={14} style={{ fontSize: 12, color: AXIS_INK() }}>
              <span>目标 <b style={{ color: INK() }}>{summary?.targets ?? 0}</b></span>
              <span>在线 <b style={{ color: cssColor('--ok', '#3f8600') }}>{summary?.up ?? 0}</b></span>
              <span>离线 <b style={{ color: (summary?.down ?? 0) > 0 ? cssColor('--danger', '#cf1322') : INK() }}>{summary?.down ?? 0}</b></span>
              <span>告警 <b style={{ color: (summary?.alerts ?? 0) > 0 ? cssColor('--warn', '#fa8c16') : INK() }}>{summary?.alerts ?? 0}</b></span>
              <span>
                负载{' '}
                <b style={{ color: summary?.load1 == null ? INK() : (summary.load1 > 8 ? cssColor('--warn', '#fa8c16') : INK()) }}>
                  {summary?.load1 == null ? '-' : Number(summary.load1).toFixed(2)}
                </b>
              </span>
            </Space>
          )}
          {/* 轻量下划线切换（替代 Segmented 块感） */}
          <Space size={4}>
            {([
              { key: 'global', label: '全局态势' },
              { key: 'compare', label: '主机对比' },
            ] as const).map((m) => {
              const active = mode === m.key
              return (
                <Button
                  key={m.key}
                  type="text"
                  size="small"
                  onClick={() => setMode(m.key)}
                  style={{
                    height: 32,
                    padding: '0 10px',
                    fontWeight: active ? 600 : 400,
                    color: active ? 'var(--ink, #0f172a)' : 'var(--ink-3, #94a3b8)',
                    borderBottom: active ? '2px solid var(--accent, #2563eb)' : '2px solid transparent',
                    borderRadius: 0,
                  }}
                >
                  {m.label}
                </Button>
              )
            })}
          </Space>
          <Select
            value={rangeMinutes}
            onChange={setRangeMinutes}
            style={{ width: 110 }}
            options={RANGE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          />
          <GrafanaLink label="更多图表" />
          <Button
            icon={<ReloadOutlined />}
            onClick={() => (mode === 'global' ? refetchGlobal() : refetchCompare())}
            loading={mode === 'global' ? loadingGlobal : fetchingCompare}
          >
            刷新
          </Button>
        </Space>
      </Flex>

      {globalData && globalData.configured === false && mode === 'global' && (
        <Card size="small" style={{ marginBottom: 8 }}>
          <Empty description={globalData.message || 'VictoriaMetrics 未配置，请先在系统管理中接入监控'} style={{ padding: '24px 0' }} />
        </Card>
      )}

      {/* 新手引导（完成即隐藏）；链路自检不占看板，需要时去采集车队/系统配置 */}
      <OnboardingCard />

      {mode === 'global' && globalData?.configured && (
        <>

          {/* 六图：CPU/内存/磁盘/网络 + 磁盘 IO；宽屏两列 */}
          <Row gutter={[8, 8]} style={{ marginBottom: 8 }}>
            <Col xs={24} xl={12}>
              <Card title="CPU 使用率（均值 / P95）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([{ field: 'cpu', name: '均值' }, { field: 'cpu_p95', name: 'P95', dashed: true }])}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="内存使用率（均值 / P95）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([{ field: 'memory', name: '均值' }, { field: 'memory_p95', name: 'P95', dashed: true }])}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="磁盘使用率（均值 / P95）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([{ field: 'disk', name: '均值' }, { field: 'disk_p95', name: 'P95', dashed: true }])}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="网络流量（入 / 出）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([
                    { field: 'networkIn', name: '入' },
                    { field: 'networkOut', name: '出' },
                  ], 'MB/s')}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="磁盘 I/O（读 / 写吞吐）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([
                    { field: 'diskRead', name: '读' },
                    { field: 'diskWrite', name: '写' },
                  ], 'MB/s')}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="Load1（1 分钟平均负载）" size="small" styles={{ body: { padding: 8 } }}>
                <ReactECharts
                  option={buildGlobalOption([{ field: 'load1', name: 'Load1' }], '')}
                  style={{ height: 200 }} notMerge
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[8, 8]} style={{ marginBottom: 8 }}>
            <Col xs={24} lg={10}>
              <Card title="资源占用 Top 主机" size="small">
                <Table
                  rowKey={(r) => String(r.ip || r.name)}
                  size="small"
                  columns={topHostColumns}
                  dataSource={
                    Array.isArray(globalData?.top_hosts)
                      ? (globalData.top_hosts as Record<string, unknown>[])
                      : []
                  }
                  pagination={false}
                  loading={loadingGlobal}
                  locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                />
              </Card>
            </Col>
            <Col xs={24} lg={7}>
              <Card title="文件系统 Top5（按已用%）" size="small">
                <Table
                  rowKey={(r) => `${r.instance || ''}-${r.mountpoint || r.device || ''}`}
                  size="small"
                  columns={fsColumns}
                  dataSource={Array.isArray(globalData?.top_filesystems) ? globalData.top_filesystems : []}
                  pagination={false}
                  loading={loadingGlobal}
                  locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                />
              </Card>
            </Col>
            <Col xs={24} lg={7}>
              <Card
                title={
                  <Flex justify="space-between" align="center">
                    <span>最近告警</span>
                    <Button type="link" size="small" style={{ padding: 0 }} href="/monitoring/alerts">
                      全部
                    </Button>
                  </Flex>
                }
                size="small"
              >
                <Table
                  rowKey={(r) => String(r.id || r.name || r.time)}
                  size="small"
                  columns={alertColumns}
                  dataSource={Array.isArray(globalData?.recent_alerts) ? globalData.recent_alerts : []}
                  pagination={false}
                  loading={loadingGlobal}
                  locale={{ emptyText: <Empty description="暂无告警" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col xs={24}>
              <Card
                title={`采集目标状态（${(globalData?.services || []).length}）`}
                size="small"
              >
                <div style={{ maxHeight: 260, overflow: 'auto' }}>
                  {(globalData?.services || []).map((svc, idx) => (
                    <Flex
                      key={`${svc.ip}-${idx}`}
                      justify="space-between"
                      style={{ padding: '5px 0', borderBottom: `1px solid ${SPLIT_LINE()}` }}
                    >
                      <Space>
                        <Badge
                          status={svc.status === 'healthy' ? 'success' : 'error'}
                          text={<Text strong>{svc.name || svc.ip}</Text>}
                        />
                        <Text type="secondary">{svc.ip}</Text>
                      </Space>
                      <Space size={6}>
                        {svc.provider && svc.provider !== 'unknown' && <Tag>{String(svc.provider)}</Tag>}
                        {svc.region && <Text type="secondary" style={{ fontSize: 12 }}>{String(svc.region)}</Text>}
                      </Space>
                    </Flex>
                  ))}
                  {(globalData?.services || []).length === 0 && (
                    <Empty description="暂无采集目标" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ padding: '32px 0' }} />
                  )}
                </div>
              </Card>
            </Col>
          </Row>
        </>
      )}

      {mode === 'compare' && (
        <Card size="small">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Flex justify="space-between" wrap gap="small">
              <Select
                mode="multiple"
                value={compareIds}
                onChange={(ids) => setCompareIds((ids as string[]).slice(0, COMPARE_MAX_HOSTS))}
                placeholder={`选择要对比的主机（最多 ${COMPARE_MAX_HOSTS} 台）`}
                style={{ minWidth: 360 }}
                showSearch optionFilterProp="label"
                options={hosts.map((h) => ({ value: hostKey(h), label: `${h.name || h.hostname || '主机'}（${h.ip || '-'}）` }))}
              />
              <Text type="secondary">
                {compareIds.length ? `已选 ${compareIds.length} 台` : '选择主机后开始对比'}
              </Text>
            </Flex>

            {compareIds.length === 0 ? (
              <Empty description="选择主机后展示 CPU / 内存 / Load / 网络 / 磁盘 IO 对比曲线" style={{ padding: '60px 0' }} />
            ) : (
              <>
                <Card title="对比摘要（当前值）" size="small">
                  <Table
                    rowKey={(r) => r.name}
                    size="small"
                    columns={compareSummaryColumns}
                    dataSource={compareSeries}
                    pagination={false}
                    loading={fetchingCompare && compareSeries.length === 0}
                    locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                  />
                </Card>
                <Row gutter={[8, 8]}>
                  {COMPARE_CHARTS.map((chart) => (
                    <Col xs={24} xl={12} key={chart.field}>
                      <Card title={chart.title} size="small" styles={{ body: { padding: 8 } }}>
                        <ReactECharts
                          option={buildCompareOption(chart.field, chart.unit, chart.max)}
                          style={{ height: 220 }}
                          notMerge
                        />
                      </Card>
                    </Col>
                  ))}
                </Row>
              </>
            )}
          </Space>
        </Card>
      )}
    </div>
  )
}
