// ───────────────────────── 审批流配置 Tab ─────────────────────────
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Button, Space, Tag, Typography, Row, Col, Select, App, Alert,
} from 'antd'
import { PlusOutlined, DeleteOutlined, NodeIndexOutlined } from '@ant-design/icons'
import { dbApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'

const { Text } = Typography

/** 单个工单类型的审批链编辑器：有序角色列表，可增删级。 */
function FlowStepsEditor({
  steps, roles, onChange, disabled,
}: {
  steps: string[]
  roles: string[]
  onChange: (steps: string[]) => void
  disabled: boolean
}) {
  return (
    <div>
      {steps.length === 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          未配置 → 单级模式：任何具备「SQL 工单审批」权限的用户均可审批
        </Text>
      )}
      {steps.map((role, idx) => (
        <Row key={idx} gutter={8} style={{ marginBottom: 8 }} align="middle">
          <Col flex="none" style={{ width: 92 }}>
            <Tag style={{ marginInlineEnd: 0 }}>第 {idx + 1} 级</Tag>
          </Col>
          <Col flex="auto" style={{ maxWidth: 240 }}>
            <Select
              value={role || undefined}
              placeholder="选择角色"
              style={{ width: '100%' }}
              disabled={disabled}
              showSearch
              options={roles.map((r) => ({ value: r, label: r }))}
              onChange={(v) => onChange(steps.map((s, i) => (i === idx ? v : s)))}
            />
          </Col>
          <Col flex="none">
            <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={disabled}
              onClick={() => onChange(steps.filter((_, i) => i !== idx))} />
          </Col>
        </Row>
      ))}
      <Button size="small" icon={<PlusOutlined />} disabled={disabled}
        onClick={() => onChange([...steps, roles[0] || ''])}>
        添加审批级
      </Button>
    </div>
  )
}

export function ApprovalFlowsTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.db.manage')
  const [changeSteps, setChangeSteps] = useState<string[]>([])
  const [querySteps, setQuerySteps] = useState<string[]>([])
  const [roles, setRoles] = useState<string[]>([])

  // 拉取后初始化草稿（与审核规则 Tab 相同的"草稿-保存"模式）。
  useEffect(() => {
    dbApi.getApprovalFlows().then((data) => {
      setChangeSteps(data.change?.steps || [])
      setQuerySteps(data.query?.steps || [])
      setRoles(data.roles || [])
    })
  }, [])

  const saveMutation = useMutation({
    mutationFn: () => dbApi.updateApprovalFlows({ change: { steps: changeSteps }, query: { steps: querySteps } }),
    onSuccess: () => {
      message.success('审批流已保存（仅对新提交的工单生效，在途工单沿用提交时的链）')
      queryClient.invalidateQueries({ queryKey: ['db-approval-flows'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  return (
    <Card
      size="small"
      title={<Space><NodeIndexOutlined />审批流配置（按工单类型指定逐级审批角色）</Space>}
      extra={
        <Button type="primary" size="small" disabled={!canManage} loading={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}>
          保存配置
        </Button>
      }
    >
      <Row gutter={24}>
        <Col xs={24} md={12}>
          <Card size="small" type="inner" title="变更工单（DDL/DML）" style={{ height: '100%' }}>
            <FlowStepsEditor steps={changeSteps} roles={roles} disabled={!canManage} onChange={setChangeSteps} />
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card size="small" type="inner" title="查询授权工单" style={{ height: '100%' }}>
            <FlowStepsEditor steps={querySteps} roles={roles} disabled={!canManage} onChange={setQuerySteps} />
          </Card>
        </Col>
      </Row>
      <Alert type="info" showIcon style={{ marginTop: 12 }}
        message="审批链从上到下逐级流转：每一级只有该角色且具备审批权限的用户能审批，批准后自动流向下一级；任一级驳回即终止。提交人不能审批自己的工单（管理员角色豁免）。配置变更不影响已提交的在途工单。" />
    </Card>
  )
}
