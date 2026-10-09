import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Table, Button, Space, Tag, Typography, Empty, Tooltip, Select, Input, Flex } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, FileProtectOutlined, DownloadOutlined } from '@ant-design/icons'
import { clusterOpsApi } from '@/api'
import type { ClusterAuditLog } from './types'

const { Text } = Typography

// ───────────────────────── 操作审计 Tab ─────────────────────────

const RISK_META: Record<string, { color: string; text: string }> = {
  high: { color: 'red', text: '高危' },
  medium: { color: 'orange', text: '中危' },
  low: { color: 'default', text: '低' },
}

const RESULT_META: Record<string, { color: string; label: string }> = {
  success: { color: 'green', label: '成功' },
  成功: { color: 'green', label: '成功' },
  failed: { color: 'red', label: '失败' },
  fail: { color: 'red', label: '失败' },
  deny: { color: 'red', label: '拒绝' },
}

const ACTION_OPTIONS = [
  { value: '创建', label: '创建' },
  { value: '更新', label: '更新' },
  { value: '删除', label: '删除' },
  { value: '读取', label: '读取' },
]

const RISK_OPTIONS = [
  { value: 'high', label: '高危' },
  { value: 'medium', label: '中危' },
  { value: 'low', label: '低' },
]

const RESULT_OPTIONS = [
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'deny', label: '拒绝' },
]

interface AuditFilters {
  user: string
  action: string
  result: string
  risk: string
  q: string
}

const EMPTY_FILTERS: AuditFilters = { user: '', action: '', result: '', risk: '', q: '' }

