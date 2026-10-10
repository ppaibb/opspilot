import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Drawer, Segmented, Button, Spin, Flex, Typography, Tooltip, Space } from 'antd'
import { ReloadOutlined, LineChartOutlined, ExportOutlined, FileTextOutlined, AlertOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { monitoringApi } from '@/api'
import type { HostMetricPoint } from '@/types/api'
import MetricChart from '@/components/monitoring/MetricChart'
import { CHART_COLORS, RANGE_OPTIONS } from '@/components/monitoring/chartTheme'
import GrafanaLink from '@/components/monitoring/GrafanaLink'
import EmptyState from '@/components/monitoring/EmptyState'

const { Text } = Typography

// Host 抽屉用的时间窗：默认 1 小时（曲线即时可见），再看 24h/7d 趋势
const DRAWER_RANGES = RANGE_OPTIONS.filter((r) => ['5m', '1h', '24h', '7d'].includes(r.value))

interface Props {
  open: boolean
  assetName?: string
  assetIp?: string
  onClose: () => void
}

/** 白底图卡：标题行 + 图表体。minWidth:0 防 grid 列被 nowrap 口径文本撑爆溢出。 */
function ChartCard({ title, color, children, wide = false }: { title: string; color: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div
      style={{
        background: '#fff',
        border: '1px solid #f0f0f0',
        borderRadius: 10,
        padding: '12px 12px 6px',
        minWidth: 0,
        overflow: 'hidden',
        gridColumn: wide ? '1 / -1' : undefined,
      }}
    >
      <Flex align="center" gap={8} style={{ marginBottom: 2 }}>
        <LineChartOutlined style={{ color }} />
        <Text strong style={{ fontSize: 14 }}>{title}</Text>
      </Flex>
      {children}
    </div>
  )
}

export default function HostMetricsDrawer({ open, assetName, assetIp, onClose }: Props) {
  const navigate = useNavigate()
  const [rangeKey, setRangeKey] = useState('1h')
  const rangeCfg = DRAWER_RANGES.find((r) => r.value === rangeKey) || DRAWER_RANGES[1]

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['host-metrics', assetIp, assetName, rangeKey],
    queryFn: () =>
      monitoringApi.hostMetrics({
        host_ip: assetIp,
        host_name: assetName,
        range_minutes: rangeCfg.minutes,
        step_seconds: rangeCfg.step,
      }),
    enabled: open && (!!assetIp || !!assetName),
    refetchOnWindowFocus: false,
  })

  const points: HostMetricPoint[] = useMemo(() => data?.metrics ?? [], [data])
  const times = useMemo(() => points.map((p) => p.time), [points])
  const cpuVals = useMemo(() => points.map((p) => (p.cpu == null ? null : Number(p.cpu))), [points])
  const memVals = useMemo(() => points.map((p) => (p.memory == null ? null : Number(p.memory))), [points])
  const netInVals = useMemo(() => points.map((p) => (p.networkIn == null ? null : Number(p.networkIn))), [points])
  const netOutVals = useMemo(() => points.map((p) => (p.networkOut == null ? null : Number(p.networkOut))), [points])
  // 物理盘使用率：后端按 disk_<盘名> 返回多序列
  const partitions = useMemo(() => data?.disk_partitions ?? [], [data])
  const diskSeries = useMemo(
    () =>
      partitions.map((p, i) => ({
        name: p,
        values: points.map((pt) => {
          const v = pt[`disk_${p}`]
          return v == null ? null : Number(v)
        }),
        color: CHART_COLORS[i % CHART_COLORS.length],
      })),
    [points, partitions],
  )

  const notConfigured = data && data.configured === false
  const forbidden = data && (data as { status?: number }).status === 403

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={760}
      closable
      styles={{ body: { background: '#f5f6fa', padding: 16 } }}
      title={
        <Flex align="center" justify="space-between" style={{ width: '100%' }}>
          <Flex align="center" gap={10}>
            <LineChartOutlined style={{ color: '#1677ff', fontSize: 18 }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 15, lineHeight: 1.2 }}>{assetName || '主机监控'}</div>
              <div style={{ fontSize: 12, color: '#94a3b8' }}>{assetIp}</div>
            </div>
          </Flex>
          <Flex align="center" gap={8}>
            <Segmented
              size="small"
              value={rangeKey}
              onChange={(v) => setRangeKey(v as string)}
              options={DRAWER_RANGES.map((r) => ({ label: r.label, value: r.value }))}
            />
            <Tooltip title="刷新">
              <Button size="small" type="text" icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()} />
            </Tooltip>
          </Flex>
        </Flex>
      }
      footer={
        <Flex justify="space-between">
          <Space>
            <Button
              type="link"
              size="small"
              icon={<FileTextOutlined />}
              onClick={() => navigate(`/monitoring/logs${assetIp ? `?host=${encodeURIComponent(assetIp)}` : ''}`)}
            >
              相关日志
            </Button>
            <Button
              type="link"
              size="small"
              icon={<AlertOutlined />}
              onClick={() => navigate(`/monitoring/alerts${assetIp ? `?instance=${encodeURIComponent(assetIp)}` : ''}`)}
            >
              相关告警
            </Button>
          </Space>
          <Space>
            <GrafanaLink
              host={assetIp}
              from={`now-${rangeCfg.minutes}m`}
              to="now"
              expr="up"
              label="Grafana 更多数据"
            />
            <Button type="link" icon={<ExportOutlined />} onClick={() => navigate('/monitoring/live')}>
              查看完整大盘
            </Button>
          </Space>
        </Flex>
      }
    >
      {forbidden ? (
        <EmptyState kind="forbidden" />
      ) : notConfigured ? (
        <EmptyState kind="not-connected" description={data?.message || 'VictoriaMetrics 未配置，暂无法展示监控指标'} />
      ) : isFetching && points.length === 0 ? (
        <Flex align="center" justify="center" style={{ height: 240 }}><Spin /></Flex>
      ) : points.length === 0 ? (
        <EmptyState
          kind="no-result"
          title="该主机暂无指标"
          description="确认监控栈内置 Alloy 或主机 Agent 已在采集，且 instance 标签与本机 IP/主机名匹配。"
        />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <ChartCard title="CPU 使用率" color="#2f54eb">
            <MetricChart
              times={times}
              series={[{ name: 'CPU', values: cpuVals, color: '#2f54eb' }]}
              unit="%"
              area
              height={200}
              subtitle={'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) * 100'}
              emptyText="暂无 CPU 数据"
            />
          </ChartCard>
          <ChartCard title="内存使用率" color="#1677ff">
            <MetricChart
              times={times}
              series={[{ name: '内存', values: memVals, color: '#1677ff' }]}
              unit="%"
              area
              height={200}
              subtitle={'(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100'}
              emptyText="暂无内存数据"
            />
          </ChartCard>
          {diskSeries.length > 0 ? (
            <ChartCard title="磁盘使用率" color="#722ed1" wide>
              <MetricChart
                times={times}
                series={diskSeries}
                unit="%"
                height={200}
                subtitle={`(1 - filesystem_free / filesystem_size) * 100 · 盘：${partitions.join('、')}`}
                emptyText="暂无磁盘数据"
              />
            </ChartCard>
          ) : null}
          <ChartCard title="网络流量" color="#52c41a" wide>
            <MetricChart
              times={times}
              series={[
                { name: '入站', values: netInVals, color: '#52c41a' },
                { name: '出站', values: netOutVals, color: '#fa8c16' },
              ]}
              unit="MB/s"
              height={200}
              subtitle={'rate(node_network_receive/transmit_bytes_total[5m]) / 1024 / 1024'}
              emptyText="暂无网络数据"
            />
          </ChartCard>
        </div>
      )}
    </Drawer>
  )
}
