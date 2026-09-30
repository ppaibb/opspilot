/**
 * 采集车队（/monitoring/collectors）。
 *
 * 一页一表：主机 × 指标灯 × 日志灯 × 状态（未接入/在线/异常）。
 * 安装入口只有 Alloy；本机 localhost 走内置监控栈，不提供 SSH 安装。
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Tag, Space, Button, App, Typography, Modal, Select, Tooltip, Collapse,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, DownloadOutlined, DeleteOutlined, LinkOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'
import { assetsApi, monitoringApi } from '@/api'
import type { LogTargetItem } from '@/types/api'
import { extractAxiosDetail, usePermission } from '@/hooks/usePermission'
import EmptyState from '@/components/monitoring/EmptyState'
import { PageHeader, FilterBar, MainSplit, SideCard } from '@/components/monitoring/PageShell'
import {
  fleetSummary, sortTargets, metricLight, logLight,
  type FleetTarget, type LightState,
} from './fleet'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

const { Text } = Typography

/** 统一状态：未接入 / 在线 / 异常 / 已停用 */
type RowStatus = 'never' | 'online' | 'error' | 'disabled'

type MergedRow = FleetTarget & {
  metrics_enabled?: boolean
  logs_enabled?: boolean
  last_metric_at?: string
  last_log_at?: string
  source?: string
}

function heartbeat(time?: string) {
  if (!time) return <Text type="secondary">-</Text>
  const d = dayjs(time)
  if (!d.isValid()) return <Text type="secondary">{time}</Text>
  return (
    <Tooltip title={d.format('YYYY-MM-DD HH:mm:ss')}>
      <Text type="secondary" style={{ fontSize: 12 }}>{d.fromNow()}</Text>
    </Tooltip>
  )
}

const LIGHT_COLOR: Record<LightState, string> = {
  green: 'var(--ok, #16a34a)',
  gray: 'var(--ink-3, #9ca3af)',
  red: 'var(--danger, #dc2626)',
}
const LIGHT_LABEL: Record<LightState, string> = {
  green: '在采',
  gray: '未采',
  red: '失败',
}

function DotLight({ state, label }: { state: LightState; label?: string }) {
  const color = LIGHT_COLOR[state]
  return (
    <Space size={6}>
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: color,
          display: 'inline-block',
          boxShadow: `0 0 0 2px color-mix(in srgb, ${color} 25%, transparent)`,
        }}
      />
      <span style={{ fontSize: 12, color: 'var(--ink-2)' }}>{label ?? LIGHT_LABEL[state]}</span>
    </Space>
  )
}

function DualLights({ row }: { row: MergedRow }) {
  return (
    <Space direction="vertical" size={2}>
      <Space size={6}>
        <span style={{ fontSize: 11, color: 'var(--ink-3)', width: 28 }}>指标</span>
        <DotLight state={metricLight(row)} />
      </Space>
      <Space size={6}>
        <span style={{ fontSize: 11, color: 'var(--ink-3)', width: 28 }}>日志</span>
        <DotLight state={logLight(row)} />
      </Space>
    </Space>
  )
}

function rowStatus(row: MergedRow): RowStatus {
  const status = (row.status || '').toLowerCase()
  if (row.last_error || ['unreachable', 'error', 'failed', 'offline', 'down'].includes(status)) {
    return 'error'
  }
  if (!row.enabled) {
    // 无 agent 记录或从未接入
    if (status === 'disabled' || !row.last_checked_at) return 'never'
    return 'disabled'
  }
  if (['up', 'online', 'healthy', 'running', 'ok'].includes(status) || row.last_checked_at) {
    return 'online'
  }
  return 'never'
}

const STATUS_META: Record<RowStatus, { color: string; label: string }> = {
  never: { color: 'default', label: '未接入' },
  online: { color: 'success', label: '在线' },
  error: { color: 'error', label: '异常' },
  disabled: { color: 'warning', label: '已停用' },
}

/** 主机展示名：资产名 > hostname > IP，避免容器 ID。 */
function displayHost(row: MergedRow, hostnames?: Record<string, string>) {
  const hint = hostnames?.[row.asset_id]
  const name = (row.name || '').trim()
  // 像容器 ID / 纯 hash 的名字降级
  const looksLikeHash = /^[0-9a-f]{6,}$/i.test(name) || name.length >= 12 && /^[0-9a-f]+$/i.test(name.replace(/\D/g, ''))
  const primary = (!looksLikeHash && name) || hint || row.ip || row.asset_id
  return primary
}