/** CSV 单元格转义：引号翻倍 + 含逗号/引号/换行的包引号。 */
function csvCell(value: unknown): string {
  const raw = value == null ? '' : String(value)
  return /[",\n]/.test(raw) ? `"${raw.replace(/"/g, '""')}"` : raw
}

export default function AuditTab({ clusterId }: { clusterId: string }) {
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [exporting, setExporting] = useState(false)

  const { data, isLoading, refetch, dataUpdatedAt } = useQuery({
    queryKey: ['cluster-audit', clusterId, filters, page, pageSize],
    queryFn: () => clusterOpsApi.auditLogs(clusterId, {
      limit: pageSize,
      offset: (page - 1) * pageSize,
      user: filters.user || undefined,
      action: filters.action || undefined,
      result: filters.result || undefined,
      risk_level: filters.risk || undefined,
      q: filters.q || undefined,
    }),
    refetchInterval: 30000,
  })
  const logs = data?.items ?? []
  const total = data?.total ?? 0

  // 操作人 / 动作下拉的选项来源：拉最近 200 条不限筛选的记录（一个集群的操作人就那么几个）。
  const { data: optionSeed } = useQuery({
    queryKey: ['cluster-audit-options', clusterId],
    queryFn: () => clusterOpsApi.auditLogs(clusterId, { limit: 200 }),
    staleTime: 5 * 60 * 1000,
  })
  const userOptions = useMemo(() => {
    const users = [...new Set((optionSeed?.items ?? []).map((l) => l.user).filter(Boolean))].sort()
    return users.map((u) => ({ value: u, label: u }))
  }, [optionSeed])
  const actionOptions = useMemo(() => {
    const seen = new Set((optionSeed?.items ?? []).map((l) => l.action).filter(Boolean))
    const extras = [...seen].filter((a) => !ACTION_OPTIONS.some((o) => o.value === a))
    return [...ACTION_OPTIONS, ...extras.sort().map((a) => ({ value: a, label: a }))]
  }, [optionSeed])

  const updateFilter = (patch: Partial<AuditFilters>) => {
    setFilters((prev) => ({ ...prev, ...patch }))
    setPage(1)
  }

  const exportCsv = async () => {
    setExporting(true)
    try {
      // 导出当前筛选下的全量（不是当前页），limit 用后端上限 1000
      const all = await clusterOpsApi.auditLogs(clusterId, {
        limit: 1000,
        offset: 0,
        user: filters.user || undefined,
        action: filters.action || undefined,
        result: filters.result || undefined,
        risk_level: filters.risk || undefined,
        q: filters.q || undefined,
      })
      const header = ['时间', '操作人', '动作', '对象', '完整路径', 'IP', '结果', '风险等级', '风险原因']
      const lines = [header.join(',')]
      for (const row of all.items) {
        const risk = RISK_META[row.risk_level]?.text ?? row.risk_level
        lines.push([
          row.time, row.user, row.action, row.resource, row.path, row.ip,
          RESULT_META[row.result]?.label ?? row.result, risk, row.risk_reason,
        ].map(csvCell).join(','))
      }
      // BOM：Excel 直接打开中文 CSV 不乱码
      const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `集群操作审计-${clusterId}-${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } finally {
      setExporting(false)
    }
  }

  const columns: ColumnsType<ClusterAuditLog> = [
    // 后端已把 naive UTC 转成本地时间串，直接展示
    { title: '时间', dataIndex: 'time', width: 170 },
    { title: '操作人', dataIndex: 'user', width: 110, render: (v: string) => <Text strong>{v || '-'}</Text> },
    // action 只剩方法标签（创建/更新/删除/读取），完整路径在 path 字段悬浮展示
    { title: '动作', dataIndex: 'action', width: 80 },
    {
      title: '对象', dataIndex: 'resource', ellipsis: true,
      render: (v: string, r) => (
        <Tooltip title={r.path || v}>
          <Text code style={{ fontSize: 12 }}>{v || '-'}</Text>
        </Tooltip>
      ),
    },
    { title: 'IP', dataIndex: 'ip', width: 130, render: (v: string) => v || <Text type="secondary">-</Text> },
    {
      title: '结果', dataIndex: 'result', width: 90,
      render: (v: string) => {
        const meta = RESULT_META[v] ?? { color: 'default', label: v || '未知' }
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '风险', dataIndex: 'risk_level', width: 90,
      render: (v: string, r) => {
        const meta = RISK_META[v] ?? { color: 'default', text: v || '低' }
        return (
          <Tooltip title={r.risk_reason || undefined}>
            <Tag color={meta.color}>{meta.text}</Tag>
          </Tooltip>
        )
      },
    },
  ]

  const updatedAt = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString('zh-CN', { hour12: false })
    : ''

  return (
    <Card
      size="small"
      title={<Space><FileProtectOutlined />集群操作审计（本集群的全部管理操作）</Space>}
      extra={
        <Space size={4}>
          <Text type="secondary" style={{ fontSize: 12 }}>{updatedAt && `更新于 ${updatedAt} · 30s 自动刷新`}</Text>
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={exportCsv}>导出 CSV</Button>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
        </Space>
      }
    >
      <Flex wrap gap="small" style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          placeholder="关键字（操作人 / 路径）"
          style={{ width: 230 }}
          defaultValue={filters.q}
          onSearch={(v) => updateFilter({ q: v })}
        />
        <Select
          allowClear
          placeholder="操作人"
          style={{ width: 150 }}
          value={filters.user || undefined}
          onChange={(v) => updateFilter({ user: v ?? '' })}
          options={userOptions}
          showSearch
          optionFilterProp="label"
        />
        <Select
          allowClear
          placeholder="动作"
          style={{ width: 120 }}
          value={filters.action || undefined}
          onChange={(v) => updateFilter({ action: v ?? '' })}
          options={actionOptions}
        />
        <Select
          allowClear
          placeholder="结果"
          style={{ width: 110 }}
          value={filters.result || undefined}
          onChange={(v) => updateFilter({ result: v ?? '' })}
          options={RESULT_OPTIONS}
        />
        <Select
          allowClear
          placeholder="风险等级"
          style={{ width: 120 }}
          value={filters.risk || undefined}
          onChange={(v) => updateFilter({ risk: v ?? '' })}
          options={RISK_OPTIONS}
        />
      </Flex>
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={logs}
        loading={isLoading}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setPage(p); setPageSize(ps) },
        }}
        locale={{ emptyText: <Empty description={total === 0 && Object.values(filters).some(Boolean) ? '没有匹配的审计记录' : '暂无审计记录'} /> }}
      />
    </Card>
  )
}
