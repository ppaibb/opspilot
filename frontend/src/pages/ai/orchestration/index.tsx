/**
 * AI 编排中心（/ai/orchestration）。
 *
 * 第一版聚焦「执行前置策略」治理（而非画布编排）：
 * - 策略配置：审批开关、预算护栏、超时、步数上限、工单要求
 * - 高风险动作清单（只读展示，来自后端策略）
 * - 预检模拟器：输入拟执行操作的元信息，返回 是否放行 / 是否需审批 / 原因
 *   ——上线任何自动化动作前，先用预检确认策略行为，避免误伤
 *
 * 后续版本在此页扩展可视化画布（React Flow）与真实执行引擎。
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Row, Col, Form, InputNumber, Switch, Button, Space, Typography, Flex, App, Tag, Alert,
  Descriptions, Divider, Input, Empty, Spin,
} from 'antd'
import {
  SaveOutlined, ReloadOutlined, SafetyCertificateOutlined, AuditOutlined, WarningOutlined,
} from '@ant-design/icons'
import { aiPlatformApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { OrchestrationPolicyPayload, OrchestrationPreviewPayload, OrchestrationPreviewResult } from '@/types/api'

const { Text } = Typography

/** 策略表单值直接复用后端策略载荷类型。 */
type PolicyFormValues = OrchestrationPolicyPayload

