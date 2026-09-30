import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Checkbox,
  Tooltip, Flex, App, Typography, Popconfirm, Collapse, Badge,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, ReloadOutlined, EditOutlined, DeleteOutlined, SafetyOutlined,
} from '@ant-design/icons'
import { rbacApi } from '@/api'
import type { RoleTemplateItem, RoleTemplateCreateRequest, PermissionCatalogItem } from '@/types/api'

const { Text, Title } = Typography

export default function PermissionsPage() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  const [drawerOpen, setDrawerOpen]   = useState(false)
  const [editingRole, setEditingRole] = useState<RoleTemplateItem | null>(null)
  const [form]                        = Form.useForm<RoleTemplateCreateRequest>()
  const [checkedPerms, setCheckedPerms] = useState<Record<string, boolean>>({})

  // ── Queries ──
  const { data: meta, isLoading } = useQuery({
    queryKey: ['rbac-meta'],
    queryFn: rbacApi.metadata,
  })

  const roleTemplates: RoleTemplateItem[] = meta?.role_templates ?? []
  const permissions: PermissionCatalogItem[] = meta?.permissions ?? []

  // 按 group 分组权限
  const permGroups = permissions.reduce<Record<string, PermissionCatalogItem[]>>((acc, p) => {
    if (!acc[p.group]) acc[p.group] = []
    acc[p.group].push(p)
    return acc
  }, {})

  // ── Mutations ──
  const createMut = useMutation({
    mutationFn: (data: RoleTemplateCreateRequest) => rbacApi.createRole(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rbac-meta'] })
      setDrawerOpen(false)
      message.success('权限组已创建')
    },
    onError: () => message.error('创建失败，角色名可能已存在'),
  })

  const updateMut = useMutation({
    mutationFn: ({ role, data }: { role: string; data: { description?: string; permissions: Record<string, boolean> } }) =>
      rbacApi.updateRole(role, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rbac-meta'] })
      setDrawerOpen(false)
      message.success('权限组已更新')
    },
    onError: () => message.error('更新失败'),
  })

  const deleteMut = useMutation({
    mutationFn: (role: string) => rbacApi.deleteRole(role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rbac-meta'] })
      message.success('权限组已删除')
    },
    onError: () => message.error('删除失败，可能有用户正在使用该权限组'),
  })

  const openCreate = () => {
    setEditingRole(null)
    form.resetFields()
    setCheckedPerms({})
    setDrawerOpen(true)
  }

  const openEdit = (r: RoleTemplateItem) => {
    setEditingRole(r)
    form.setFieldsValue({ role: r.role, description: r.description })
    setCheckedPerms({ ...r.permissions })
    setDrawerOpen(true)
  }

  const handleSave = () => {
    form.validateFields().then(({ role, description }) => {
      if (editingRole) {
        updateMut.mutate({ role: editingRole.role, data: { description, permissions: checkedPerms } })
      } else {
        createMut.mutate({ role, description, permissions: checkedPerms })
      }
    })
  }

  const togglePerm = (key: string, checked: boolean) => {
    setCheckedPerms((prev) => ({ ...prev, [key]: checked }))
  }

  const toggleGroup = (keys: string[], checked: boolean) => {
    setCheckedPerms((prev) => {
      const next = { ...prev }
      keys.forEach((k) => { next[k] = checked })
      return next
    })
  }

  const columns: ColumnsType<RoleTemplateItem> = [
    {
      title: '权限组', dataIndex: 'role', width: 160,
      render: (v: string, r) => (
        <Space>
          <SafetyOutlined style={{ color: '#1677ff' }} />
          <Text strong>{r.title || v}</Text>
          {r.is_system && <Tag color="orange">系统</Tag>}
        </Space>
      ),
    },
    { title: '角色标识', dataIndex: 'role', width: 140 },
    { title: '描述', dataIndex: 'description', ellipsis: true, render: (v: string) => v || '-' },
    {
      title: '权限数', key: 'perm_count', width: 90,
      render: (_: unknown, r) => {
        const count = Object.values(r.permissions).filter(Boolean).length
        return <Badge count={count} showZero color="#1677ff" overflowCount={999} />
      },
    },
    {
      title: '操作', key: 'action', width: 120, fixed: 'right',
      render: (_: unknown, r) => (
        <Space size="small">
          <Tooltip title={r.editable ? '编辑' : '系统内置，只能查看'}>
            <Button size="small" type="primary" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          </Tooltip>
          {r.deletable && (
            <Popconfirm
              title="删除权限组"
              description="确定删除该权限组吗？正在使用的用户将无法访问。"
              onConfirm={() => deleteMut.mutate(r.role)}
            >
              <Tooltip title="删除">
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Tooltip>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" wrap gap={8}>
        <Title level={5} style={{ margin: 0 }}>权限组管理</Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ['rbac-meta'] })}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增权限组</Button>
        </Space>
      </Flex>

      <Table<RoleTemplateItem>
        rowKey="role"
        columns={columns}
        dataSource={roleTemplates}
        loading={isLoading}
        size="middle"
        pagination={false}
      />

      {/* 新增/编辑抽屉 */}
      <Drawer
        title={editingRole ? `编辑权限组 · ${editingRole.title || editingRole.role}` : '新增权限组'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={560}
        extra={
          <Button
            type="primary"
            loading={createMut.isPending || updateMut.isPending}
            onClick={handleSave}
            disabled={editingRole?.is_system && !editingRole?.editable}
          >
            保存
          </Button>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item name="role" label="角色标识（英文）" rules={[{ required: true, message: '请输入角色标识' }, { pattern: /^[a-zA-Z0-9_-]{2,32}$/, message: '2-32位字母数字下划线' }]}>
            <Input placeholder="例如 devops" disabled={!!editingRole} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="简要描述该权限组的职责" />
          </Form.Item>
        </Form>

        {/* 权限勾选 */}
        <div style={{ marginTop: 16 }}>
          <Text strong>权限配置</Text>
          <Collapse
            style={{ marginTop: 8 }}
            items={Object.entries(permGroups).map(([group, items]) => {
              const groupKeys = items.map((p) => p.key)
              const allChecked = groupKeys.every((k) => checkedPerms[k])
              const someChecked = groupKeys.some((k) => checkedPerms[k])
              return {
                key: group,
                label: (
                  <Flex align="center" gap={8}>
                    <Checkbox
                      checked={allChecked}
                      indeterminate={someChecked && !allChecked}
                      onChange={(e) => toggleGroup(groupKeys, e.target.checked)}
                      onClick={(e) => e.stopPropagation()}
                    />
                    <span>{group}</span>
                    <Badge count={groupKeys.filter((k) => checkedPerms[k]).length}
                      style={{ backgroundColor: '#52c41a' }} showZero={false} />
                  </Flex>
                ),
                children: (
                  <Flex vertical gap={6}>
                    {items.map((p) => (
                      <Checkbox
                        key={p.key}
                        checked={!!checkedPerms[p.key]}
                        onChange={(e) => togglePerm(p.key, e.target.checked)}
                        disabled={editingRole?.is_system && !editingRole?.editable}
                      >
                        <Tooltip title={p.description}>
                          <Text style={{ fontSize: 13 }}>{p.title}</Text>
                        </Tooltip>
                        <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>{p.key}</Text>
                      </Checkbox>
                    ))}
                  </Flex>
                ),
              }
            })}
          />
        </div>
      </Drawer>
    </Flex>
  )
}
