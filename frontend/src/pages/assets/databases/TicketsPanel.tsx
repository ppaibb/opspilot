// ───────────────────────── SQL 工单 Tab ─────────────────────────
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Form, Input, InputNumber, Select,
  App, Tooltip, Empty, Drawer, Descriptions, Alert, DatePicker, Row, Col, List, Steps,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { FormInstance } from 'antd'
import {
  PlusOutlined, ReloadOutlined, PlayCircleOutlined, CheckCircleOutlined, CloseCircleOutlined,
  SearchOutlined, ClockCircleOutlined, DownloadOutlined, CopyOutlined, CheckOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import axios from 'axios'
import { dbApi } from '@/api'
import { useAuthStore } from '@/store/auth'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import DbObjectBrowser from '@/components/db/DbObjectBrowser'
import type { DbApprovalLogItem, DbAuditFinding, DbExecResultItem, DbInstance, DbTicket } from '@/types/api'
import { TICKET_STATUS, sqlBrief, type TicketFormValues } from './types'

const { Text } = Typography
const { TextArea } = Input

export function TicketsTab({
  tickets, loading, refetch, instances, statusFilter, setStatusFilter, keyword, setKeyword, onGoSelfQuery,
}: {
  tickets: DbTicket[]
  loading: boolean
  refetch: () => void
  instances: DbInstance[]
  statusFilter: string
  setStatusFilter: (v: string) => void
  keyword: string
  setKeyword: (v: string) => void
  /** 空态引导跳自助查询（父组件切 Tab 用） */
  onGoSelfQuery?: () => void
}) {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  // 三权分离：submit 提交工单 / execute 执行工单 / approve 审批工单。
  const canSubmit = has('action.db.submit')
  const canExecute = has('action.db.execute')
  const canApprove = has('action.db.approve')
  // 多级审批：链非空时只有当前级角色的用户能看到审批按钮（后端同样校验，前端只是收窄入口）。
  const myRole = useAuthStore((s) => s.userInfo?.role || '')
  const canReviewTicket = (record: DbTicket) => {
    if (!canApprove) return false
    const steps = record.approval_steps || []
    if (!steps.length) return true
    return (steps[(record.current_step || 1) - 1] || '') === myRole
  }
  /** pending 工单的当前级提示（多级时展示「第 n/m 级 · 角色」）。 */
  const renderStepHint = (record: DbTicket) => {
    const steps = record.approval_steps || []
    if (record.status !== 'pending' || !steps.length) return null
    const step = Math.max(1, record.current_step || 1)
    const role = steps[step - 1] || ''
    const mine = role === myRole
    return (
      <Tooltip title={`多级审批：当前第 ${step}/${steps.length} 级，由「${role}」审批`}>
        <Tag color={mine ? 'orange' : 'default'}>{step}/{steps.length} · {role}{mine ? '（待我审）' : ''}</Tag>
      </Tooltip>
    )
  }

  const [submitOpen, setSubmitOpen] = useState(false)
  // 实例下拉只展示当前用户可用实例（全员实例 + 与我角色匹配的授权实例；后端 403 兜底）。
  const usableInstances = useMemo(
    () => instances.filter((i) => {
      const roles: string[] = i.allowed_roles || []
      return roles.length === 0 || roles.includes(myRole)
    }),
    [instances, myRole],
  )
  const [detailTicket, setDetailTicket] = useState<DbTicket | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [form] = Form.useForm<TicketFormValues>()
  // 库表面板的选中态：与表单的「目标实例」联动，提交时作为工单的目标库。
  const [browserInstance, setBrowserInstance] = useState<number | null>(null)
  const [browserDatabase, setBrowserDatabase] = useState('')

  const invalidateTickets = () => {
    queryClient.invalidateQueries({ queryKey: ['db-tickets'] })
    setSelectedIds([])
  }

  const submitMutation = useMutation({
    mutationFn: (values: TicketFormValues) => dbApi.createTicket(values),
    onSuccess: (ticket) => {
      if (ticket.ticket_type === 'query') {
        message.success('查询授权申请已提交，等待审批')
      } else {
        const warns = (ticket.audit_result || []).filter((f) => f.level === 'warn')
        message.success(`工单已提交（${warns.length ? `含 ${warns.length} 条警告` : '审核通过'}），等待审批`)
      }
      setSubmitOpen(false)
      invalidateTickets()
    },
    onError: (error: unknown) => {
      // 审核引擎拦截时后端返回对象型 detail（message + findings 明细）
      const data = axios.isAxiosError(error)
        ? (error.response?.data as { detail?: { message?: string; findings?: string[] } | string } | undefined)
        : undefined
      const detail = data?.detail
      if (detail && typeof detail === 'object' && detail.findings) message.error(`${detail.message}：${detail.findings.join('；')}`)
      else message.error(typeof detail === 'string' ? detail : '提交失败')
    },
  })

  const reviewMutation = useMutation({
    mutationFn: ({ id, decision }: { id: number; decision: 'approve' | 'reject' }) =>
      dbApi.reviewTicket(id, { decision }),
    onSuccess: (ticket, vars) => {
      if (vars.decision === 'reject') {
        message.success('已驳回')
      } else if (ticket.status === 'pending') {
        message.success('本级已批准，流转到下一级审批')
      } else if (ticket.ticket_type === 'query') {
        message.success('终审通过，查询授权已生效')
      } else {
        message.success('已批准，等待执行')
      }
      invalidateTickets()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '审批失败'),
  })

  const executeMutation = useMutation({
    mutationFn: (id: number) => dbApi.executeTicket(id),
    onSuccess: (ticket) => {
      if (ticket.status === 'success') message.success('执行成功')
      else message.warning('执行失败，详见工单详情')
      invalidateTickets()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '执行失败'),
  })

  // 批准确认：展示 SQL 摘要 / 授权明细，避免误点
  const confirmApprove = (ids: number[]) => {
    const targets = tickets.filter((t) => ids.includes(t.id))
    const briefs = targets
      .map((t) => (t.ticket_type === 'query'
        ? `#${t.id} [查询授权] ${t.database || '-'} · ${t.grant_detail?.hours ?? '-'}h · ${t.title}`
        : `#${t.id} ${sqlBrief(t.sql_text, 60)}`))
      .join('\n')
    const multi = targets.some((t) => (t.approval_steps || []).length > 1)
    modal.confirm({
      title: ids.length > 1 ? `批量批准 ${ids.length} 张工单？` : '批准该工单？',
      content: (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text type="secondary">
            {multi
              ? '多级审批工单：批准后流转到下一级审批人，全部级次通过后生效。'
              : '批准后工单进入「待执行」，由执行人手动执行；查询工单批准即授权生效。'}
          </Text>
          <pre style={{ margin: 0, maxHeight: 160, overflow: 'auto', fontSize: 12, fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>{briefs}</pre>
        </Space>
      ),
      okText: '批准',
      cancelText: '取消',
      onOk: () => Promise.all(ids.map((id) => reviewMutation.mutateAsync({ id, decision: 'approve' as const }))),
    })
  }

  const confirmReject = (id: number) => {
    modal.confirm({
      title: '驳回工单', content: '确定驳回该 SQL 工单？驳回后提交人将收到通知。',
      okText: '驳回', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: () => reviewMutation.mutate({ id, decision: 'reject' }),
    })
  }

  const confirmExecute = (id: number) => {
    const ticket = tickets.find((t) => t.id === id)
    modal.confirm({
      title: '执行 SQL 工单？',
      content: (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text type="secondary">
            将在「{ticket?.instance_name}」上直接执行；UPDATE/DELETE 会自动备份受影响数据并生成回滚语句。
          </Text>
          <pre style={{ margin: 0, maxHeight: 160, overflow: 'auto', fontSize: 12, fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
            {sqlBrief(ticket?.sql_text || '', 200)}
          </pre>
        </Space>
      ),
      okText: '确认执行', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: () => executeMutation.mutate(id),
    })
  }

  const columns: ColumnsType<DbTicket> = [
    {
      title: '工单', dataIndex: 'title', width: 220, ellipsis: true,
      render: (v: string, record) => (
        <Space size={6}>
          <Tag color={record.ticket_type === 'query' ? 'cyan' : 'geekblue'}>
            {record.ticket_type === 'query' ? '查询授权' : '变更'}
          </Tag>
          <a onClick={() => setDetailTicket(record)}>{v}</a>
        </Space>
      ),
    },
    { title: '实例', dataIndex: 'instance_name', width: 140, ellipsis: true },
    {
      title: '状态', dataIndex: 'status', width: 170,
      render: (v: string, record) => {
        const meta = TICKET_STATUS[v] || { color: 'default', label: v }
        return (
          <Space size={4} wrap>
            <Tag color={meta.color}>{meta.label}</Tag>
            {record.execute_after && v === 'approved' && (
              <Tooltip title={`定时执行：${record.execute_after}`}>
                <Tag color="purple" icon={<ClockCircleOutlined />}>定时</Tag>
              </Tooltip>
            )}
            {renderStepHint(record)}
          </Space>
        )
      },
    },
    { title: '提交人', dataIndex: 'submitter', width: 90 },
    { title: '审批人', dataIndex: 'reviewer', width: 90, render: (v: string) => v || '-' },
    { title: '提交时间', dataIndex: 'created_at', width: 150 },
    {
      title: '操作', key: 'ops', width: 220, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          {record.status === 'pending' && canReviewTicket(record) && (
            <>
              <Button size="small" type="primary" icon={<CheckCircleOutlined />}
                loading={reviewMutation.isPending && reviewMutation.variables?.id === record.id && reviewMutation.variables?.decision === 'approve'}
                onClick={() => confirmApprove([record.id])}>
                批准
              </Button>
              <Button size="small" danger icon={<CloseCircleOutlined />}
                loading={reviewMutation.isPending && reviewMutation.variables?.id === record.id && reviewMutation.variables?.decision === 'reject'}
                onClick={() => confirmReject(record.id)}>
                驳回
              </Button>
            </>
          )}
          {record.status === 'approved' && canExecute && (
            <Button size="small" type="primary" danger icon={<PlayCircleOutlined />}
              loading={executeMutation.isPending && executeMutation.variables === record.id}
              onClick={() => confirmExecute(record.id)}>
              执行
            </Button>
          )}
          {record.has_rollback && (
            <Tooltip title="下载回滚语句（执行前自动生成的反向 INSERT）">
              <Button size="small" type="text" icon={<DownloadOutlined />}
                onClick={() => window.open(`/api/db/tickets/${record.id}/rollback`)} />
            </Tooltip>
          )}
          <Button size="small" onClick={() => setDetailTicket(record)}>详情</Button>
        </Space>
      ),
    },
  ]

  return (
    <Card size="small">
      <Flex justify="space-between" wrap gap="small" style={{ marginBottom: 12 }}>
        <Space>
          <Select
            value={statusFilter} onChange={setStatusFilter} style={{ width: 130 }} allowClear
            placeholder="全部状态"
            options={Object.entries(TICKET_STATUS).map(([value, meta]) => ({ value, label: meta.label }))}
          />
          <Input
            prefix={<SearchOutlined />}
            allowClear
            placeholder="搜索标题 / 实例 / 提交人"
            style={{ width: 220 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refetch}>刷新</Button>
          {canSubmit && (
            <Button type="primary" icon={<PlusOutlined />} disabled={instances.length === 0}
              onClick={() => { form.resetFields(); setSubmitOpen(true) }}>
              提交工单
            </Button>
          )}
        </Space>
      </Flex>

      {instances.length === 0 && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message={has('action.db.manage')
            ? '还没有数据源，请先到「数据源」Tab 添加数据库实例'
            : '还没有可用数据源，请联系管理员添加'}
        />
      )}

      {selectedIds.length > 0 && (
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message={
            <Space>
              <Text>已选 {selectedIds.length} 张待审批工单</Text>
              <Button size="small" type="primary" icon={<CheckOutlined />}
                loading={reviewMutation.isPending}
                onClick={() => confirmApprove(selectedIds)}>
                批量批准
              </Button>
              <Button size="small" onClick={() => setSelectedIds([])}>取消选择</Button>
            </Space>
          }
        />
      )}

      <Table<DbTicket>
        rowKey="id" size="small" columns={columns} dataSource={tickets}
        loading={loading}
        rowSelection={canApprove ? {
          selectedRowKeys: selectedIds,
          onChange: (keys) => setSelectedIds(keys as number[]),
          // 只有「轮到我审」的行可勾选（多级审批时其他级次的行不进批量批准）。
          getCheckboxProps: (record) => ({
            disabled: record.status !== 'pending' || !canReviewTicket(record),
          }),
          preserveSelectedRowKeys: false,
        } : undefined}
        pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (t) => `共 ${t} 张` }}
        locale={{
          emptyText:
            keyword || statusFilter ? (
              <Empty description='没有符合条件的工单' image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span style={{ color: '#8c8c8c', fontSize: 13 }}>
                    暂无变更工单；SELECT 查询不走工单——
                    <a onClick={() => onGoSelfQuery?.()}>到「自助查询」执行（需查询授权）</a>
                  </span>
                }
              />
            ),
        }}
      />

      <SubmitTicketDrawer
        open={submitOpen}
        onClose={() => setSubmitOpen(false)}
        instances={usableInstances}
        form={form}
        browserInstance={browserInstance}
        setBrowserInstance={setBrowserInstance}
        browserDatabase={browserDatabase}
        setBrowserDatabase={setBrowserDatabase}
        loading={submitMutation.isPending}
        onSubmit={(values) => submitMutation.mutate(values)}
      />

      <TicketDetailDrawer ticket={detailTicket} onClose={() => setDetailTicket(null)} />
    </Card>
  )
}

/** 提交工单抽屉：变更走 SQL 编辑区 + 库表浏览器；查询工单切「授权申请」表单。 */
function SubmitTicketDrawer({
  open, onClose, instances, form, browserInstance, setBrowserInstance, browserDatabase, setBrowserDatabase,
  loading, onSubmit,
}: {
  open: boolean
  onClose: () => void
  instances: DbInstance[]
  form: FormInstance<TicketFormValues>
  browserInstance: number | null
  setBrowserInstance: (v: number | null) => void
  browserDatabase: string
  setBrowserDatabase: (v: string) => void
  loading: boolean
  onSubmit: (values: TicketFormValues) => void
}) {
  // 查询工单 = 查询授权申请：不需要 SQL，填库/表范围/时长/理由。
  const ticketType = Form.useWatch('ticket_type', form)
  const isQuery = ticketType === 'query'
  const watchInstanceId = Form.useWatch('instance_id', form)

  // 授权申请的目标库 / 表范围下拉数据。
  const { data: dbList } = useQuery({
    queryKey: ['db-databases', watchInstanceId],
    queryFn: () => dbApi.listDatabases(watchInstanceId!),
    enabled: open && isQuery && !!watchInstanceId,
    staleTime: 60 * 1000,
  })
  const grantDatabase = Form.useWatch('grant_database', form) as string | undefined
  const { data: grantTables = [] } = useQuery({
    queryKey: ['db-tables', watchInstanceId, grantDatabase],
    queryFn: () => dbApi.listTables(watchInstanceId!, grantDatabase || ''),
    enabled: open && isQuery && !!watchInstanceId && !!grantDatabase,
    staleTime: 60 * 1000,
  })

  return (
    <Drawer
      title="提交 SQL 工单"
      open={open}
      onClose={onClose}
      width={920}
      extra={<Button type="primary" loading={loading} onClick={() => form.submit()}>{isQuery ? '提交申请' : '提交（自动审核）'}</Button>}
    >
      <Form
        form={form} layout="vertical"
        onFinish={(values) => {
          const { execute_after_picker, grant_database, grant_tables, grant_hours, grant_reason, ...rest } = values
          if (ticketType === 'query') {
            onSubmit({
              ...rest,
              database: grant_database,
              grant_tables: grant_tables || [],
              grant_hours: grant_hours || 4,
              grant_reason: grant_reason || '',
            })
            return
          }
          onSubmit({
            ...rest,
            database: browserDatabase || undefined,
            execute_after: execute_after_picker ? execute_after_picker.format('YYYY-MM-DD HH:mm:ss') : '',
          })
        }}
      >
        <Space size="large" style={{ display: 'flex' }}>
          <Form.Item name="instance_id" label="目标实例" rules={[{ required: true, message: '请选择实例' }]} style={{ minWidth: 280 }}>
            <Select
              showSearch optionFilterProp="label"
              onChange={() => { if (ticketType === 'query') form.setFieldsValue({ grant_database: undefined, grant_tables: [] }) }}
              options={instances.map((i) => ({ value: i.id, label: `${i.name}（${i.db_type} ${i.host}）` }))}
            />
          </Form.Item>
          <Form.Item
            name="ticket_type" label="工单类型" initialValue="change" rules={[{ required: true }]}
            extra={isQuery ? '查询工单 = 申请限时查询授权，终审通过后自动生效' : undefined}
          >
            <Select style={{ width: 190 }} options={[
              { value: 'change', label: '变更（DDL/DML）' },
              { value: 'query', label: '查询授权申请' },
            ]} />
          </Form.Item>
        </Space>

        {isQuery ? (
          <>
            <Form.Item name="title" label="标题（可选）">
              <Input placeholder="如：排查订单超时问题需要查库" />
            </Form.Item>
            <Space size="large" style={{ display: 'flex' }} align="start">
              <Form.Item name="grant_database" label="目标库" rules={[{ required: true, message: '请选择授权目标库' }]} style={{ minWidth: 220 }}>
                <Select
                  showSearch placeholder="选择库"
                  options={(dbList?.databases ?? []).map((d) => ({
                    value: d,
                    label: d === dbList?.default_database ? `${d}（默认）` : d,
                  }))}
                />
              </Form.Item>
              <Form.Item name="grant_tables" label="表范围（可选，留空 = 全部表）" style={{ minWidth: 260 }}>
                <Select
                  mode="multiple" allowClear showSearch placeholder="全部表"
                  maxTagCount="responsive"
                  options={grantTables.map((t) => ({ value: t.name, label: `${t.name}（约 ${t.rows} 行）` }))}
                />
              </Form.Item>
              <Form.Item name="grant_hours" label="时长（小时）" initialValue={4} rules={[{ required: true }]}>
                <InputNumber min={1} max={720} style={{ width: 110 }} />
              </Form.Item>
            </Space>
            <Form.Item name="grant_reason" label="申请理由" extra="审批人将根据理由与范围判断是否放行">
              <Input.TextArea rows={3} placeholder="如：排查订单表 status 字段异常，需要查最近一天的订单数据" />
            </Form.Item>
            <Alert type="info" showIcon
              message="查询授权说明：终审通过后授权立即生效，到期自动回收；期间可在「自助查询」页执行只读语句，全部查询落审计日志。" />
          </>
        ) : (
          <>
            <Form.Item name="title" label="标题（可选）">
              <Input placeholder="如：订单表加索引" />
            </Form.Item>
            <Form.Item
              name="execute_after_picker" label="定时执行（可选，留空=批准后手动执行）"
              extra="低峰期变更建议定时：批准后由系统在指定时间自动执行"
            >
              <DatePicker
                showTime
                style={{ width: 260 }}
                format="YYYY-MM-DD HH:mm:ss"
                disabledDate={(d) => d.isBefore(dayjs(), 'day')}
              />
            </Form.Item>
            <Form.Item
              label={
                <Space size={4}>
                  SQL
                  <Tooltip title="Ctrl / Cmd + Enter 快捷提交">
                    <Tag style={{ marginInlineEnd: 0 }}>⌘⏎ 提交</Tag>
                  </Tooltip>
                </Space>
              }
              extra="提交即自动审核：DROP DATABASE/TABLE、TRUNCATE、无 WHERE 的 UPDATE/DELETE 会被直接拒绝"
            >
              <Row gutter={12}>
                <Col span={8}>
                  <DbObjectBrowser
                    instances={instances}
                    instanceId={browserInstance}
                    onInstanceChange={(id) => { setBrowserInstance(id); setBrowserDatabase(''); form.setFieldsValue({ instance_id: id }) }}
                    database={browserDatabase}
                    onDatabaseChange={setBrowserDatabase}
                    onPickTable={(db, table) => {
                      const dbType = instances.find((i) => i.id === browserInstance)?.db_type
                      const quote = dbType === 'postgresql' ? '"' : '`'
                      // PostgreSQL 的连接已选定数据库，`库.表` 会被当成 `schema.table`；
                      // MySQL 才支持用 `database.table` 在同一连接里限定库。
                      const qualified = dbType === 'postgresql'
                        ? `${quote}${table}${quote}`
                        : `${quote}${db}${quote}.${quote}${table}${quote}`
                      const current = form.getFieldValue('sql_text') || ''
                      form.setFieldsValue({ sql_text: current ? `${current}\n${qualified}` : qualified })
                    }}
                    compact
                  />
                </Col>
                <Col span={16}>
                  <Form.Item
                    name="sql_text"
                    rules={[{ required: true, message: '请输入 SQL' }]}
                    style={{ marginBottom: 0 }}
                  >
                    <TextArea
                      rows={14}
                      placeholder={'ALTER TABLE orders ADD INDEX idx_user(user_id);\nUPDATE orders SET status=1 WHERE id=100;'}
                      style={{ fontFamily: 'monospace', fontSize: 12 }}
                      onKeyDown={(e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                          e.preventDefault()
                          form.submit()
                        }
                      }}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </Form.Item>
          </>
        )}
      </Form>
    </Drawer>
  )
}