function isLocalTarget(row: MergedRow) {
  return row.asset_id === 'ins-localhost' || row.provider === 'localhost'
}

export default function CollectorsPage() {
  const queryClient = useQueryClient()
  const { message, modal } = App.useApp()
  const { has } = usePermission()
  const canInstall = has('action.assets.hosts.monitoring') || has('module.assets.hosts')
  const [installOpen, setInstallOpen] = useState(false)
  const [installAssetId, setInstallAssetId] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<'all' | RowStatus>('all')
  const [keyword, setKeyword] = useState('')

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['monitoring-targets'],
    queryFn: monitoringApi.targets,
    refetchInterval: 30000,
  })

  const { data: logData, refetch: refetchLogs } = useQuery({
    queryKey: ['log-targets'],
    queryFn: monitoringApi.logTargets,
    refetchInterval: 30000,
  })

  const { data: assets = [], refetch: refetchAssets } = useQuery({
    queryKey: ['hosts'],
    queryFn: assetsApi.list,
    enabled: true,
  })

  const hostnames = useMemo(() => {
    const map: Record<string, string> = {}
    for (const a of assets as Array<{ id?: string; instance_id?: string; name?: string; hostname?: string }>) {
      const id = a.id || a.instance_id || ''
      if (id && a.hostname) map[id] = a.hostname
    }
    return map
  }, [assets])

  const merged = useMemo((): MergedRow[] => {
    const metrics = (data?.targets || []) as unknown as MergedRow[]
    const logs = (logData?.targets || []) as unknown as (MergedRow & LogTargetItem)[]
    const byId = new Map<string, MergedRow>()
    for (const t of metrics) byId.set(t.asset_id, { ...t })
    for (const t of logs) {
      const prev = byId.get(t.asset_id)
      if (prev) {
        byId.set(t.asset_id, {
          ...prev,
          ...t,
          enabled: Boolean(prev.enabled || t.enabled),
          metrics_enabled: prev.metrics_enabled ?? prev.enabled,
          logs_enabled: t.logs_enabled ?? t.enabled,
          last_metric_at: prev.last_metric_at || prev.last_checked_at,
          last_log_at: t.last_log_at || t.last_checked_at,
          last_error: t.last_error || prev.last_error || '',
          name: prev.name || t.name,
          ip: prev.ip || t.ip,
          status: prev.status || t.status,
          last_checked_at: prev.last_checked_at || t.last_checked_at,
          source: t.source || prev.source,
        })
      } else {
        byId.set(t.asset_id, {
          ...t,
          metrics_enabled: false,
          logs_enabled: t.logs_enabled ?? t.enabled,
          last_log_at: t.last_log_at || t.last_checked_at,
        })
      }
    }
    const rows = [...byId.values()].map((r) => ({
      ...r,
      name: displayHost(r, hostnames),
    }))
    return sortTargets(rows)
  }, [data, logData, hostnames])

  const filtered = useMemo(() => {
    let rows = merged
    if (statusFilter !== 'all') {
      rows = rows.filter((r) => rowStatus(r) === statusFilter)
    }
    const kw = keyword.trim().toLowerCase()
    if (kw) {
      rows = rows.filter((r) =>
        [r.name, r.ip, r.asset_id, r.last_error].some((v) => String(v || '').toLowerCase().includes(kw)))
    }
    return rows
  }, [merged, statusFilter, keyword])

  const summary = useMemo(() => fleetSummary(merged), [merged])
  const neverCount = merged.filter((r) => rowStatus(r) === 'never').length
  const errorCount = merged.filter((r) => rowStatus(r) === 'error').length
  const onlineCount = merged.filter((r) => rowStatus(r) === 'online').length
  const logsCollecting = merged.filter((r) => logLight(r) === 'green').length

  const installMut = useMutation({
    mutationFn: (assetId: string) =>
      monitoringApi.installAlloy(assetId, { port: 9100, job: 'opspilot-node' }),
    onSuccess: (resp: { message?: string }) => {
      message.success(resp?.message || '安装完成')
      setInstallOpen(false)
      setInstallAssetId(null)
      queryClient.invalidateQueries({ queryKey: ['monitoring-targets'] })
      queryClient.invalidateQueries({ queryKey: ['log-targets'] })
      refetch()
      refetchLogs()
      refetchAssets()
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) || '安装失败')
    },
  })

  const uninstallMut = useMutation({
    mutationFn: (assetId: string) => monitoringApi.uninstallAlloy(assetId),
    onSuccess: (resp: { message?: string }) => {
      message.success(resp?.message || '已卸载')
      queryClient.invalidateQueries({ queryKey: ['monitoring-targets'] })
      queryClient.invalidateQueries({ queryKey: ['log-targets'] })
      refetch()
      refetchLogs()
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) || '卸载失败')
    },
  })

  const openInstall = (assetId?: string) => {
    let preferred = assetId ?? null
    if (!preferred && merged.length === 1) preferred = merged[0].asset_id
    if (!preferred) {
      const first = merged.find((r) => rowStatus(r) !== 'online') || merged[0]
      preferred = first?.asset_id ?? null
    }
    if (!preferred) {
      const list = assets as Array<{ id?: string; provider?: string }>
      const pick = list.find((a) => a.provider !== 'localhost') || list[0]
      preferred = pick?.id || null
    }
    setInstallAssetId(preferred)
    setInstallOpen(true)
  }

  const assetOptions = useMemo(() => {
    const list = assets as Array<{ id?: string; instance_id?: string; name?: string; hostname?: string; ip?: string; provider?: string }>
    const opts = list
      .filter((a) => {
        const id = a.id || a.instance_id || ''
        if (!id) return false
        // 展示全部资产；本机标注，不再静默过滤导致下拉为空
        return true
      })
      .map((a) => {
        const id = a.id || a.instance_id!
        const label = a.hostname || a.name || a.ip || id
        const suffix = a.provider === 'localhost' || id === 'ins-localhost' ? '（本机）' : (a.ip ? ` · ${a.ip}` : '')
        return { value: id, label: `${label}${suffix}` }
      })
    // 已有采集目标但不在资产列表时补上
    for (const r of merged) {
      if (!opts.some((o) => o.value === r.asset_id)) {
        opts.push({ value: r.asset_id, label: `${r.name || r.asset_id} · ${r.ip}` })
      }
    }
    return opts
  }, [assets, merged])

  const columns: ColumnsType<MergedRow> = [
    {
      title: '主机',
      key: 'name',
      width: 200,
      render: (_: unknown, row) => (
        <div>
          <Text strong ellipsis style={{ maxWidth: 180, display: 'block' }}>
            {displayHost(row, hostnames)}
          </Text>
          <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>
            {row.ip}
            {isLocalTarget(row) ? ' · 本机' : ''}
          </div>
        </div>
      ),
    },
    {
      title: '指标 / 日志',
      key: 'lights',
      width: 130,
      render: (_: unknown, row) => <DualLights row={row} />,
    },
    {
      title: '状态',
      key: 'status',
      width: 90,
      render: (_: unknown, row) => {
        const st = rowStatus(row)
        const meta = STATUS_META[st]
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '最近心跳',
      dataIndex: 'last_checked_at',
      key: 'last_checked_at',
      width: 110,
      render: (time: string) => heartbeat(time),
    },
    {
      title: '错误',
      dataIndex: 'last_error',
      key: 'last_error',
      ellipsis: true,
      render: (error: string) => error
        ? <Tooltip title={error}><Text type="danger" style={{ fontSize: 12 }}>{error}</Text></Tooltip>
        : <Text type="secondary">-</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_: unknown, row) => {
        if (isLocalTarget(row)) {
          return (
            <Tooltip title="本机由监控栈内置 Alloy 自动采集（host 指标 + Docker 日志），无需安装 Agent">
              <Tag color="blue" style={{ margin: 0 }}>内置采集</Tag>
            </Tooltip>
          )
        }
        return (
          <Space size={4}>
            <Button
              size="small"
              type={rowStatus(row) === 'never' ? 'primary' : 'default'}
              icon={<DownloadOutlined />}
              disabled={!canInstall}
              onClick={() => {
                setInstallAssetId(row.asset_id)
                setInstallOpen(true)
              }}
            >
              {rowStatus(row) === 'never' || rowStatus(row) === 'error' ? '安装' : '重装'}
            </Button>
            <Tooltip title="卸载 Alloy Agent">
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={!canInstall}
                onClick={() => {
                  modal.confirm({
                    title: '卸载 Alloy Agent？',
                    content: '将在目标机执行卸载脚本并清理本地记录。',
                    onOk: () => uninstallMut.mutate(row.asset_id),
                  })
                }}
              />
            </Tooltip>
          </Space>
        )
      },
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <PageHeader
        title="采集车队"
        subtitle={
          <Space size={12} wrap>
            <span>在线 <b style={{ color: onlineCount > 0 ? 'var(--ok)' : 'var(--ink)' }}>{onlineCount}</b> / {summary.total}</span>
            <span>未接入 <b style={{ color: neverCount > 0 ? 'var(--ink-3)' : 'var(--ink)' }}>{neverCount}</b></span>
            <span>异常 <b style={{ color: errorCount > 0 ? 'var(--danger)' : 'var(--ink)' }}>{errorCount}</b></span>
          </Space>
        }
        extra={
          <>
            <Button
              type="primary"
              icon={<DownloadOutlined />}
              disabled={!canInstall}
              onClick={() => openInstall()}
            >
              安装 Alloy
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => { refetch(); refetchLogs(); refetchAssets() }}>
              刷新
            </Button>
          </>
        }
      />

      <FilterBar
        left={
          <>
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              style={{ width: 120 }}
              options={[
                { value: 'all', label: '全部状态' },
                { value: 'never', label: '未接入' },
                { value: 'online', label: '在线' },
                { value: 'error', label: '异常' },
                { value: 'disabled', label: '已停用' },
              ]}
            />
            <Select
              allowClear
              showSearch
              placeholder="筛选主机"
              style={{ width: 180 }}
              value={keyword || undefined}
              onSearch={setKeyword}
              onChange={(v) => setKeyword(v || '')}
              options={merged.map((r) => ({ value: r.name || r.ip, label: r.name || r.ip }))}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>30 秒自动刷新</Text>
          </>
        }
        right={
          <Text type="secondary" style={{ fontSize: 12 }}>
            指标 {summary.online}/{summary.total} · 日志在采 {logsCollecting}/{summary.total}
          </Text>
        }
      />

      <MainSplit
        main={
          <Card title={`目标列表 (${filtered.length})`} size="small">
            {filtered.length === 0 && !isLoading ? (
              <EmptyState
                kind="no-result"
                title="暂无采集目标"
                description="点右上角「安装 Alloy」为业务主机安装指标与 Docker 日志采集。"
                actionLabel={canInstall ? '安装 Alloy' : undefined}
                onAction={canInstall ? () => openInstall() : undefined}
              />
            ) : (
              <Table
                dataSource={filtered}
                columns={columns}
                rowKey="asset_id"
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                size="small"
                loading={isLoading}
              />
            )}
          </Card>
        }
        side={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <SideCard title="健康总览">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Text style={{ fontSize: 12, color: 'var(--ink-2)' }}>在线 {onlineCount} / {summary.total}</Text>
                <Text style={{ fontSize: 12, color: 'var(--ink-2)' }}>未接入 {neverCount} · 异常 {errorCount}</Text>
                <Text style={{ fontSize: 12, color: 'var(--ink-2)' }}>日志在采 {logsCollecting} / {summary.total}</Text>
              </Space>
            </SideCard>
            <Collapse
              size="small"
              items={[{
                key: 'pref',
                label: <span style={{ fontSize: 12 }}>前置条件</span>,
                children: (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    一次安装同时提供指标与 Docker 日志。目标机需可读{' '}
                    <Text code>/var/run/docker.sock</Text>；Alloy 在容器内时须挂载该 socket。
                    本机 localhost 由监控栈内置 Alloy 自动采集，无需安装 Agent。
                  </Text>
                ),
              }]}
            />
            <SideCard title="快捷入口">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/assets/hosts">主机资产</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/live">实时看板</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/logs">日志查询</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/settings/system">监控连接</Button>
              </Space>
            </SideCard>
          </Space>
        }
      />

      <Modal
        title="安装 Alloy Agent"
        open={installOpen}
        onCancel={() => { setInstallOpen(false) }}
        onOk={() => {
          if (!installAssetId) {
            message.warning('请选择主机')
            return
          }
          installMut.mutate(installAssetId)
        }}
        confirmLoading={installMut.isPending}
        okText="开始安装"
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            将通过 SSH 安装 Alloy，并开启指标与 Docker 日志采集（一次装齐）。
          </Text>
          <Select
            style={{ width: '100%' }}
            placeholder="选择目标主机"
            showSearch
            optionFilterProp="label"
            value={installAssetId}
            onChange={setInstallAssetId}
            options={assetOptions}
          />
        </Space>
      </Modal>
    </div>
  )
}
