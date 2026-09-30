import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Table, Input, Tag, Flex, Select, Space, Badge, Typography, Tooltip, Button,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DownloadOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons'
import { auditApi } from '@/api'
import type { AuditLogResponse } from '@/types/api'

const { Text } = Typography

const RESULT_MAP: Record<string, { color: 'success' | 'error' | 'default'; label: string }> = {
  success: { color: 'success', label: '成功' },
  fail:    { color: 'error',   label: '失败' },
  deny:    { color: 'error',   label: '拒绝' },
}

const RISK_COLOR: Record<string, string> = {
  high:   'red',
  medium: 'orange',
  low:    'default',
}

export default function AuditPage() {
  const [search, setSearch]           = useState('')
  const [userFilter, setUserFilter]   = useState<string | null>(null)
  const [resultFilter, setResultFilter] = useState<string | null>(null)
  const [riskFilter, setRiskFilter]   = useState<string | null>(null)

  const { data: logs = [], isLoading, refetch } = useQuery({
    queryKey: ['audit-logs'],
    queryFn: auditApi.list,
  })

  // 动态生成用户选项
  const userOptions = useMemo(() => {
    const users = [...new Set(logs.map((l) => l.user))].sort()
    return users.map((u) => ({ value: u, label: u }))
  }, [logs])

  const filtered = logs.filter((l) => {
    const matchSearch = !search || l.action.includes(search) || l.resource.includes(search)
    const matchUser   = !userFilter || l.user === userFilter
    const matchResult = !resultFilter || l.result === resultFilter
    const matchRisk   = !riskFilter || l.risk_level === riskFilter
    return matchSearch && matchUser && matchResult && matchRisk
  })

  const columns: ColumnsType<AuditLogResponse> = [
    {
      title: '时间', dataIndex: 'time', width: 170, fixed: 'left',
      sorter: (a, b) => a.time.localeCompare(b.time),
      defaultSortOrder: 'descend',
    },
    {
      title: '操作人', dataIndex: 'user', width: 120,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '操作', dataIndex: 'action', width: 200,
      render: (v: string) => {
        // API 路径 → 业务语义（HTTP 方法 + 首段资源）；未命中回落原文，审计原文始终在「操作对象」可见
        const m = /^(创建|删除|更新)\s+(\S+)$/.exec(v || '')
        if (!m) return <Text>{v}</Text>
        const [, verb, path] = m
        const parts = path.split('/').filter(Boolean)
        const seg = parts[0] === 'api' && parts[1] ? `api/${parts[1]}` : parts[0] || path
        const known: Record<string, string> = {
          'api/ai': 'AI 对话',
          'api/assets': '主机',
          'api/certificates': '证书',
          'api/clusters': 'K8s 集群',
          'api/credentials': '凭证',
          'api/db': '数据库',
          'api/domains': '域名',
          'api/finance': '费用',
          'api/groups': '主机组',
          'api/wireguard': '虚拟组网',
          'api/monitoring': '监控',
          'api/notifications': '通知',
          'api/auth/risk-challenge': '高危票据',
        }
        const key = Object.keys(known).find((k) => seg.startsWith(k))
        const noun = key ? known[key] : seg
        const verbMap: Record<string, string> = { 创建: verb === '删除' ? '删除' : '操作', 删除: '删除', 更新: '修改' }
        return <Text>{`${verbMap[verb] ?? verb} ${noun}${seg.includes('risk-challenge') ? '（取票）' : ''}`}</Text>
      },
    },
    {
      title: '操作对象', dataIndex: 'resource', ellipsis: true,
      render: (v: string) => <Tooltip title={v}><Text type="secondary" style={{ fontSize: 12 }}>{v}</Text></Tooltip>,
    },
    { title: 'IP 地址', dataIndex: 'ip', width: 140 },
    {
      title: '结果', dataIndex: 'result', width: 90,
      render: (v: string) => {
        const m = RESULT_MAP[v] ?? { color: 'default', label: v }
        return <Badge status={m.color} text={m.label} />
      },
    },
    {
      title: '风险', dataIndex: 'risk_level', width: 90,
      render: (v: string, r) =>
        v ? <Tooltip title={r.risk_reason}><Tag color={RISK_COLOR[v] ?? 'default'}>{v}</Tag></Tooltip> : '-',
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" wrap gap={8}>
        <Space wrap>
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索操作或资源"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear style={{ width: 220 }}
          />
          <Select
            placeholder="操作人"
            allowClear style={{ width: 130 }}
            options={userOptions}
            value={userFilter}
            onChange={(v) => setUserFilter(v ?? null)}
          />
          <Select
            placeholder="结果"
            allowClear style={{ width: 100 }}
            options={[
              { value: 'success', label: '成功' },
              { value: 'fail',    label: '失败' },
              { value: 'deny',    label: '拒绝' },
            ]}
            value={resultFilter}
            onChange={(v) => setResultFilter(v ?? null)}
          />
          <Select
            placeholder="风险等级"
            allowClear style={{ width: 110 }}
            options={[
              { value: 'high',   label: '高危' },
              { value: 'medium', label: '中危' },
              { value: 'low',    label: '低危' },
            ]}
            value={riskFilter}
            onChange={(v) => setRiskFilter(v ?? null)}
          />
        </Space>
        <Space>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => {
              // 当前筛选结果导出 CSV（UTF-8 BOM，Excel 直开不乱码）
              const headers = ['时间', '操作人', '操作', '操作对象', 'IP 地址', '结果', '风险等级', '风险原因']
              const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
              const csv = [
                headers.join(','),
                ...filtered.map((l) =>
                  [l.time, l.user, l.action, l.resource, l.ip, l.result, l.risk_level, l.risk_reason].map(esc).join(','),
                ),
              ].join('\r\n')
              const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
              const a = document.createElement('a')
              a.href = URL.createObjectURL(blob)
              a.download = `审计日志-${new Date().toISOString().slice(0, 10)}.csv`
              a.click()
              URL.revokeObjectURL(a.href)
            }}
          >
            导出 CSV
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Flex>

      <Table<AuditLogResponse>
        rowKey="id"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        size="middle"
        scroll={{ x: 1000 }}
        pagination={{
          defaultPageSize: 50,
          showSizeChanger: true,
          pageSizeOptions: ['50', '100', '200'],
          showTotal: (t) => `共 ${t} 条记录`,
        }}
        rowClassName={(r) => r.risk_level === 'high' ? 'ant-table-row-danger' : ''}
      />
    </Flex>
  )
}
