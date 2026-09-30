// ───────────────────────── 审核规则配置 Tab ─────────────────────────
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Select, Switch, App, Alert,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { SafetyCertificateOutlined } from '@ant-design/icons'
import { dbApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { DbAuditRuleRow } from './types'

export function AuditRulesTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.db.manage')
  const [draft, setDraft] = useState<DbAuditRuleRow[]>([])

  const { data, isLoading } = useQuery({
    queryKey: ['db-audit-rules'],
    queryFn: dbApi.getAuditRules,
  })

  // 拉取后初始化草稿；保存成功后 data 变化会刷新草稿（保存按钮随即不可重复点击旧值）。
  useEffect(() => {
    if (data?.rules?.length) {
      // 回填延后到微任务，避免在 effect 内同步 setState 触发级联渲染
      queueMicrotask(() => setDraft(data.rules))
    }
  }, [data])

  const saveMutation = useMutation({
    mutationFn: () => dbApi.updateAuditRules(draft.map(({ rule, enabled, level }) => ({ rule, enabled, level }))),
    onSuccess: () => {
      message.success('审核规则已保存（即时对新工单生效）')
      queryClient.invalidateQueries({ queryKey: ['db-audit-rules'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const columns: ColumnsType<DbAuditRuleRow> = [
    { title: '规则', dataIndex: 'rule', width: 70 },
    { title: '说明', dataIndex: 'name', ellipsis: true },
    {
      title: '默认', dataIndex: 'default_level', width: 80,
      render: (v: string) => (v === 'block' ? <Tag color="red">拦截</Tag> : <Tag color="orange">警告</Tag>),
    },
    {
      title: '启用', dataIndex: 'enabled', width: 80,
      render: (v: boolean, _r, idx) => (
        <Switch size="small" checked={v} disabled={!canManage}
          onChange={(checked) => setDraft((prev) => prev.map((r, i) => (i === idx ? { ...r, enabled: checked } : r)))} />
      ),
    },
    {
      title: '级别', dataIndex: 'level', width: 120,
      render: (v: string, _r, idx) => (
        <Select size="small" value={v} disabled={!canManage} style={{ width: 100 }}
          options={[{ value: 'block', label: '拦截' }, { value: 'warn', label: '警告' }]}
          onChange={(level) => setDraft((prev) => prev.map((r, i) => (i === idx ? { ...r, level } : r)))} />
      ),
    },
  ]

  return (
    <Card
      size="small"
      title={<Space><SafetyCertificateOutlined />SQL 审核规则（提交即生效，block 级拒收 / warn 级提示放行）</Space>}
      extra={
        <Button type="primary" size="small" disabled={!canManage} loading={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}>
          保存配置
        </Button>
      }
    >
      <Table rowKey="rule" size="small" columns={columns} dataSource={draft} loading={isLoading} pagination={false} />
      <Alert type="info" showIcon style={{ marginTop: 12 }}
        message="把拦截降级为警告或关闭规则会降低安全底线（如允许 DROP TABLE），请谨慎调整；R0（空 SQL）不可配置。" />
    </Card>
  )
}
