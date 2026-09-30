// ───────────────────────── 查询授权 Tab ─────────────────────────
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Form, Input, InputNumber, Select,
  App, Tooltip, Empty, Drawer,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { dbApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { DbInstance, DbQueryGrant } from '@/types/api'
import { GRANT_STATUS } from './types'

const { Text } = Typography

export function GrantsTab({ instances }: { instances: DbInstance[] }) {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.db.manage')
  const [scope, setScope] = useState<'mine' | 'all'>('mine')
  const [grantOpen, setGrantOpen] = useState(false)
  const [form] = Form.useForm()

  const { data: grants = [], isLoading, refetch } = useQuery({
    queryKey: ['db-query-grants', scope],
    queryFn: () => dbApi.listQueryGrants(scope),
  })

  const revokeMutation = useMutation({
    mutationFn: (id: number) => dbApi.revokeQueryGrant(id),
    onSuccess: () => {
      message.success('授权已吊销')
      queryClient.invalidateQueries({ queryKey: ['db-query-grants'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '吊销失败'),
  })

  const createMutation = useMutation({
    mutationFn: (values: { username: string; instance_id: number; database: string; tables?: string[]; hours: number; reason?: string }) => dbApi.createQueryGrant(values),
    onSuccess: () => {
      message.success('授权已创建，立即生效')
      setGrantOpen(false)
      queryClient.invalidateQueries({ queryKey: ['db-query-grants'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '创建失败'),
  })

  const confirmRevoke = (record: DbQueryGrant) => {
    modal.confirm({
      title: `吊销该查询授权？`,
      content: `${record.display_name || record.username} 在「${record.database}」库的查询授权将立即失效，本人会收到通知。`,
      okText: '吊销', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: () => revokeMutation.mutate(record.id),
    })
  }

  // 手动授权抽屉的库 / 表下拉数据（选实例 → 带出库 → 带出表）。
  const watchInstanceId = Form.useWatch('instance_id', form)
  const watchDatabase = Form.useWatch('database', form)
  const { data: dbList } = useQuery({
    queryKey: ['db-databases', watchInstanceId],
    queryFn: () => dbApi.listDatabases(watchInstanceId!),
    enabled: grantOpen && !!watchInstanceId,
    staleTime: 60 * 1000,
  })
  const { data: tableList = [] } = useQuery({
    queryKey: ['db-tables', watchInstanceId, watchDatabase],
    queryFn: () => dbApi.listTables(watchInstanceId!, watchDatabase),
    enabled: grantOpen && !!watchInstanceId && !!watchDatabase,
    staleTime: 60 * 1000,
  })

  const columns: ColumnsType<DbQueryGrant> = [
    {
      title: '被授权人', key: 'user', width: 130,
      render: (_v, r) => (
        <Space size={4}>
          <Text strong>{r.display_name || r.username}</Text>
          {scope === 'all' && <Text type="secondary" style={{ fontSize: 11 }}>{r.username}</Text>}
        </Space>
      ),
    },
    { title: '实例', dataIndex: 'instance_name', width: 130, ellipsis: true },
    { title: '授权库', dataIndex: 'database', width: 120, render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text> },
    {
      title: '表范围', dataIndex: 'tables', width: 110,
      render: (v: string[]) => (v?.length ? <Tooltip title={v.join('、')}><Tag>{v.length} 张表</Tag></Tooltip> : <Tag>全部表</Tag>),
    },
    {
      title: '到期时间', dataIndex: 'expires_at', width: 200,
      render: (v: string, r) => {
        if (r.status !== 'active') return <Text type="secondary">{v}</Text>
        const remainHours = dayjs(v).diff(dayjs(), 'hour')
        // 常显剩余时间：临时授权的「还剩多久」比绝对时间更直观
        const remainText =
          remainHours >= 24
            ? `剩余 ${Math.floor(remainHours / 24)} 天${remainHours % 24 ? ` ${remainHours % 24} 小时` : ''}`
            : remainHours >= 1
              ? `剩余 ${remainHours} 小时`
              : `剩余 ${Math.max(0, dayjs(v).diff(dayjs(), 'minute'))} 分钟`
        return (
          <Space size={4}>
            <Text>{v}</Text>
            {remainHours > 0 && <Text type="secondary" style={{ fontSize: 12 }}>{remainText}</Text>}
            {remainHours > 0 && remainHours <= 2 && <Tag color="orange">即将到期</Tag>}
          </Space>
        )
      },
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v: string) => {
        const meta = GRANT_STATUS[v] || { color: 'default', label: v }
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '来源', key: 'source', width: 120,
      render: (_v, r) => (r.ticket_id ? <Tag color="geekblue">工单 #{r.ticket_id}</Tag> : <Tag>手动授权</Tag>),
    },
    { title: '授权人', dataIndex: 'granted_by', width: 100, render: (v: string) => v || '-' },
    {
      title: '操作', key: 'ops', width: 80,
      render: (_v, r) => (canManage && r.status === 'active' ? (
        <Button size="small" danger disabled={revokeMutation.isPending && revokeMutation.variables === r.id}
          onClick={() => confirmRevoke(r)}>
          吊销
        </Button>
      ) : null),
    },
  ]

  return (
    <Card size="small">
      <Flex justify="space-between" wrap gap="small" style={{ marginBottom: 12 }}>
        <Space>
          {canManage && (
            <Select
              value={scope} onChange={(v) => setScope(v)} style={{ width: 130 }}
              options={[
                { value: 'mine', label: '我的授权' },
                { value: 'all', label: '全部授权' },
              ]}
            />
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            查询授权由工单终审自动生成；管理员可手动授权或提前吊销，到期自动回收
          </Text>
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
          {canManage && (
            <Button type="primary" icon={<PlusOutlined />}
              onClick={() => { form.resetFields(); setGrantOpen(true) }}>
              手动授权
            </Button>
          )}
        </Space>
      </Flex>
      <Table
        rowKey="id" size="small" columns={columns} dataSource={grants} loading={isLoading}
        pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        locale={{ emptyText: <Empty description="暂无查询授权" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
      />

      <Drawer
        title="手动查询授权"
        open={grantOpen} onClose={() => setGrantOpen(false)} width={520} destroyOnClose
        extra={<Button type="primary" loading={createMutation.isPending} onClick={() => form.submit()}>授权</Button>}
      >
        <Form form={form} layout="vertical" onFinish={(values) => createMutation.mutate(values)}
          initialValues={{ hours: 4 }}>
          <Form.Item name="username" label="被授权人账号" rules={[{ required: true, message: '请输入账号' }]}
            extra="用户登录账号（非姓名）">
            <Input placeholder="zhangsan" />
          </Form.Item>
          <Form.Item name="instance_id" label="实例" rules={[{ required: true, message: '请选择实例' }]}>
            <Select
              showSearch optionFilterProp="label" placeholder="选择实例"
              onChange={() => form.setFieldsValue({ database: undefined, tables: [] })}
              options={instances.map((i) => ({ value: i.id, label: `${i.name}（${i.db_type} ${i.host}）` }))}
            />
          </Form.Item>
          <Form.Item name="database" label="目标库" rules={[{ required: true, message: '请选择库' }]}>
            <Select
              showSearch placeholder={watchInstanceId ? '选择库' : '先选实例'} disabled={!watchInstanceId}
              options={(dbList?.databases ?? []).map((d) => ({
                value: d, label: d === dbList?.default_database ? `${d}（默认）` : d,
              }))}
            />
          </Form.Item>
          <Form.Item name="tables" label="表范围（可选，留空 = 全部表）"
            extra="表范围作为审批与审计参考记录">
            <Select
              mode="multiple" allowClear showSearch placeholder="全部表" maxTagCount="responsive"
              options={tableList.map((t) => ({ value: t.name, label: t.name }))}
            />
          </Form.Item>
          <Form.Item name="hours" label="时长（小时）" rules={[{ required: true }]}>
            <InputNumber min={1} max={720} style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name="reason" label="授权理由">
            <Input.TextArea rows={2} placeholder="如：临时支持排查线上问题" />
          </Form.Item>
        </Form>
      </Drawer>
    </Card>
  )
}
