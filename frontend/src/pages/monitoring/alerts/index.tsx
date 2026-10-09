/**
 * 告警中心（/monitoring/alerts）。
 *
 * 布局：页头（统计句 + 主操作）/ 状态 Tab（活动·已恢复·已静默）/ 筛选条一行 /
 * 主次分区（列表 2/3 + 说明快捷入口 1/3）。
 * 行内一键确认、静默；异常严重度置顶；空态可操作。
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Space, Tag, Button, Select, Input, Drawer, Descriptions,
  App, Badge, Popconfirm, Alert, Timeline, Tooltip, Typography, Card, Modal,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  BellOutlined, CheckOutlined, EyeInvisibleOutlined, ReloadOutlined, DownloadOutlined,
  RobotOutlined, HistoryOutlined, ClockCircleOutlined, LinkOutlined,
} from '@ant-design/icons'
import { Link as RouterLink } from 'react-router-dom'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'
import client from '@/api/client'
import { monitoringApi, systemApi } from '@/api'
import type { HistoryAlertItem } from '@/types/api'
import AlertRcaDrawer from '@/components/alert/AlertRcaDrawer'
import RootCauseLibrary from '@/components/alert/RootCauseLibrary'
import SeverityTag from '@/components/monitoring/SeverityTag'
import EmptyState from '@/components/monitoring/EmptyState'
import { PageHeader, FilterBar, UnderlineTabs, MainSplit, SideCard } from '@/components/monitoring/PageShell'
import {
  applyAlertFilters, countBySeverity,
  type AlertListRow, type AlertStatusTab,
} from './alertList'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

const { Search } = Input
const { Text, Paragraph } = Typography

/** GET /monitoring/alerts/active 返回的活跃告警。 */
interface ActiveAlert {
  fingerprint: string
  name: string
  instance?: string
  severity?: string
  summary?: string
  description?: string
  status?: string
  started_at?: string
  acknowledged?: boolean
  acknowledged_by?: string
  acknowledged_at?: string
  acknowledged_note?: string
  labels?: Record<string, string>
}

interface ActiveAlertsResponse {
  alerts?: ActiveAlert[]
}

function relativeOrAbsolute(time?: string) {
  if (!time) return '-'
  const d = dayjs(time)
  if (!d.isValid()) return time
  const abs = d.format('YYYY-MM-DD HH:mm:ss')
  return (
    <Tooltip title={abs}>
      <span>{d.fromNow()}</span>
    </Tooltip>
  )
}

/** 严重度左边色条（不只靠 Tag 颜色）。 */
const SEVERITY_BAR: Record<string, string> = {
  critical: 'var(--danger, #dc2626)',
  warning: 'var(--warn, #d97706)',
  info: 'var(--chart-1, #2563eb)',
  suppressed: 'var(--ink-3, #94a3b8)',
}

const TAB_ITEMS: { key: AlertStatusTab; label: string }[] = [
  { key: 'active', label: '活动' },
  { key: 'resolved', label: '已恢复' },
  { key: 'silenced', label: '已静默' },
]