function TicketDetailDrawer({ ticket, onClose }: { ticket: DbTicket | null; onClose: () => void }) {
  const { message } = App.useApp()
  const columns: ColumnsType<DbExecResultItem> = [
    { title: '#', dataIndex: 'index', width: 40 },
    {
      title: '语句', dataIndex: 'sql', ellipsis: true,
      render: (v: string) => <Text code style={{ fontSize: 11 }}>{v}</Text>,
    },
    {
      title: '结果', dataIndex: 'ok', width: 90,
      render: (v: boolean, r) => v
        ? <Tag color="green">{r.rows_affected ?? 0} 行</Tag>
        : <Tooltip title={r.error}><Tag color="red">失败</Tag></Tooltip>,
    },
    { title: '耗时', dataIndex: 'duration_ms', width: 80, render: (v: number) => `${v}ms` },
  ]

  const copySql = async () => {
    if (!ticket?.sql_text) return
    try {
      await navigator.clipboard.writeText(ticket.sql_text)
      message.success('SQL 已复制')
    } catch {
      message.warning('浏览器不支持自动复制，请手动选择')
    }
  }

  // 多级审批链展示：Steps 列出每级角色，当前级高亮，已审级显示审批人与结论。
  const steps = ticket?.approval_steps || []
  const log = ticket?.approval_log || []
  const currentStep = Math.max(1, ticket?.current_step || 1)

  return (
    <Drawer title={`工单详情 - ${ticket?.title || ''}`} open={!!ticket} onClose={onClose} width={760}>
      {ticket && (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="状态"><Tag color={TICKET_STATUS[ticket.status]?.color}>{TICKET_STATUS[ticket.status]?.label || ticket.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="类型">
              <Tag color={ticket.ticket_type === 'query' ? 'cyan' : 'geekblue'}>
                {ticket.ticket_type === 'query' ? '查询授权' : '变更'}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="实例">{ticket.instance_name}{ticket.database ? ` · ${ticket.database}` : ''}</Descriptions.Item>
            <Descriptions.Item label="提交">{ticket.submitter} · {ticket.created_at}</Descriptions.Item>
            <Descriptions.Item label="审批">{ticket.reviewer || '-'}{ticket.review_note ? `（${ticket.review_note}）` : ''}</Descriptions.Item>
            <Descriptions.Item label="执行">{ticket.executor || '-'} · {ticket.executed_at || '-'}</Descriptions.Item>
            {ticket.execute_after && (
              <Descriptions.Item label="定时执行" span={2}>
                <Tag color="purple" icon={<ClockCircleOutlined />}>{ticket.execute_after}</Tag>
              </Descriptions.Item>
            )}
            {ticket.ticket_type === 'query' && ticket.grant_detail && (
              <Descriptions.Item label="授权申请" span={2}>
                <Space size={6} wrap>
                  <Tag color="cyan">库：{ticket.database || '-'}</Tag>
                  <Tag>{ticket.grant_detail.hours ?? '-'} 小时</Tag>
                  <Tag>{ticket.grant_detail.tables?.length ? `限定 ${ticket.grant_detail.tables.length} 张表` : '全部表'}</Tag>
                  {ticket.grant_detail.reason && <Text type="secondary">理由：{ticket.grant_detail.reason}</Text>}
                </Space>
              </Descriptions.Item>
            )}
            {ticket.has_backup && (
              <Descriptions.Item label="变更备份" span={2}>
                <Space>
                  <Tag color="green">已自动备份受影响数据</Tag>
                  {ticket.has_rollback && (
                    <Button size="small" icon={<DownloadOutlined />}
                      onClick={() => window.open(`/api/db/tickets/${ticket.id}/rollback`)}>
                      下载回滚语句
                    </Button>
                  )}
                </Space>
              </Descriptions.Item>
            )}
          </Descriptions>

          {steps.length > 0 && (
            <Card size="small" title={`审批链（${steps.length} 级）`}>
              <Steps
                size="small"
                current={
                  ticket.status === 'pending'
                    ? currentStep - 1
                    : steps.length
                }
                status={ticket.status === 'rejected' ? 'error' : ticket.status === 'pending' ? 'process' : 'finish'}
                items={steps.map((role, idx) => {
                  const entry = log.find((l: DbApprovalLogItem) => l.step === idx + 1)
                  const isCurrent = ticket.status === 'pending' && idx === currentStep - 1
                  return {
                    title: `第 ${idx + 1} 级 · ${role}`,
                    description: entry
                      ? `${entry.reviewer} ${entry.decision === 'approve' ? '批准' : '驳回'}${entry.note ? `（${entry.note}）` : ''}`
                      : isCurrent ? '待本级审批' : undefined,
                  }
                })}
              />
            </Card>
          )}

          {!!ticket.audit_result?.length && (
            <Card
              size="small"
              title="审核结果（规则引擎）"
              styles={{ body: { padding: ticket.audit_result.length > 4 ? '12px 12px 12px 12px' : undefined } }}
            >
              <List
                size="small"
                dataSource={ticket.audit_result}
                renderItem={(f: DbAuditFinding, idx: number) => (
                  <List.Item key={idx} style={{ padding: '6px 0' }}>
                    <Space size={8}>
                      <Tag color={f.level === 'block' ? 'red' : 'orange'}>{f.rule}</Tag>
                      <Text style={{ fontSize: 12 }}>{f.message}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            </Card>
          )}

          <Card
            size="small"
            title="SQL"
            extra={<Button size="small" type="link" icon={<CopyOutlined />} onClick={copySql}>复制</Button>}
          >
            <pre style={{ margin: 0, maxHeight: 240, overflow: 'auto', fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>{ticket.sql_text}</pre>
          </Card>

          {!!ticket.exec_result?.length && (
            <Card size="small" title="执行结果">
              <Table rowKey="index" size="small" columns={columns} dataSource={ticket.exec_result} pagination={false} />
            </Card>
          )}
        </Space>
      )}
    </Drawer>
  )
}
