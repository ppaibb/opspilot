/**
 * 主机日志抽屉：从主机列表「日志」灯打开，按 host/instance 预筛最近日志。
 * 与 HostMetricsDrawer 对称；完整查询跳 /monitoring/logs?host=。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Drawer, Segmented, Button, Spin, Flex, Typography, Tooltip, Space, Tag, Table, Alert,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, FileTextOutlined, ExportOutlined, LineChartOutlined, AlertOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { logsApi, type LogLine } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import { normalizeLog, formatLogTime } from '@/pages/monitoring/logs/normalizeLog'
import EmptyState from '@/components/monitoring/EmptyState'
import GrafanaLink from '@/components/monitoring/GrafanaLink'

const { Text } = Typography

const RANGE_OPTIONS = [
  { label: '15 分钟', value: 15, minutes: 15 },
  { label: '1 小时', value: 60, minutes: 60 },
  { label: '6 小时', value: 360, minutes: 360 },
  { label: '24 小时', value: 1440, minutes: 1440 },
]

const LEVEL_COLOR: Record<string, string> = {
  ERROR: 'red', FATAL: 'red', CRITICAL: 'red',
  WARN: 'orange', WARNING: 'orange',
  INFO: 'blue', DEBUG: 'default', TRACE: 'default',
}

interface Props {
  open: boolean
  assetName?: string
  assetIp?: string
  onClose: () => void
}

export default function HostLogsDrawer({ open, assetName, assetIp, onClose }: Props) {
  const navigate = useNavigate()
  const [rangeValue, setRangeValue] = useState(60)
  const rangeCfg = RANGE_OPTIONS.find((r) => r.value === rangeValue) || RANGE_OPTIONS[1]

  const { data, isFetching, refetch, isError, error } = useQuery({
    queryKey: ['host-logs', assetIp, assetName, rangeValue],
    queryFn: async () => {
      const end = new Date()
      const start = new Date(end.getTime() - rangeCfg.minutes * 60 * 1000)
      return logsApi.query({
        query: '*',
        start: start.toISOString(),
        end: end.toISOString(),
        limit: 100,
        host_ips: assetIp ? [assetIp] : [],
        host_names: assetName && !assetIp ? [assetName] : [],
      })
    },
    enabled: open && (!!assetIp || !!assetName),
    refetchOnWindowFocus: false,
    retry: false,
  })

  const rows = useMemo(
    () => (data?.logs || []).map((line: LogLine) => normalizeLog(line)).slice().reverse(),
    [data],
  )

  const notConfigured = data && data.configured === false
  const queryError = isError ? extractAxiosDetail(error) : null

  const columns: ColumnsType<ReturnType<typeof normalizeLog>> = [
    {
      title: '时间',
      dataIndex: 'time',
      width: 150,
      render: (v: string) => (
        <Text style={{ fontSize: 12, color: 'var(--ink-2)', fontFamily: 'monospace' }}>
          {formatLogTime(v)}
        </Text>
      ),
    },
    {
      title: '级别',
      dataIndex: 'level',
      width: 70,
      render: (v: string) => <Tag color={LEVEL_COLOR[v] || 'default'} style={{ margin: 0 }}>{v}</Tag>,
    },
    {
      title: '消息',
      dataIndex: 'message',
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <Text style={{ fontSize: 12 }}>{v || '-'}</Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={680}
      closable
      styles={{ body: { background: '#f5f6fa', padding: 16 } }}
      title={
        <Flex align="center" justify="space-between" style={{ width: '100%' }}>
          <Flex align="center" gap={10}>
            <FileTextOutlined style={{ color: '#16a34a', fontSize: 18 }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 15, lineHeight: 1.2 }}>{assetName || '主机日志'}</div>
              <div style={{ fontSize: 12, color: '#94a3b8' }}>{assetIp}</div>
            </div>
          </Flex>
          <Flex align="center" gap={8}>
            <Segmented
              size="small"
              value={rangeValue}
              onChange={(v) => setRangeValue(v as number)}
              options={RANGE_OPTIONS.map((r) => ({ label: r.label, value: r.value }))}
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
              icon={<LineChartOutlined />}
              onClick={() => navigate(`/monitoring/live`)}
            >
              实时看板
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
              label="Grafana"
            />
            <Button
              type="link"
              icon={<ExportOutlined />}
              onClick={() => navigate(`/monitoring/logs${assetIp ? `?host=${encodeURIComponent(assetIp)}` : ''}`)}
            >
              完整查询
            </Button>
          </Space>
        </Flex>
      }
    >
      {notConfigured ? (
        <EmptyState kind="not-connected" description={data?.message || 'VictoriaLogs 未配置，暂无法展示日志'} />
      ) : queryError ? (
        <Alert type="error" showIcon message="日志查询失败" description={queryError} style={{ marginBottom: 12 }} />
      ) : isFetching && rows.length === 0 ? (
        <Flex align="center" justify="center" style={{ height: 240 }}><Spin /></Flex>
      ) : rows.length === 0 ? (
        <EmptyState
          kind="no-result"
          title="该主机暂无日志"
          description="确认监控栈 Alloy 已采集 Docker 日志，且时间范围内有容器输出。"
        />
      ) : (
        <div
          style={{
            background: '#fff',
            border: '1px solid #f0f0f0',
            borderRadius: 10,
            padding: 8,
          }}
        >
          <div style={{ padding: '4px 8px 8px', fontSize: 12, color: '#64748b' }}>
            最近 {rows.length} 条 · {rangeCfg.label}
          </div>
          <Table
            dataSource={rows}
            columns={columns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            scroll={{ y: 360 }}
          />
        </div>
      )}
    </Drawer>
  )
}