export default function AlertsPage() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [selectedAlert, setSelectedAlert] = useState<ActiveAlert | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [rcaAlert, setRcaAlert] = useState<ActiveAlert | null>(null)
  const [rcaLibraryOpen, setRcaLibraryOpen] = useState(false)
  // 认领弹窗：认领=接手处理，可附处理备注（做了什么/如何跟进）
  const [ackTarget, setAckTarget] = useState<{ fingerprint: string; name: string } | null>(null)
  const [ackNote, setAckNote] = useState('')
  const [tab, setTab] = useState<AlertStatusTab>('active')
  const [filters, setFilters] = useState({
    severity: 'all',
    search: '',
  })

  const { data, isLoading, refetch, isError, error } = useQuery({
    queryKey: ['active-alerts'],
    queryFn: () => client.get<ActiveAlertsResponse>('/monitoring/alerts/active').then(r => r.data),
    refetchInterval: 30000,
    retry: false,
  })

  const { data: historyData, isLoading: loadingHistory } = useQuery({
    queryKey: ['alert-history', 'resolved'],
    queryFn: () => monitoringApi.alertHistory({ state: 'resolved', limit: 100 }),
    enabled: tab === 'resolved',
    retry: false,
  })

  const alerts = data?.alerts || []

  // 活动 + 历史归一成公共行，供三个 Tab 过滤
  const allRows = useMemo<AlertListRow[]>(() => {
    const active: AlertListRow[] = alerts.map((a) => ({ ...a, fromHistory: false }))
    const history: AlertListRow[] = (historyData?.alerts || []).map((h: HistoryAlertItem) => ({
      fingerprint: h.id != null ? String(h.id) : undefined,
      id: h.id,
      name: h.name,
      severity: h.severity,
      instance: h.instance,
      summary: h.summary,
      status: h.status || 'resolved',
      started_at: h.started_at,
      resolved_at: h.resolved_at,
      fromHistory: true,
    }))
    return [...active, ...history]
  }, [alerts, historyData])

  const rows = useMemo(
    () => applyAlertFilters(allRows, tab, filters),
    [allRows, tab, filters],
  )
  const stats = useMemo(() => countBySeverity(allRows), [allRows])

  // 详情时间线数据：通知发送记录（触发/恢复）
  const { data: notifyLogs } = useQuery({
    queryKey: ['alert-notify-logs', selectedAlert?.fingerprint],
    queryFn: () => systemApi.notificationLogs({ fingerprint: selectedAlert!.fingerprint, page_size: 20 }),
    enabled: drawerOpen && !!selectedAlert?.fingerprint,
  })

  const timelineItems = useMemo(() => {
    if (!selectedAlert) return []
    const items: Array<{ color: string; children: React.ReactNode }> = []
    items.push({
      color: 'red',
      children: (
        <div>
          <div><strong>触发</strong> {relativeOrAbsolute(selectedAlert.started_at)}</div>
          <div style={{ color: 'var(--ink-2)', fontSize: 12 }}>{selectedAlert.summary || selectedAlert.name}</div>
        </div>
      ),
    })
    for (const log of (notifyLogs?.logs || []) as Array<Record<string, unknown>>) {
      const ok = Boolean(log.success)
      items.push({
        color: ok ? 'green' : 'red',
        children: (
          <div>
            <div>
              <strong>{log.event_type === 'resolved' ? '恢复通知' : '通知'}</strong>{' '}
              {relativeOrAbsolute(String(log.created_at || ''))}
            </div>
            <div style={{ color: 'var(--ink-2)', fontSize: 12 }}>
              渠道 {String(log.channel_name || '-')} · {ok ? '成功' : `失败：${String(log.message || '').slice(0, 80)}`}
            </div>
          </div>
        ),
      })
    }
    if (selectedAlert.acknowledged) {
      items.push({
        color: 'blue',
        children: (
          <div>
            <div><strong>认领</strong> {relativeOrAbsolute(selectedAlert.acknowledged_at)}</div>
            <div style={{ color: 'var(--ink-2)', fontSize: 12 }}>
              由 {selectedAlert.acknowledged_by || '未知用户'} 认领
              {selectedAlert.acknowledged_note ? ` · 备注：${selectedAlert.acknowledged_note}` : ''}
            </div>
          </div>
        ),
      })
    }
    if (selectedAlert.status === 'resolved') {
      items.push({
        color: 'gray',
        children: <div><strong>恢复</strong></div>,
      })
    }
    return items
  }, [selectedAlert, notifyLogs])

  const acknowledgeMut = useMutation({
    mutationFn: (d: { fingerprint: string; note: string }) =>
      client.post(`/monitoring/alerts/${d.fingerprint}/acknowledge`, { note: d.note }).then(r => r.data),
    onSuccess: (_d, v) => {
      queryClient.invalidateQueries({ queryKey: ['active-alerts'] })
      setAckTarget(null)
      setAckNote('')
      message.success(v.note ? '已认领，备注已记录' : '已认领')
    },
  })

  const unacknowledgeMut = useMutation({
    mutationFn: (fingerprint: string) =>
      client.post(`/monitoring/alerts/${fingerprint}/unacknowledge`).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['active-alerts'] })
      message.success('已释放认领，告警回到未认领状态')
    },
  })

  const silenceMut = useMutation({
    mutationFn: (d: { alert_name: string; instance?: string; duration_minutes: number }) =>
      client.post('/monitoring/alerts/silence', d).then(r => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['active-alerts'] })
      message.success('已屏蔽')
    },
  })

  const openDetail = (record: AlertListRow) => {
    setSelectedAlert({
      fingerprint: record.fingerprint || '',
      name: record.name,
      instance: record.instance,
      severity: record.severity,
      summary: record.summary,
      status: record.status,
      started_at: record.started_at,
      acknowledged: record.acknowledged,
      acknowledged_by: record.acknowledged_by,
      acknowledged_at: record.acknowledged_at,
      acknowledged_note: record.acknowledged_note,
      labels: record.labels,
    })
    setDrawerOpen(true)
  }

  const columns: ColumnsType<AlertListRow> = [
    {
      title: '严重程度',
      dataIndex: 'severity',
      key: 'severity',
      width: 120,
      render: (severity: string, record) => (
        <div
          style={{
            borderLeft: `3px solid ${SEVERITY_BAR[(severity || '').toLowerCase()] || 'var(--line)'}`,
            paddingLeft: 8,
          }}
        >
          <SeverityTag severity={severity} />
          {record.acknowledged ? (
            <Tooltip title={record.acknowledged_note ? `备注：${record.acknowledged_note}` : '已认领'}>
              <div style={{ fontSize: 11, color: 'var(--ok, #16a34a)', marginTop: 2 }}>
                已认领{record.acknowledged_by ? ` · ${record.acknowledged_by}` : ''}
              </div>
            </Tooltip>
          ) : null}
        </div>
      ),
    },
    {
      title: '告警名称',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      ellipsis: true,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '实例',
      dataIndex: 'instance',
      key: 'instance',
      width: 130,
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v || '-'}</Text>,
    },
    {
      title: '摘要',
      dataIndex: 'summary',
      key: 'summary',
      ellipsis: true,
      render: (v: string) => v || '-',
    },
    {
      title: '触发时间',
      dataIndex: 'started_at',
      key: 'started_at',
      width: 120,
      render: (time: string) => relativeOrAbsolute(time),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (status: string, record) => {
        if (record.fromHistory || status === 'resolved') {
          return <Badge status="default" text="已恢复" />
        }
        if ((record.severity || '').toLowerCase() === 'suppressed') {
          return <Badge status="warning" text="已静默" />
        }
        return (
          <Badge
            status={status === 'firing' ? 'error' : 'warning'}
            text={status === 'firing' ? '告警中' : '待定'}
          />
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_: unknown, record) => {
        const canAct = !record.fromHistory && (record.status || '') !== 'resolved'
        return (
          <Space size={4} wrap>
            <Button size="small" icon={<BellOutlined />} onClick={() => openDetail(record)}>
              详情
            </Button>
            {canAct && !record.acknowledged && record.fingerprint ? (
              <Button
                size="small"
                type="primary"
                ghost
                icon={<CheckOutlined />}
                onClick={() => { setAckTarget({ fingerprint: record.fingerprint!, name: record.name }); setAckNote('') }}
              >
                认领
              </Button>
            ) : null}
            {canAct ? (
              <Popconfirm
                title="确认屏蔽此告警 1 小时？"
                onConfirm={() => silenceMut.mutate({
                  alert_name: record.name,
                  instance: record.instance,
                  duration_minutes: 60,
                })}
              >
                <Button size="small" icon={<EyeInvisibleOutlined />}>
                  静默
                </Button>
              </Popconfirm>
            ) : null}
            {canAct ? (
              <Button
                size="small"
                icon={<RobotOutlined />}
                onClick={() => setRcaAlert({
                  fingerprint: record.fingerprint || '',
                  name: record.name,
                  instance: record.instance,
                  severity: record.severity,
                  summary: record.summary,
                  labels: record.labels,
                })}
              >
                根因
              </Button>
            ) : null}
          </Space>
        )
      },
    },
  ]

  if (isError) {
    const status = (error as { response?: { status?: number } })?.response?.status
    return (
      <EmptyState
        kind={status === 403 ? 'forbidden' : 'no-result'}
        title={status === 403 ? undefined : '告警加载失败'}
        description={status === 403 ? undefined : '下一步：确认告警服务可用后点刷新。'}
        actionLabel="刷新"
        onAction={() => refetch()}
      />
    )
  }

  const emptyDesc = tab === 'active'
    ? '当前没有活动告警。可到「告警规则」确认规则是否启用，或放宽级别筛选。'
    : tab === 'resolved'
      ? '最近没有已恢复的告警记录。'
      : '当前没有被静默的告警。行内点「静默」可临时屏蔽吵闹告警。'

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <PageHeader
        title="告警中心"
        subtitle={
          <Space size={12}>
            <span>活动 <b style={{ color: 'var(--ink)' }}>{stats.total}</b></span>
            <span>严重 <b style={{ color: stats.critical > 0 ? 'var(--danger)' : 'var(--ink)' }}>{stats.critical}</b></span>
            <span>警告 <b style={{ color: stats.warning > 0 ? 'var(--warn)' : 'var(--ink)' }}>{stats.warning}</b></span>
            <span>已静默 <b>{stats.silenced}</b></span>
            <span>已恢复 <b>{stats.resolved}</b></span>
          </Space>
        }
        extra={
          <>
            <Button icon={<HistoryOutlined />} onClick={() => setRcaLibraryOpen(true)}>
              根因库
            </Button>
            <Button
              icon={<DownloadOutlined />}
              onClick={() => {
                // 导出当前活动告警（CSV，UTF-8 BOM）
                if (!alerts.length) {
                  message.warning('当前没有活动告警可导出')
                  return
                }
                const headers = ['告警名称', '严重度', '实例', '摘要', '状态', '触发时间']
                const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
                const rows = alerts.map((a) =>
                  [a.name, a.severity, a.instance, a.summary || a.description, a.status, a.started_at]
                    .map(esc).join(','),
                )
                const csv = [headers.join(','), ...rows].join('\r\n')
                const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
                const link = document.createElement('a')
                link.href = URL.createObjectURL(blob)
                link.download = `活动告警-${dayjs().format('YYYYMMDD')}.csv`
                link.click()
                URL.revokeObjectURL(link.href)
                message.success(`已导出 ${alerts.length} 条活动告警`)
              }}
            >
              导出 CSV
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => refetch()} loading={isLoading}>
              刷新
            </Button>
          </>
        }
      />

      <FilterBar
        left={
          <>
            <UnderlineTabs value={tab} onChange={setTab} items={TAB_ITEMS} />
            <Select
              value={filters.severity}
              onChange={v => setFilters({ ...filters, severity: v })}
              style={{ width: 110 }}
              options={[
                { value: 'all', label: '全部级别' },
                { value: 'critical', label: 'Critical' },
                { value: 'warning', label: 'Warning' },
                { value: 'info', label: 'Info' },
                { value: 'suppressed', label: '已静默' },
              ]}
            />
            <Search
              placeholder="搜索告警名称 / 实例"
              allowClear
              style={{ width: 220 }}
              onSearch={v => setFilters({ ...filters, search: v })}
            />
          </>
        }
        right={
          <Text type="secondary" style={{ fontSize: 12 }}>
            异常严重度置顶 · 时间为相对时间
          </Text>
        }
      />

      <MainSplit
        main={
          <Card
            title={`${TAB_ITEMS.find((t) => t.key === tab)?.label || '告警'} (${rows.length})`}
            size="small"
          >
            {rows.length === 0 ? (
              <EmptyState
                kind="no-result"
                title="暂无告警"
                description={emptyDesc}
                actionLabel={tab === 'active' ? '查看告警规则' : '刷新'}
                onAction={() => {
                  if (tab === 'active') window.location.assign('/monitoring/alert-rules')
                  else refetch()
                }}
              />
            ) : (
              <Table
                dataSource={rows}
                columns={columns}
                rowKey={(r) => r.fingerprint || `${r.name}-${r.instance}-${r.started_at}`}
                pagination={{ pageSize: 20 }}
                size="small"
                loading={isLoading || (tab === 'resolved' && loadingHistory)}
                rowClassName={(r) =>
                  (r.severity || '').toLowerCase() === 'critical' ? 'alert-row-critical' : ''
                }
              />
            )}
          </Card>
        }
        side={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <SideCard title="快捷入口">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/alert-rules">
                  告警规则
                </Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/live">
                  实时看板
                </Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/metrics">
                  指标查询
                </Button>
                <Button type="link" size="small" icon={<HistoryOutlined />} onClick={() => setRcaLibraryOpen(true)}>
                  根因库 · AI 准确率
                </Button>
                <RouterLink to="/monitoring/logs" style={{ fontSize: 12 }}>相关日志查询</RouterLink>
              </Space>
            </SideCard>
            {(stats.critical > 0) && (
              <Card size="small" style={{ borderColor: 'var(--danger)' }}>
                <Space>
                  <Tag color="red">严重 {stats.critical}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>优先处理 Critical 告警</Text>
                </Space>
              </Card>
            )}
          </Space>
        }
      />

      <Drawer
        title="告警详情"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={640}
      >
        {selectedAlert && (
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <Alert
              message={selectedAlert.summary}
              description={selectedAlert.description}
              type={selectedAlert.severity === 'critical' ? 'error' : 'warning'}
              showIcon
            />

            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="告警名称">{selectedAlert.name}</Descriptions.Item>
              <Descriptions.Item label="严重程度">
                <SeverityTag severity={selectedAlert.severity} />
              </Descriptions.Item>
              <Descriptions.Item label="实例">{selectedAlert.instance}</Descriptions.Item>
              <Descriptions.Item label="触发时间">
                {dayjs(selectedAlert.started_at).format('YYYY-MM-DD HH:mm:ss')}（{relativeOrAbsolute(selectedAlert.started_at)}）
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Badge
                  status={selectedAlert.status === 'firing' ? 'error' : 'warning'}
                  text={selectedAlert.status}
                />
              </Descriptions.Item>
              <Descriptions.Item label="认领状态">
                {selectedAlert.acknowledged
                  ? `已由 ${selectedAlert.acknowledged_by || '未知用户'} 于 ${selectedAlert.acknowledged_at || '-'} 认领`
                  : '未认领'}
              </Descriptions.Item>
            </Descriptions>

            <div>
              <div style={{ marginBottom: 8, fontWeight: 'bold' }}>
                <ClockCircleOutlined /> 处理时间线
              </div>
              <Timeline items={timelineItems} />
            </div>

            {selectedAlert.labels && Object.keys(selectedAlert.labels).length > 0 && (
              <div>
                <div style={{ marginBottom: 8, fontWeight: 'bold' }}>标签：</div>
                <Space wrap>
                  {Object.entries(selectedAlert.labels).map(([key, value]) => (
                    <Tag key={key}>{key}: {String(value)}</Tag>
                  ))}
                </Space>
              </div>
            )}

            <Space>
              {!selectedAlert.acknowledged && (
                <Button
                  type="primary"
                  icon={<CheckOutlined />}
                  onClick={() => {
                    if (selectedAlert.fingerprint) {
                      setAckTarget({ fingerprint: selectedAlert.fingerprint, name: selectedAlert.name })
                      setAckNote('')
                    }
                  }}
                >
                  认领处理
                </Button>
              )}
              {selectedAlert.acknowledged && selectedAlert.fingerprint && (
                <Popconfirm
                  title="释放认领？"
                  description="告警将回到未认领状态，其他人可以重新接手。"
                  onConfirm={() => unacknowledgeMut.mutate(selectedAlert.fingerprint!)}
                >
                  <Button icon={<CheckOutlined />}>释放认领</Button>
                </Popconfirm>
              )}
              <Button
                icon={<EyeInvisibleOutlined />}
                onClick={() => {
                  silenceMut.mutate({
                    alert_name: selectedAlert.name,
                    instance: selectedAlert.instance,
                    duration_minutes: 60,
                  })
                  setDrawerOpen(false)
                }}
              >
                屏蔽 1 小时
              </Button>
            </Space>
          </Space>
        )}
      </Drawer>

      <AlertRcaDrawer
        open={!!rcaAlert}
        alert={rcaAlert}
        onClose={() => setRcaAlert(null)}
      />

      <RootCauseLibrary open={rcaLibraryOpen} onClose={() => setRcaLibraryOpen(false)} />

      {/* 认领弹窗：认领即接手处理，备注写清做了什么/如何跟进，形成处理闭环 */}
      <Modal
        title={ackTarget ? `认领告警：${ackTarget.name}` : '认领告警'}
        open={!!ackTarget}
        onCancel={() => { setAckTarget(null); setAckNote('') }}
        onOk={() => {
          if (ackTarget) acknowledgeMut.mutate({ fingerprint: ackTarget.fingerprint, note: ackNote })
        }}
        okText="认领"
        confirmLoading={acknowledgeMut.isPending}
        destroyOnHidden
      >
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          认领后告警会标记处理人；恢复或换人接手前可在详情里释放认领。
        </Paragraph>
        <Input.TextArea
          rows={3}
          maxLength={500}
          showCount
          value={ackNote}
          onChange={(e) => setAckNote(e.target.value)}
          placeholder="处理备注（可选）：如「已定位为日志盘满，正在清理 / 已通知业务方扩容」"
        />
      </Modal>
    </div>
  )
}