export default function AIOrchestrationPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.ai.orchestration.manage')

  const [policyForm] = Form.useForm<PolicyFormValues>()
  const [previewForm] = Form.useForm()
  const [previewResult, setPreviewResult] = useState<OrchestrationPreviewResult | null>(null)

  const { data: policy, isLoading } = useQuery({
    queryKey: ['ai-orchestration-policy'],
    queryFn: aiPlatformApi.getOrchestrationPolicy,
  })

  // 策略加载完成后同步进表单（编辑不触发重复回填）。
  useEffect(() => {
    if (policy) {
      policyForm.setFieldsValue({
        approval_enabled: policy.approval_enabled,
        budget_guard_enabled: policy.budget_guard_enabled,
        max_budget_cost_index: policy.max_budget_cost_index,
        timeout_seconds: policy.timeout_seconds,
        max_steps: policy.max_steps,
        required_approvers: policy.required_approvers,
        ticket_required: policy.ticket_required,
      })
    }
  }, [policy, policyForm])

  const saveMutation = useMutation({
    mutationFn: (values: PolicyFormValues) => aiPlatformApi.updateOrchestrationPolicy(values),
    onSuccess: () => {
      message.success('编排策略已保存')
      queryClient.invalidateQueries({ queryKey: ['ai-orchestration-policy'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const previewMutation = useMutation({
    mutationFn: (values: OrchestrationPreviewPayload) => aiPlatformApi.previewOrchestration(values),
    onSuccess: (result) => setPreviewResult(result),
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '预检失败'),
  })

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: '120px 0' }}>
        <Spin size="large" tip="加载编排策略..." />
      </div>
    )
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>编排中心</Typography.Title>
          <Text type="secondary">
            自动化操作的执行前置策略（审批 / 预算 / 超时 / 步数）；画布编排将在后续版本提供
            {policy?.updated_at ? ` · 策略更新于 ${policy.updated_at}` : ''}
          </Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ['ai-orchestration-policy'] })}>
          刷新
        </Button>
      </Flex>

      <Row gutter={16}>
        <Col xs={24} lg={13}>
          <Card
            title={<Space><SafetyCertificateOutlined />执行前置策略</Space>}
            size="small"
            extra={
              <Button
                type="primary" size="small" icon={<SaveOutlined />}
                disabled={!canManage} loading={saveMutation.isPending}
                onClick={() => policyForm.submit()}
              >
                保存策略
              </Button>
            }
          >
            {!canManage && (
              <Alert type="info" showIcon style={{ marginBottom: 12 }} message="当前角色只读，修改策略需「编排策略写操作」权限" />
            )}
            <Form
              form={policyForm}
              layout="vertical"
              onFinish={(values) => saveMutation.mutate(values)}
            >
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="approval_enabled" label="人工审批门禁"
                    valuePropName="checked"
                    extra="开启后命中高风险动作需审批放行"
                  >
                    <Switch checkedChildren="开" unCheckedChildren="关" disabled={!canManage} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="budget_guard_enabled" label="预算护栏"
                    valuePropName="checked"
                    extra="预估成本超过上限时阻断"
                  >
                    <Switch checkedChildren="开" unCheckedChildren="关" disabled={!canManage} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="max_budget_cost_index" label="单次预算上限（成本指数）">
                    <InputNumber min={100} max={100000} step={100} style={{ width: '100%' }} disabled={!canManage} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="timeout_seconds" label="执行超时（秒）">
                    <InputNumber min={15} max={3600} step={15} style={{ width: '100%' }} disabled={!canManage} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="max_steps" label="最大步数">
                    <InputNumber min={1} max={64} style={{ width: '100%' }} disabled={!canManage} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="required_approvers" label="审批人数">
                    <InputNumber min={1} max={5} style={{ width: '100%' }} disabled={!canManage} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item
                name="ticket_required" label="强制工单关联"
                valuePropName="checked"
                extra="开启后无工单号的执行请求将被拒绝"
              >
                <Switch checkedChildren="要求" unCheckedChildren="不要求" disabled={!canManage} />
              </Form.Item>
            </Form>

            <Divider style={{ margin: '8px 0 12px' }} />
            <Descriptions title="高风险动作清单（只读）" size="small" column={1}>
              <Descriptions.Item>
                {policy?.high_risk_actions?.length ? (
                  <Space size={[6, 6]} wrap>
                    {policy.high_risk_actions.map((action) => (
                      <Tag key={action} color="red" icon={<WarningOutlined />}>{action}</Tag>
                    ))}
                  </Space>
                ) : (
                  <Text type="secondary">清单为空，由服务端策略默认值维护</Text>
                )}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>

        <Col xs={24} lg={11}>
          <Card title={<Space><AuditOutlined />预检模拟器</Space>} size="small">
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              模拟一次拟执行操作，检验当前策略是否放行、是否触发审批。
            </Text>
            <Form
              form={previewForm}
              layout="vertical"
              onFinish={(values) => {
                setPreviewResult(null)
                previewMutation.mutate(values)
              }}
              initialValues={{ estimated_cost_index: 500, expected_steps: 3, expected_duration_seconds: 60, has_ticket: true }}
            >
              <Form.Item name="operation_name" label="操作名称" rules={[{ required: true, message: '请输入操作名称' }]}>
                <Input placeholder="如：批量重启生产集群节点" />
              </Form.Item>
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item name="estimated_cost_index" label="预估成本指数">
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="expected_steps" label="预计步数">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="expected_duration_seconds" label="预计时长（秒）">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name="has_ticket" label="持有工单" valuePropName="checked">
                <Switch checkedChildren="有" unCheckedChildren="无" />
              </Form.Item>
              <Form.Item name="action_keys" label="涉及动作（逗号分隔，可选）">
                <Input placeholder="vm.poweroff, cluster.restart" />
              </Form.Item>
              <Button
                type="primary" block loading={previewMutation.isPending} htmlType="submit"
                icon={<SafetyCertificateOutlined />}
              >
                执行预检
              </Button>
            </Form>

            {previewResult && (
              <Alert
                style={{ marginTop: 16 }}
                type={previewResult.allowed ? (previewResult.requires_approval ? 'warning' : 'success') : 'error'}
                showIcon
                icon={previewResult.allowed ? <SafetyCertificateOutlined /> : <WarningOutlined />}
                message={
                  previewResult.allowed
                    ? (previewResult.requires_approval ? '放行，但需要人工审批' : '直接放行')
                    : '策略阻断'
                }
                description={
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    {previewResult.reasons.length === 0
                      ? <Text type="secondary">无附加说明</Text>
                      : previewResult.reasons.map((reason) => <Text key={reason} style={{ fontSize: 12 }}>· {reason}</Text>)}
                    {previewResult.matched_high_risk_actions.length > 0 && (
                      <Space size={4} wrap style={{ marginTop: 4 }}>
                        <Text type="secondary" style={{ fontSize: 12 }}>命中高风险动作：</Text>
                        {previewResult.matched_high_risk_actions.map((a) => <Tag key={a} color="red">{a}</Tag>)}
                      </Space>
                    )}
                  </Space>
                }
              />
            )}

            {!previewResult && !previewMutation.isPending && (
              <Empty
                description="填写操作元信息后执行预检"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                style={{ marginTop: 24 }}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
