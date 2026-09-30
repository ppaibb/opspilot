import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Tag, Flex, Typography, Drawer, Form, Input, Select, Switch,
  Popconfirm, Tooltip, Badge, Alert, Empty, App,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, ReloadOutlined, SyncOutlined, CheckCircleOutlined, HistoryOutlined,
} from '@ant-design/icons'
import { financeApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import type { BillSyncRunItem, CloudAccountItem, CredentialOption } from '@/types/api'

const { Text } = Typography

const STATUS_META: Record<string, { color: 'success' | 'processing' | 'error' | 'default' | 'warning'; label: string }> = {
  active: { color: 'success', label: '正常' },
  pending: { color: 'processing', label: '待校验' },
  error: { color: 'error', label: '异常' },
}

export default function FinanceAccountsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const { confirm } = useRiskConfirm()
  const canManage = has('action.finance.accounts.manage')
  const canSync = has('action.finance.sync')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<CloudAccountItem | null>(null)
  const [recordsAccount, setRecordsAccount] = useState<CloudAccountItem | null>(null)
  const [form] = Form.useForm()

  const { data: accounts = [], isLoading, refetch } = useQuery({
    queryKey: ['finance-accounts'],
    queryFn: financeApi.listAccounts,
  })

  const { data: credentialOptions = [] } = useQuery({
    queryKey: ['finance-credential-options'],
    queryFn: financeApi.listCredentialOptions,
    enabled: drawerOpen,
  })

  const { data: syncRecords = [] } = useQuery({
    queryKey: ['finance-sync-records', recordsAccount?.id],
    queryFn: () => financeApi.listSyncRecords(recordsAccount!.id, 30),
    enabled: !!recordsAccount,
    refetchInterval: (query) =>
      (query.state.data || []).some((r) => r.status === 'running') ? 3000 : false,
  })

  const { saveMut: saveMutation, deleteMut: deleteMutation } = useCrudResource<
    { name: string; credential_ref: string; enabled: boolean },
    number
  >({
    queryKey: ['finance-accounts'],
    entityName: '云账号',
    createFn: financeApi.createAccount,
    updateFn: financeApi.updateAccount,
    deleteFn: financeApi.deleteAccount,
    onSaved: () => setDrawerOpen(false),
  })

  const verifyMutation = useMutation({
    mutationFn: (id: number) => financeApi.verifyAccount(id),
    onSuccess: (account: CloudAccountItem) => {
      if (account.status === 'active') message.success(`「${account.name}」凭证校验通过`)
      else message.warning(`「${account.name}」校验失败：${account.status_message || '未知原因'}`)
      queryClient.invalidateQueries({ queryKey: ['finance-accounts'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '校验失败'),
  })

  const syncMutation = useMutation({
    mutationFn: (id: number) => financeApi.syncAccount(id, { include_previous: true }),
    onSuccess: () => {
      message.success('同步任务已提交，账单较大时约需 1-2 分钟')
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '触发同步失败'),
  })

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setDrawerOpen(true)
  }

  const openEdit = (account: CloudAccountItem) => {
    setEditing(account)
    form.setFieldsValue({
      name: account.name,
      credential_ref: account.credential_ref,
      enabled: account.enabled,
    })
    setDrawerOpen(true)
  }

  const columns: ColumnsType<CloudAccountItem> = [
    {
      title: '账号名称', dataIndex: 'name', width: 180,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '云厂商', dataIndex: 'provider', width: 100,
      render: () => <Tag>阿里云</Tag>,
    },
    {
      title: '绑定凭证', dataIndex: 'credential_name', width: 180, ellipsis: true,
      render: (v: string, record) => v || <Text type="secondary">{record.credential_ref || '未绑定'}</Text>,
    },
    {
      title: '云账号 UID', dataIndex: 'vendor_account_id', width: 160,
      render: (v: string) => v ? <Text copyable={{ text: v }}>{v.length > 18 ? `${v.slice(0, 8)}…${v.slice(-6)}` : v}</Text> : <Text type="secondary">同步后自动识别</Text>,
    },
    {
      title: '状态', dataIndex: 'status', width: 130,
      render: (v: string, record) => {
        const meta = STATUS_META[v] || STATUS_META.pending
        return (
          <Tooltip title={record.status_message || ''} placement="topLeft">
            <Badge status={meta.color} text={meta.label} />
          </Tooltip>
        )
      },
    },
    {
      title: '自动同步', dataIndex: 'enabled', width: 90,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: '最近同步', dataIndex: 'last_sync_at', width: 170,
      render: (v: string) => v || <Text type="secondary">从未同步</Text>,
    },
    {
      title: '操作', key: 'actions', width: 260, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Tooltip title="手动同步当月与上月账单">
            <Button
              size="small" icon={<SyncOutlined />}
              disabled={!canSync}
              loading={syncMutation.isPending && syncMutation.variables === record.id}
              onClick={() => syncMutation.mutate(record.id)}
            >
              同步
            </Button>
          </Tooltip>
          <Tooltip title="校验 AK/SK 凭证">
            <Button
              size="small" icon={<CheckCircleOutlined />}
              disabled={!canManage}
              loading={verifyMutation.isPending && verifyMutation.variables === record.id}
              onClick={() => verifyMutation.mutate(record.id)}
            />
          </Tooltip>
          <Tooltip title="同步记录">
            <Button size="small" icon={<HistoryOutlined />} onClick={() => setRecordsAccount(record)} />
          </Tooltip>
          <Button size="small" disabled={!canManage} onClick={() => openEdit(record)}>编辑</Button>
          <Popconfirm
            title="删除云账号"
            description="将同时删除该账号已同步的全部账单数据，不可恢复。"
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            // 后端云账号删除挂 finance 守卫动作，需确认 + 一次性票据
            onConfirm={() => confirm('finance:delete', () => deleteMutation.mutate(record.id))}
            disabled={!canManage}
          >
            <Button size="small" danger disabled={!canManage}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const runColumns: ColumnsType<BillSyncRunItem> = [
    { title: '账期', dataIndex: 'billing_cycle', width: 90 },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v: string) => (
        <Tag color={v === 'success' ? 'green' : v === 'failed' ? 'red' : 'blue'}>
          {v === 'success' ? '成功' : v === 'failed' ? '失败' : '进行中'}
        </Tag>
      ),
    },
    { title: '触发', dataIndex: 'trigger', width: 80, render: (v: string) => (v === 'manual' ? '手动' : '定时') },
    { title: '落库行数', dataIndex: 'rows_synced', width: 90 },
    { title: '开始时间', dataIndex: 'started_at', width: 160 },
    {
      title: '错误信息', dataIndex: 'error_message', ellipsis: true,
      render: (v: string) => (v ? <Text type="danger">{v}</Text> : '-'),
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>云账号</Typography.Title>
          <Text type="secondary">纳管阿里云账号，同步费用中心账单（AK/SK 引用凭证保险箱，建议只授予 AliyunBSSReadOnlyAccess）</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canManage} onClick={openCreate}>
            新增云账号
          </Button>
        </Space>
      </Flex>

      {accounts.length === 0 && !isLoading && (
        <Alert
          type="info" showIcon style={{ marginBottom: 16 }}
          message="还没有云账号"
          description={<>先在 <Text code>资产管理 → 凭证管理</Text> 创建 AK/SK 凭证，然后在这里绑定即可开始同步账单。</>}
        />
      )}

      <Table<CloudAccountItem>
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={isLoading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无云账号" /> }}
      />

      <Drawer
        title={editing ? '编辑云账号' : '新增云账号'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={480}
        destroyOnClose
        extra={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button
              type="primary"
              loading={saveMutation.isPending}
              onClick={() => form.submit()}
            >
              保存
            </Button>
          </Space>
        }
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) =>
            // 创建云账号是守卫动作（finance:create）：确认取票后再提交；编辑不取票
            editing
              ? saveMutation.mutate({ id: editing.id, data: values })
              : confirm('finance:create', () => saveMutation.mutate({ data: values }))
          }
          initialValues={{ enabled: true }}
        >
          <Form.Item name="name" label="账号名称" rules={[{ required: true, message: '请输入账号名称' }]}>
            <Input placeholder="如：生产环境-阿里云主账号" />
          </Form.Item>
          <Form.Item label="云厂商">
            <Input value="阿里云" disabled />
          </Form.Item>
          <Form.Item
            name="credential_ref" label="AK/SK 凭证"
            rules={[{ required: true, message: '请选择凭证' }]}
            extra="仅列出对你可见的 AK/SK 类型凭证；密钥内容保存在凭证保险箱中"
          >
            <Select
              placeholder="选择凭证"
              options={credentialOptions.map((c: CredentialOption) => ({ value: c.cred_id, label: `${c.name}（${c.type}）` }))}
              showSearch optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item name="enabled" label="自动同步" valuePropName="checked" extra="开启后每 6 小时自动刷新当月与上月账单">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Drawer>

      <Drawer
        title={`同步记录 - ${recordsAccount?.name || ''}`}
        open={!!recordsAccount}
        onClose={() => setRecordsAccount(null)}
        width={720}
      >
        <Table<BillSyncRunItem>
          rowKey="id"
          columns={runColumns}
          dataSource={syncRecords}
          pagination={false}
          size="small"
          loading={!syncRecords.length && !!recordsAccount}
        />
      </Drawer>
    </div>
  )
}
