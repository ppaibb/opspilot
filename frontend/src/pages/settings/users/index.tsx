import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, Switch,
  Tooltip, Flex, App, Badge, Typography, Divider,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, ReloadOutlined, SearchOutlined,
  EditOutlined, LockOutlined, UserOutlined,
} from '@ant-design/icons'
import { usersApi, rbacApi } from '@/api'
import { useRiskConfirm } from '@/hooks/usePermission'
import type { UserResponse, UserCreateRequest, UserAccessUpdate } from '@/types/api'

const { Text } = Typography

const ROLE_COLOR: Record<string, string> = {
  管理员: 'red',
  运维: 'blue',
  开发: 'green',
  只读: 'default',
}

const STATUS_MAP: Record<string, { color: 'success' | 'error' | 'default'; text: string }> = {
  active:   { color: 'success', text: '正常' },
  inactive: { color: 'error',   text: '已禁用' },
  locked:   { color: 'default', text: '已锁定' },
}

export default function UsersPage() {
  const queryClient = useQueryClient()
  const { confirm } = useRiskConfirm()
  const { message, modal } = App.useApp()

  const [search, setSearch]         = useState('')
  const [drawerMode, setDrawerMode] = useState<'create' | 'role' | 'password' | null>(null)
  const [editingUser, setEditingUser] = useState<UserResponse | null>(null)
  const [createForm]  = Form.useForm<UserCreateRequest>()
  const [roleForm]    = Form.useForm<{ role: string }>()
  const [pwdForm]     = Form.useForm<{ new_password: string; confirm_password: string }>()

  // ── Queries ──
  const { data: users = [], isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: usersApi.list,
  })

  const { data: rbacMeta } = useQuery({
    queryKey: ['rbac-meta'],
    queryFn: rbacApi.metadata,
  })

  const roleOptions = rbacMeta?.role_templates?.map((r) => ({ value: r.role, label: r.title ?? r.role })) ?? []

  // ── Mutations ──
  const createMut = useMutation({
    mutationFn: (data: UserCreateRequest) => usersApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setDrawerMode(null)
      message.success('用户创建成功')
    },
    onError: () => message.error('创建失败，用户名可能已存在'),
  })

  const accessMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: UserAccessUpdate }) => usersApi.updateAccess(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setDrawerMode(null)
      message.success('角色已更新')
    },
    onError: () => message.error('更新失败'),
  })

  const statusMut = useMutation({
    mutationFn: ({ id, status }: { id: number; status: string }) => usersApi.updateStatus(id, status),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      message.success('状态已更新')
    },
    onError: () => message.error('操作失败'),
  })

  const resetPwdMut = useMutation({
    mutationFn: ({ id, pwd }: { id: number; pwd: string }) => usersApi.resetPassword(id, pwd),
    onSuccess: () => {
      setDrawerMode(null)
      message.success('密码已重置')
    },
    onError: () => message.error('重置失败'),
  })

  const filtered = users.filter((u) =>
    !search ||
    u.username.includes(search) ||
    u.name?.includes(search) ||
    u.email?.includes(search) ||
    u.department?.includes(search),
  )

  // ── 打开抽屉 ──
  const openCreate = () => {
    createForm.resetFields()
    setDrawerMode('create')
  }

  const openRole = (u: UserResponse) => {
    setEditingUser(u)
    roleForm.setFieldsValue({ role: u.role })
    setDrawerMode('role')
  }

  const openResetPwd = (u: UserResponse) => {
    setEditingUser(u)
    pwdForm.resetFields()
    setDrawerMode('password')
  }

  const handleToggleStatus = (u: UserResponse) => {
    const next = u.status === 'active' ? 'inactive' : 'active'
    if (next === 'inactive') {
      // 禁用是后端守卫动作（users:disable）：riskConfirm 取一次性票据
      confirm('users:disable', () => statusMut.mutate({ id: u.id, status: next }), `确定要禁用用户「${u.username}」吗？`)
    } else {
      // 启用无守卫：普通二次确认即可，不取风险票据
      modal.confirm({
        title: '启用用户',
        content: `确定要启用用户「${u.username}」吗？`,
        okText: '启用',
        cancelText: '取消',
        onOk: () => statusMut.mutate({ id: u.id, status: next }),
      })
    }
  }

  const columns: ColumnsType<UserResponse> = [
    {
      title: '用户名', dataIndex: 'username', width: 140, fixed: 'left',
      render: (v: string, r) => (
        <Space>
          <UserOutlined style={{ color: '#8c8c8c' }} />
          <a onClick={() => openRole(r)}>{v}</a>
        </Space>
      ),
    },
    { title: '姓名', dataIndex: 'name', width: 110, render: (v: string) => v || '-' },
    { title: '邮箱', dataIndex: 'email', width: 200, render: (v: string) => v || '-' },
    { title: '部门', dataIndex: 'department', width: 120, render: (v: string) => v || '-' },
    {
      title: '角色', dataIndex: 'role', width: 100,
      render: (v: string) => <Tag color={ROLE_COLOR[v] ?? 'blue'}>{v}</Tag>,
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string) => {
        const m = STATUS_MAP[s] ?? STATUS_MAP.active
        return <Badge status={m.color} text={m.text} />
      },
    },
    {
      title: '权限数', dataIndex: 'permission_count', width: 80,
      render: (v: number) => <Text type="secondary">{v}</Text>,
    },
    {
      title: '最后登录', dataIndex: 'last_login', width: 170,
      render: (v: string) => v || '从未登录',
    },
    {
      title: '操作', key: 'action', width: 180, fixed: 'right',
      render: (_: unknown, r) => (
        <Space size="small">
          <Tooltip title="修改角色/权限">
            <Button size="small" type="primary" icon={<EditOutlined />} onClick={() => openRole(r)} />
          </Tooltip>
          <Tooltip title="重置密码">
            <Button size="small" icon={<LockOutlined />} onClick={() => openResetPwd(r)} />
          </Tooltip>
          <Tooltip title={r.status === 'active' ? '禁用' : '启用'}>
            <Switch
              size="small"
              checked={r.status === 'active'}
              onChange={() => handleToggleStatus(r)}
              loading={statusMut.isPending}
            />
          </Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" wrap gap={8}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索用户名、姓名、邮箱、部门"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear style={{ width: 300 }}
        />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ['users'] })}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增用户
          </Button>
        </Space>
      </Flex>

      <Table<UserResponse>
        rowKey="id"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        size="middle"
        scroll={{ x: 1100 }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个用户` }}
      />

      {/* ── 新增用户抽屉 ── */}
      <Drawer
        title="新增用户"
        open={drawerMode === 'create'}
        onClose={() => setDrawerMode(null)}
        width={480}
        extra={<Button type="primary" loading={createMut.isPending} onClick={() => createForm.validateFields().then((v) => createMut.mutate(v))}>创建</Button>}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }, { pattern: /^[a-zA-Z0-9_-]{3,32}$/, message: '3-32位字母数字下划线' }]}>
            <Input prefix={<UserOutlined />} placeholder="例如 zhangsan" />
          </Form.Item>
          <Form.Item name="password" label="初始密码" rules={[{ required: true, message: '请设置初始密码' }, { min: 8, message: '至少 8 位' }]}>
            <Input.Password placeholder="至少 8 位" />
          </Form.Item>
          <Flex gap={16}>
            <Form.Item name="name" label="姓名" style={{ flex: 1 }}>
              <Input placeholder="真实姓名" />
            </Form.Item>
            <Form.Item name="department" label="部门" style={{ flex: 1 }}>
              <Input placeholder="例如 运维部" />
            </Form.Item>
          </Flex>
          <Form.Item name="email" label="邮箱" rules={[{ type: 'email', message: '请输入有效邮箱' }]}>
            <Input placeholder="zhangsan@example.com" />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="只读">
            <Select options={roleOptions} />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="active">
            <Select options={[{ value: 'active', label: '正常' }, { value: 'inactive', label: '禁用' }]} />
          </Form.Item>
        </Form>
      </Drawer>

      {/* ── 修改角色抽屉 ── */}
      <Drawer
        title={`修改角色 · ${editingUser?.username}`}
        open={drawerMode === 'role'}
        onClose={() => setDrawerMode(null)}
        width={480}
        extra={
          <Button type="primary" loading={accessMut.isPending} onClick={() => {
            roleForm.validateFields().then(({ role }) => {
              if (!editingUser) return
              accessMut.mutate({ id: editingUser.id, data: { role, effective_permissions: {} } })
            })
          }}>保存</Button>
        }
      >
        <Form form={roleForm} layout="vertical">
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={roleOptions} placeholder="选择角色" />
          </Form.Item>
          {editingUser && (
            <div style={{ marginTop: 8 }}>
              <Divider orientation="left" style={{ fontSize: 13 }}>当前权限摘要</Divider>
              <Flex gap={6} wrap>
                {(editingUser.access_summary ?? []).slice(0, 10).map((s, i) => (
                  <Tag key={i} style={{ fontSize: 12 }}>{s}</Tag>
                ))}
                {(editingUser.access_summary?.length ?? 0) > 10 && (
                  <Tag>+{(editingUser.access_summary?.length ?? 0) - 10} 项</Tag>
                )}
              </Flex>
            </div>
          )}
        </Form>
      </Drawer>

      {/* ── 重置密码抽屉 ── */}
      <Drawer
        title={`重置密码 · ${editingUser?.username}`}
        open={drawerMode === 'password'}
        onClose={() => setDrawerMode(null)}
        width={400}
        extra={
          <Button type="primary" danger loading={resetPwdMut.isPending} onClick={() => {
            pwdForm.validateFields().then(({ new_password }) => {
              if (!editingUser) return
              confirm('users:reset-password', () => resetPwdMut.mutate({ id: editingUser.id, pwd: new_password }), `确定重置「${editingUser.username}」的密码？`)
            })
          }}>重置</Button>
        }
      >
        <Form form={pwdForm} layout="vertical">
          <Form.Item name="new_password" label="新密码" rules={[{ required: true }, { min: 8, message: '至少 8 位' }]}>
            <Input.Password placeholder="至少 8 位" />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="确认密码"
            dependencies={['new_password']}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                  return Promise.reject(new Error('两次密码不一致'))
                },
              }),
            ]}
          >
            <Input.Password placeholder="再次输入新密码" />
          </Form.Item>
        </Form>
      </Drawer>
    </Flex>
  )
}
