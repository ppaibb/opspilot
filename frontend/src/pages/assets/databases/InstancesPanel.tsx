// ───────────────────────── 数据源 Tab ─────────────────────────
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Form, Input, InputNumber, Select,
  App, Tooltip, Popconfirm, Empty, Drawer, Badge,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, ReloadOutlined, DatabaseOutlined, DeleteOutlined, ApiOutlined,
} from '@ant-design/icons'
import { dbApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { DbInstance } from '@/types/api'

const { Text } = Typography

export function InstancesTab({ instances, loading, refetch }: { instances: DbInstance[]; loading: boolean; refetch: () => void }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.db.manage')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<DbInstance | null>(null)
  const [form] = Form.useForm()
  // 授权角色下拉：从审批流接口带出的角色模板列表（系统预设 + 自定义），不再硬编码。
  const { data: roleOptions = [] } = useQuery({
    queryKey: ['db-approval-flows'],
    queryFn: async () => {
      const flows = await dbApi.getApprovalFlows()
      return (flows.roles || []).map((r) => ({ value: r, label: r }))
    },
    staleTime: 5 * 60 * 1000,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['db-instances'] })
    refetch()
  }

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      editing ? dbApi.updateInstance(editing.id, values) : dbApi.createInstance(values),
    onSuccess: () => {
      message.success(editing ? '实例已更新' : '实例已创建（已自动连通测试）')
      setDrawerOpen(false)
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => dbApi.deleteInstance(id),
    onSuccess: () => { message.success('实例已删除'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const testMutation = useMutation({
    mutationFn: (id: number) => dbApi.testInstance(id),
    onSuccess: (result) => {
      if (result.ok) message.success(result.message)
      else message.warning(result.message)
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '测试失败'),
  })

  const openEdit = (record: DbInstance) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name, db_type: record.db_type, host: record.host, port: record.port,
      database: record.database, username: record.username, password: '', remark: record.remark,
      allowed_roles: record.allowed_roles || [],
    })
    setDrawerOpen(true)
  }

  const columns: ColumnsType<DbInstance> = [
    {
      title: '实例', dataIndex: 'name', width: 180,
      render: (v: string, record) => (
        <Space size={6}>
          <DatabaseOutlined style={{ color: '#1677ff' }} />
          <Text strong>{v}</Text>
          <Tag>{record.db_type}</Tag>
        </Space>
      ),
    },
    { title: '地址', width: 220, render: (_v, r) => <Text code>{r.host}:{r.port}{r.database ? `/${r.database}` : ''}</Text> },
    { title: '用户', dataIndex: 'username', width: 110 },
    {
      title: '连通', dataIndex: 'status', width: 110,
      render: (v: string, record) => (
        <Tooltip title={record.status_message}>
          <Badge status={v === 'ok' ? 'success' : v === 'error' ? 'error' : 'default'}
            text={v === 'ok' ? '正常' : v === 'error' ? '异常' : '未测试'} />
        </Tooltip>
      ),
    },
    { title: '备注', dataIndex: 'remark', ellipsis: true, render: (v: string) => v || '-' },
    {
      title: '授权', dataIndex: 'allowed_roles', width: 110,
      render: (v: string[] | undefined, record) => {
        const roles = v || record.allowed_roles
        return !roles || roles.length === 0
          ? <Tag color="green">全员</Tag>
          : <Tooltip title={roles.join('、')}><Tag color="blue">{roles.length} 组</Tag></Tooltip>
      },
    },
    {
      title: '操作', key: 'ops', width: 180, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button size="small" icon={<ApiOutlined />} disabled={!canManage}
            loading={testMutation.isPending && testMutation.variables === record.id}
            onClick={() => testMutation.mutate(record.id)}>
            测试
          </Button>
          <Button size="small" disabled={!canManage} onClick={() => openEdit(record)}>编辑</Button>
          <Popconfirm
            title={`删除实例 ${record.name}？`} description="有工单记录的实例不可删除"
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => deleteMutation.mutate(record.id)} disabled={!canManage}
          >
            <Button size="small" danger icon={<DeleteOutlined />} disabled={!canManage} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card size="small">
      <Flex justify="flex-end" style={{ marginBottom: 12 }}>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canManage}
            onClick={() => { setEditing(null); form.resetFields(); setDrawerOpen(true) }}>
            新增实例
          </Button>
        </Space>
      </Flex>
      <Table<DbInstance>
        rowKey="id" size="small" columns={columns} dataSource={instances} loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无数据源" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
      />

      <Drawer
        title={editing ? `编辑实例 - ${editing.name}` : '新增实例'}
        open={drawerOpen} onClose={() => setDrawerOpen(false)} width={520} destroyOnClose
        extra={<Button type="primary" loading={saveMutation.isPending} onClick={() => form.submit()}>保存</Button>}
      >
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}
          initialValues={{ db_type: 'mysql', port: 3306 }}>
          <Form.Item name="name" label="实例名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="生产订单库" />
          </Form.Item>
          <Form.Item name="db_type" label="类型" rules={[{ required: true }]}>
            <Select options={[
              { value: 'mysql', label: 'MySQL' },
              { value: 'postgresql', label: 'PostgreSQL' },
            ]} />
          </Form.Item>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="host" label="主机" rules={[{ required: true, message: '必填' }]} style={{ flex: 1 }}>
              <Input placeholder="10.0.0.5" />
            </Form.Item>
            <Form.Item name="port" label="端口" rules={[{ required: true }]}>
              <InputNumber min={1} max={65535} style={{ width: 100 }} />
            </Form.Item>
          </Space>
          <Form.Item name="database" label="库名" extra="PostgreSQL 必填；MySQL 可留空后 USE">
            <Input placeholder="orders" />
          </Form.Item>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="username" label="用户" rules={[{ required: true, message: '必填' }]} style={{ flex: 1 }}>
              <Input placeholder="readonly / deployer" />
            </Form.Item>
            <Form.Item name="password" label="密码" extra={editing?.has_password ? '留空保持不变' : ''}
              style={{ flex: 1 }}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </Space>
          <Form.Item name="remark" label="备注">
            <Input placeholder="负责人/用途" />
          </Form.Item>
          <Form.Item name="allowed_roles" label="授权权限组（使用范围）" valuePropName="value"
            extra="空=所有具备 SQL 权限的角色可用（工单/自助查询目标）；指定后仅授权角色可见可选">
            <Select
              mode="multiple" allowClear placeholder="全部角色可用"
              options={roleOptions}
            />
          </Form.Item>
        </Form>
      </Drawer>
    </Card>
  )
}
