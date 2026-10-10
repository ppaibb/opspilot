/**
 * 主机日志抽屉：从主机列表「日志」灯打开，按 host/instance 预筛最近日志。
 * 与 HostMetricsDrawer 对称；完整查询跳 /monitoring/logs?host=。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import {
  Drawer, Segmented, Button, Spin, Flex, Typography, Tooltip, Space, Tag, Table, Alert, Input,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, FileTextOutlined, ExportOutlined, LineChartOutlined, AlertOutlined, SearchOutlined,
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

/**
 * 把用户输入编译成 LogsQL 过滤串：
 * - 中文整词裸搜（`告警`）在 VLogs 分词下 0 命中，必须包通配 `*告警*` 才是子串匹配；
 * - 多词按空格拆开、逐词包通配后并列（空格即 AND，已实测）；
 * - 逐词清洗 LogsQL 元字符（`"` `[` 等会 400），白名单外一律剔除。
 */
function toLogsQL(input: string): string {
  return (
    input
      .trim()
      .split(/\s+/)
      .map((t) => t.replace(/[^A-Za-z0-9_.一-鿿-]/g, ''))
      .filter(Boolean)
      .map((t) => `*${t}*`)
      .join(' ') || '*'
  )
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
  const [keyword, setKeyword] = useState('')
  const [submitted, setSubmitted] = useState('')

  const { data, isFetching, refetch, isError, error } = useQuery({
    queryKey: ['host-logs', assetIp, assetName, rangeValue, submitted],
    queryFn: async () => {
      const end = new Date()
      const start = new Date(end.getTime() - rangeCfg.minutes * 60 * 1000)
      return logsApi.query({
        // 空=全部；有词=通配子串全文过滤（服务端查，不只筛已加载的 100 条）
        query: toLogsQL(submitted),
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
    // 搜索/切窗时保留上一份结果：工具条与表格不闪断，结果到了再替换
    placeholderData: keepPreviousData,
  })

  // 后端行 id 实为流 ID（同容器日志共用，100 行可能只有 4 个 id），
  // 直接 rowKey="id" 会因 key 重复导致翻页时旧行删不掉、新行追加。
  // 用全量数组下标拼复合 key，分页切片后依然唯一且稳定（与监控日志页同款规避）。
  const rows = useMemo(
    () =>
      (data?.logs || []).map((line: LogLine, i: number) => {
        const n = normalizeLog(line)
        return { ...n, rowKey: `${i}-${n.time}` }
      }).slice().reverse(),
    [data],
  )

  // 表格撑满抽屉：卡片高度由 flex 决定，按实测高度回推 scroll.y，
  // 消除分页下方的大片留白（原来写死 360）。
  const cardRef = useRef<HTMLDivElement>(null)
  const [tableY, setTableY] = useState(360)
  useEffect(() => {
    if (!open) return
    const el = cardRef.current
    if (!el) return
    const calc = () => {
      // 卡片内边距 + 汇总行 + 表头 + 分页
      setTableY(Math.max(200, el.clientHeight - 16 - 28 - 40 - 56))
    }
    calc()
    const ro = new ResizeObserver(calc)
    ro.observe(el)
    return () => ro.disconnect()
  }, [open, rows.length])

  const notConfigured = data && data.configured === false
  const queryError = isError ? extractAxiosDetail(error) : null

  const columns: ColumnsType<ReturnType<typeof normalizeLog>> = [
    {
      title: '时间',
      dataIndex: 'time',
      width: 150,
      fixed: 'left',
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
      fixed: 'left',
      render: (v: string) => <Tag color={LEVEL_COLOR[v] || 'default'} style={{ margin: 0 }}>{v}</Tag>,
    },
    {
      title: '消息',
      dataIndex: 'message',
      width: 900,
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
      width={760}
      closable
      styles={{ body: { background: '#f5f6fa', padding: 16, display: 'flex', flexDirection: 'column' } }}
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
      ) : !data && queryError ? (
        // 首次加载就失败：还没有工具条，直接整屏报错
        <Alert type="error" showIcon message="日志查询失败" description={queryError} style={{ marginBottom: 12 }} />
      ) : !data ? (
        <Flex align="center" justify="center" style={{ height: 240 }}><Spin /></Flex>
      ) : (
        // 只要拿过一次数据，工具条（汇总+搜索）就常驻——
        // 空结果/查询失败收进卡片内部，不再整体替换掉搜索框
        <div
          ref={cardRef}
          style={{
            background: '#fff',
            border: '1px solid #f0f0f0',
            borderRadius: 10,
            padding: 8,
            flex: 1,
            minHeight: 0,
            overflow: 'hidden',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <div
            style={{
              padding: '4px 8px 8px',
              fontSize: 12,
              color: '#64748b',
              flexShrink: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <div style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              最近 {rows.length} 条 · {rangeCfg.label}
              {submitted ? ` · 搜索「${submitted}」` : ''}
            </div>
            <Input.Search
              size="small"
              placeholder="搜索日志内容"
              style={{ width: 210 }}
              allowClear
              enterButton={<SearchOutlined />}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onSearch={(v) => setSubmitted(v.trim())}
            />
          </div>
          {queryError ? (
            <Alert type="error" showIcon message="日志查询失败" description={queryError} style={{ margin: '4px 8px' }} />
          ) : rows.length === 0 ? (
            <div
              style={{
                flex: 1,
                minHeight: 0,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <EmptyState
                kind="no-result"
                title={submitted ? '无匹配日志' : '该主机暂无日志'}
                description={
                  submitted
                    ? `时间范围内没有命中「${submitted}」的日志，换个关键词或时间窗试试。`
                    : '确认监控栈 Alloy 已采集 Docker 日志，且时间范围内有容器输出。'
                }
              />
            </div>
          ) : (
            <Table
              dataSource={rows}
              columns={columns}
              rowKey="rowKey"
              size="small"
              loading={isFetching}
              pagination={{ pageSize: 20, hideOnSinglePage: true }}
              scroll={{ x: 1120, y: tableY }}
              style={{ flex: 1, minHeight: 0 }}
            />
          )}
        </div>
      )}
    </Drawer>
  )
}
