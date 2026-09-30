import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Select, Input, Drawer, Form, Modal, App, Descriptions,
  Steps, Popconfirm, Flex, Empty,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, ReloadOutlined, PlayCircleOutlined, CheckOutlined, CloseOutlined } from '@ant-design/icons'
import { changesApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'

const STATUS_COLOR: Record<string, string> = {
  pending: 'gold',
  approved: 'blue',
  executing: 'processing',
  success: 'green',
  failed: 'red',
  rejected: 'default',
  canceled: 'default',
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待审批',
  approved: '待执行',
  executing: '执行中',
  success: '成功',
  failed: '失败',
  rejected: '已驳回',
  canceled: '已取消',
}

interface ChangeTicketItem {
  id: number
  ticket_no: string
  title: string
  change_type: string
  change_type_label: string
  risk_level: string
  resource_name?: string
  resource_type?: string
  resource_id?: string
  impact?: string
  rollback_plan?: string
  status: string
  submitter: string
  reviewer?: string
  executor?: string
  review_note?: string
  approval_steps?: string[]
  current_step?: number
  approval_log?: Array<{ step: number; role?: string; reviewer: string; decision: string; note?: string; at: string }>
  exec_result?: { ok?: boolean; message?: string }
  created_at?: string
  approved_at?: string
  finished_at?: string
  auto_executor?: boolean
  window_start?: string
  window_end?: string
}

export default function ChangeTicketsPage() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canSubmit = has('action.change.submit')
  const canApprove = has('action.change.approve')
  const canExecute = has('action.change.execute')

  const [status, setStatus] = useState('')
  const [changeType, setChangeType] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)
  const [detail, setDetail] = useState<ChangeTicketItem | null>(null)
  const [form] = Form.useForm()

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['change-tickets', status, changeType, q, page],
    queryFn: () => changesApi.list({ status, change_type: changeType, q, page, page_size: 20 }),
  })

  const { data: types = [] } = useQuery({
    queryKey: ['change-types'],
    queryFn: () => changesApi.types(),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['change-tickets'] })
    if (detail) {
      void changesApi.get(detail.id).then((fresh) => setDetail(fresh)).catch(() => setDetail(null))
    }
  }

  const createMut = useMutation({
    mutationFn: (values: Record<string, unknown>) => changesApi.create(values as never),
    onSuccess: () => {
      message.success('变更工单已提交')
      setCreateOpen(false)
      form.resetFields()
      invalidate()
    },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '提交失败'),
  })

  const approveMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => changesApi.approve(id, note),
    onSuccess: () => { message.success('已批准'); invalidate() },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '审批失败'),
  })

  const rejectMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => changesApi.reject(id, note),
    onSuccess: () => { message.success('已驳回'); invalidate() },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '驳回失败'),
  })

  const cancelMut = useMutation({
    mutationFn: (id: number) => changesApi.cancel(id),
    onSuccess: () => { message.success('已取消'); invalidate() },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '取消失败'),
  })

  const executeMut = useMutation({
    mutationFn: (id: number) => changesApi.execute(id),
    onSuccess: (row: ChangeTicketItem) => {
      message.success(row.exec_result?.message || '执行完成')
      invalidate()
    },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '执行失败'),
  })

  const columns: ColumnsType<ChangeTicketItem> = [
    { title: '工单号', dataIndex: 'ticket_no', width: 160 },
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '类型', dataIndex: 'change_type_label', width: 120 },
    {
      title: '风险', dataIndex: 'risk_level', width: 80,
      render: (v: string) => <Tag color={v === 'high' ? 'red' : 'orange'}>{v === 'high' ? '高' : '中'}</Tag>,
    },
    { title: '目标', dataIndex: 'resource_name', width: 140, ellipsis: true },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (v: string) => <Tag color={STATUS_COLOR[v]}>{STATUS_LABEL[v] || v}</Tag>,
    },
    { title: '提交人', dataIndex: 'submitter', width: 100 },
    { title: '创建时间', dataIndex: 'created_at', width: 150 },
    {
      title: '操作', width: 180, fixed: 'right',
      render: (_, r) => (
        <Space>
          <Button size="small" onClick={() => setDetail(r)}>详情</Button>
          {r.status === 'approved' && canExecute && (
            <Popconfirm title="确认执行该变更？" onConfirm={() => riskConfirm.confirm('change:execute', () => executeMut.mutate(r.id))}>
              <Button size="small" type="primary" icon={<PlayCircleOutlined />}>执行</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Card size="small" title="变更工单">
        <Flex gap={12} wrap align="center" style={{ marginBottom: 12 }}>
          <Select
            style={{ width: 130 }}
            value={status}
            options={[
              { value: '', label: '全部状态' },
              ...Object.keys(STATUS_LABEL).map((k) => ({ value: k, label: STATUS_LABEL[k] })),
            ]}
            onChange={(v) => { setStatus(v); setPage(1) }}
          />
          <Select
            style={{ width: 150 }}
            value={changeType}
            options={[
              { value: '', label: '全部类型' },
              ...(types as Array<{ key: string; label: string }>).map((t) => ({ value: t.key, label: t.label })),
            ]}
            onChange={(v) => { setChangeType(v); setPage(1) }}
          />
          <Input.Search
            allowClear
            placeholder="标题 / 工单号 / 目标"
            style={{ width: 220 }}
            onSearch={(v) => { setQ(v.trim()); setPage(1) }}
          />
          <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()}>刷新</Button>
          {canSubmit && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建变更</Button>
          )}
        </Flex>
        <Table
          rowKey="id"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={data?.items || []}
          pagination={{
            current: page,
            pageSize: 20,
            total: data?.total || 0,
            showSizeChanger: false,
            onChange: (p) => setPage(p),
          }}
          scroll={{ x: 1100 }}
        />
      </Card>

      <Drawer
        title="新建变更工单"
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        width={640}
        destroyOnClose
        extra={
          <Button type="primary" loading={createMut.isPending} onClick={() => form.submit()}>
            提交
          </Button>
        }
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ risk_level: 'high', change_type: 'host.delete', approval_steps: ['管理员'] }}
          onFinish={(values) => {
            createMut.mutate({
              ...values,
              approval_steps: String(values.approval_steps || '')
                .split(/[,，]/)
                .map((s: string) => s.trim())
                .filter(Boolean),
            })
          }}
        >
          <Form.Item name="title" label="标题" rules={[{ required: true, message: '请输入标题' }]}>
            <Input placeholder="例如：删除下线主机 web-99" />
          </Form.Item>
          <Form.Item name="change_type" label="变更类型" rules={[{ required: true }]}>
            <Select
              options={(types as Array<{ key: string; label: string; auto_executor?: boolean }>).map((t) => ({
                value: t.key,
                label: t.auto_executor ? `${t.label}（可自动执行）` : t.label,
              }))}
            />
          </Form.Item>
          <Form.Item name="risk_level" label="风险等级">
            <Select options={[{ value: 'high', label: '高' }, { value: 'medium', label: '中' }]} />
          </Form.Item>
          <Form.Item name="resource_type" label="资源类型">
            <Input placeholder="host / certificate / k8s..." />
          </Form.Item>
          <Form.Item name="resource_id" label="资源 ID">
            <Input placeholder="业务 ID，如主机 instance_id" />
          </Form.Item>
          <Form.Item name="resource_name" label="资源名称">
            <Input />
          </Form.Item>
          <Form.Item name="impact" label="影响面">
            <Input.TextArea rows={3} placeholder="谁会受影响？有无流量/数据风险？" />
          </Form.Item>
          <Form.Item name="rollback_plan" label="回滚方案">
            <Input.TextArea rows={3} placeholder="出问题怎么回退？" />
          </Form.Item>
          <Form.Item name="approval_steps" label="审批链">
            <Input placeholder="逗号分隔角色，如：组长,经理；留空表示免审直接待执行" />
          </Form.Item>
          <Form.Item name="window_start" label="变更窗口起（可选）">
            <Input placeholder="YYYY-MM-DD HH:MM" />
          </Form.Item>
          <Form.Item name="window_end" label="变更窗口止（可选）">
            <Input placeholder="YYYY-MM-DD HH:MM" />
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        open={!!detail}
        title={detail ? `变更详情 ${detail.ticket_no}` : ''}
        footer={null}
        width={760}
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <Flex vertical gap={16}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="标题" span={2}>{detail.title}</Descriptions.Item>
              <Descriptions.Item label="类型">{detail.change_type_label}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[detail.status]}>{STATUS_LABEL[detail.status] || detail.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="目标">{detail.resource_name || detail.resource_id || '-'}</Descriptions.Item>
              <Descriptions.Item label="提交人">{detail.submitter}</Descriptions.Item>
              <Descriptions.Item label="影响面" span={2}>{detail.impact || '-'}</Descriptions.Item>
              <Descriptions.Item label="回滚方案" span={2}>{detail.rollback_plan || '-'}</Descriptions.Item>
              {detail.window_start || detail.window_end ? (
                <Descriptions.Item label="变更窗口" span={2}>
                  {detail.window_start || '即刻'} ~ {detail.window_end || '不限'}
                </Descriptions.Item>
              ) : null}
              {detail.exec_result?.message ? (
                <Descriptions.Item label="执行结果" span={2}>
                  <Tag color={detail.exec_result.ok ? 'green' : 'red'}>{detail.exec_result.ok ? '成功' : '失败'}</Tag>
                  {detail.exec_result.message}
                </Descriptions.Item>
              ) : null}
            </Descriptions>

            <div>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>审批进度</div>
              {!(detail.approval_steps || []).length ? (
                <Empty description="免审批（创建即可执行）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                <Steps
                  size="small"
                  current={(detail.current_step || 1) - 1}
                  status={detail.status === 'rejected' ? 'error' : detail.status === 'approved' ? 'finish' : 'process'}
                  items={(detail.approval_steps || []).map((role, i) => {
                    const log = (detail.approval_log || []).find((l) => l.step === i + 1)
                    return {
                      title: role,
                      description: log ? `${log.reviewer} · ${log.decision === 'approve' ? '同意' : '驳回'}` : '待审批',
                    }
                  })}
                />
              )}
            </div>

            <Space>
              {detail.status === 'pending' && canApprove && (
                <>
                  <Button
                    type="primary"
                    icon={<CheckOutlined />}
                    onClick={() => approveMut.mutate({ id: detail.id })}
                  >
                    批准
                  </Button>
                  <Button
                    danger
                    icon={<CloseOutlined />}
                    onClick={() => rejectMut.mutate({ id: detail.id, note: '驳回' })}
                  >
                    驳回
                  </Button>
                </>
              )}
              {detail.status === 'approved' && canExecute && (
                <Popconfirm
                  title="确认执行变更？"
                  description={detail.auto_executor ? '将自动执行目标操作' : '无自动执行器，请执行后系统仍会记录'}
                  onConfirm={() => riskConfirm.confirm('change:execute', () => executeMut.mutate(detail.id))}
                >
                  <Button type="primary" icon={<PlayCircleOutlined />} loading={executeMut.isPending}>
                    执行
                  </Button>
                </Popconfirm>
              )}
              {(detail.status === 'pending' || detail.status === 'approved') && canSubmit && (
                <Popconfirm title="取消该工单？" onConfirm={() => cancelMut.mutate(detail.id)}>
                  <Button>取消工单</Button>
                </Popconfirm>
              )}
            </Space>
          </Flex>
        )}
      </Modal>
    </Flex>
  )
}
