import { useState, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, App, Tooltip,
  Flex, Empty, Card, Statistic, Row, Col, Badge, Popconfirm, Steps, Divider,
  Transfer,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { TransferProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined,
  AppstoreOutlined, CheckCircleOutlined, ExclamationCircleOutlined,
  LinkOutlined, ArrowLeftOutlined, ArrowRightOutlined,
} from '@ant-design/icons'
import { applicationsApi, assetsApi, vmsApi } from '@/api'
import { useRiskConfirm } from '@/hooks/usePermission'
import GroupSidebar from '@/components/GroupSidebar'
import type { ApplicationResponse, ApplicationPayload } from '@/types/api'

const CATEGORY_OPTIONS = [
  { value: '业务应用', label: '业务应用' },
  { value: '基础设施', label: '基础设施' },
  { value: '数据服务', label: '数据服务' },
  { value: '中间件', label: '中间件' },
  { value: '监控运维', label: '监控运维' },
  { value: '其他', label: '其他' },
]

const ENV_OPTIONS = [
  { value: 'prod', label: '生产' },
  { value: 'test', label: '测试' },
  { value: 'dev', label: '开发' },
  { value: 'mixed', label: '混合' },
]

const STATUS_OPTIONS = [
  { value: 'active', label: '运行中' },
  { value: 'maintenance', label: '维护中' },
  { value: 'offline', label: '下线' },
]

const ENV_COLOR: Record<string, string> = {
  prod: 'red', test: 'blue', dev: 'default', mixed: 'purple',
}
const ENV_LABEL: Record<string, string> = {
  prod: '生产', test: '测试', dev: '开发', mixed: '混合',
}

const HEALTH_COLOR: Record<string, 'success' | 'warning' | 'error' | 'default'> = {
  healthy: 'success', degraded: 'warning', unhealthy: 'error', unknown: 'default', unmanaged: 'default',
}

export default function AppListPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { confirm } = useRiskConfirm()
  const { message } = App.useApp()

  const [search, setSearch]               = useState('')
  const [catFilter, setCatFilter]         = useState<string | undefined>()
  const [envFilter, setEnvFilter]         = useState<string | undefined>()
  const [statusFilter, setStatusFilter]   = useState<string | undefined>()
  const [groupFilter, setGroupFilter]     = useState<number | null>(null)
  const [selectedKeys, setSelectedKeys]   = useState<string[]>([])
  const [drawerOpen, setDrawerOpen]       = useState(false)
  const [editingId, setEditingId]         = useState<string | null>(null)
  const [createStep, setCreateStep]       = useState(0)   // 0=基本信息 1=绑定资源
  const [resTargetKeys, setResTargetKeys] = useState<string[]>([])  // 已选资源 key 列表
  const [form]                            = Form.useForm<ApplicationPayload>()

  const { data: apps = [], isLoading } = useQuery({
    queryKey: ['applications'],
    queryFn: () => applicationsApi.list(),
  })

  // 资源池：主机 + 虚机
  const { data: hosts = [] } = useQuery({
    queryKey: ['assets'],
    queryFn: () => assetsApi.list(),
  })
  const { data: vms = [] } = useQuery({
    queryKey: ['vms'],
    queryFn: () => vmsApi.list(),
  })

  const createMut = useMutation({
    mutationFn: (data: ApplicationPayload) => applicationsApi.create(data),
    onSuccess: async (newApp) => {
      // 如果第2步选了资源，先绑定再提示
      if (resTargetKeys.length > 0) {
        const resources = resTargetKeys.map((key) => {
          const [type, id] = key.split(':')
          return { resource_type: type, resource_id: id }
        })
        try {
          await applicationsApi.bindResources(newApp.app_id, resources)
        } catch {
          message.warning('应用已创建，但资源绑定失败')
        }
      }
      queryClient.invalidateQueries({ queryKey: ['applications'] })
      message.success('应用已创建')
      setDrawerOpen(false)
      setCreateStep(0)
      setResTargetKeys([])
    },
    onError: () => message.error('创建失败'),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ApplicationPayload }) => applicationsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['applications'] })
      message.success('应用已更新')
      setDrawerOpen(false)
    },
    onError: () => message.error('更新失败'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => applicationsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['applications'] })
      message.success('应用已删除')
    },
    onError: () => message.error('删除失败'),
  })

  // ── 统计 ──
  const totalCount     = apps.length
  const activeCount    = apps.filter((a) => a.status === 'active').length
  const healthyCount   = apps.filter((a) => a.health_status === 'healthy').length
  const unhealthyCount = apps.filter((a) => a.health_status === 'unhealthy' || a.health_status === 'degraded').length

  // ── 筛选 ──
  const filtered = useMemo(() => apps.filter((a) => {
    const matchSearch = !search || a.name.includes(search) || a.owner?.includes(search) || a.description?.includes(search)
    const matchCat    = !catFilter || a.category === catFilter
    const matchEnv    = !envFilter || a.environment === envFilter
    const matchStatus = !statusFilter || a.status === statusFilter
    return matchSearch && matchCat && matchEnv && matchStatus
  }), [apps, search, catFilter, envFilter, statusFilter])

  const openDrawer = useCallback((app?: ApplicationResponse) => {
    if (app) {
      setEditingId(app.app_id)
      setCreateStep(0)
      form.setFieldsValue({
        name: app.name,
        description: app.description,
        category: app.category,
        owner: app.owner,
        status: app.status,
        environment: app.environment,
        entry_url: app.entry_url,
        remark: app.remark,
      })
    } else {
      setEditingId(null)
      setCreateStep(0)
      setResTargetKeys([])
      form.resetFields()
      form.setFieldsValue({ category: '业务应用', status: 'active', environment: 'prod' })
    }
    setDrawerOpen(true)
  }, [form])

  // 编辑：直接提交；新建第1步：下一步；新建第2步：提交
  const handleNext = () => {
    form.validateFields().then(() => setCreateStep(1))
  }

  const handleSave = () => {
    form.validateFields().then((values) => {
      if (editingId) {
        updateMut.mutate({ id: editingId, data: values })
      } else {
        createMut.mutate(values)
      }
    })
  }

  // Transfer 数据源：主机 + 虚机合并，key = "type:id"
  const resourceDataSource: TransferProps['dataSource'] = useMemo(() => [
    ...hosts.map((h) => ({ key: `host:${h.id}`, title: `${h.name}（${h.ip}）`, description: '主机' })),
    ...vms.map((v) => ({ key: `vm:${v.id}`, title: `${v.name}（${v.ip || '-'}）`, description: '虚机' })),
  ], [hosts, vms])

  const columns: ColumnsType<ApplicationResponse> = [
    {
      title: '应用名称', dataIndex: 'name', width: 200, fixed: 'left',
      render: (v: string, r) => (
        <Flex align="center" gap={8}>
          <a onClick={() => navigate(`/apps/${r.app_id}`)} style={{ fontWeight: 500 }}>{v}</a>
          {r.entry_url && (
            <Tooltip title={r.entry_url}>
              <a href={r.entry_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                <LinkOutlined style={{ color: '#8c8c8c', fontSize: 12 }} />
              </a>
            </Tooltip>
          )}
        </Flex>
      ),
    },
    {
      title: '健康状态', key: 'health', width: 110,
      render: (_: unknown, r) => (
        <Badge
          status={HEALTH_COLOR[r.health_status]}
          text={<span style={{ fontSize: 13 }}>{r.health_label || '未知'}</span>}
        />
      ),
    },
    {
      title: '环境', dataIndex: 'environment', width: 80,
      render: (v: string) => <Tag color={ENV_COLOR[v] || 'default'}>{ENV_LABEL[v] || v}</Tag>,
    },
    {
      title: '分类', dataIndex: 'category', width: 100,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '负责人', dataIndex: 'owner', width: 100,
      render: (v: string) => v || '-',
    },
    {
      title: '资源', key: 'resources', width: 160,
      render: (_: unknown, r) => (
        <Space size={12}>
          <Tooltip title="关联资源数">
            <span style={{ color: '#64748b', fontSize: 13 }}>资源 {r.resource_count}</span>
          </Tooltip>
          <Tooltip title="运行实例数">
            <span style={{ color: '#64748b', fontSize: 13 }}>实例 {r.component_count}</span>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v: string) => {
        const map: Record<string, { text: string; color: 'success' | 'default' | 'warning' }> = {
          active: { text: '运行中', color: 'success' },
          inactive: { text: '停用', color: 'default' },
          maintenance: { text: '维护中', color: 'warning' },
        }
        const s = map[v] || { text: v, color: 'default' }
        return <Badge status={s.color} text={s.text} />
      },
    },
    {
      title: '创建时间', dataIndex: 'created_at', width: 160,
      render: (v: string) => v ? v.slice(0, 16) : '-',
    },
    {
      title: '操作', key: 'action', width: 100, fixed: 'right', align: 'right',
      render: (_: unknown, r) => (
        <Space size={4}>
          <Tooltip title="编辑">
            <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openDrawer(r)} />
          </Tooltip>
          <Popconfirm
            title={`删除应用「${r.name}」？`}
            description="此操作不可恢复"
            okText="删除" okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => confirm('apps:delete', () => deleteMut.mutate(r.app_id))}
          >
            <Tooltip title="删除">
              <Button size="small" type="text" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      <Flex gap={16} style={{ flex: 1, minWidth: 0 }}>
        <GroupSidebar
          resourceType="app"
          selectedGroupId={groupFilter}
          onSelect={setGroupFilter}
          selectedIds={selectedKeys}
        />

        <Flex vertical gap={12} style={{ flex: 1, minWidth: 0 }}>
          {/* 顶部概览统计卡片 */}
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>应用总数</span>}
                  value={totalCount}
                  prefix={<AppstoreOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20 }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>运行中</span>}
                  value={activeCount}
                  prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>健康</span>}
                  value={healthyCount}
                  prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20 }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>异常</span>}
                  value={unhealthyCount}
                  prefix={<ExclamationCircleOutlined style={{ color: '#ff4d4f', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20, color: unhealthyCount > 0 ? '#ff4d4f' : 'inherit' }}
                />
              </Card>
            </Col>
          </Row>

          {/* 检索与操作栏 */}
          <Flex justify="space-between" wrap gap={8}>
            <Space wrap>
              <Input
                prefix={<SearchOutlined />}
                placeholder="搜索名称、负责人或描述"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                allowClear
                style={{ width: 220 }}
              />
              <Select
                allowClear
                placeholder="分类"
                value={catFilter}
                onChange={(v) => setCatFilter(v)}
                options={CATEGORY_OPTIONS}
                style={{ width: 120 }}
              />
              <Select
                allowClear
                placeholder="环境"
                value={envFilter}
                onChange={(v) => setEnvFilter(v)}
                options={ENV_OPTIONS}
                style={{ width: 110 }}
              />
              <Select
                allowClear
                placeholder="状态"
                value={statusFilter}
                onChange={(v) => setStatusFilter(v)}
                options={STATUS_OPTIONS}
                style={{ width: 110 }}
              />
            </Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>
              新增应用
            </Button>
          </Flex>

          {/* 主表格 */}
          <Table<ApplicationResponse>
            rowKey="app_id"
            rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as string[]) }}
            columns={columns}
            dataSource={filtered}
            loading={isLoading}
            size="middle"
            scroll={{ x: 1100 }}
            locale={{
              emptyText: (
                <Empty
                  image={<AppstoreOutlined style={{ fontSize: 48, color: '#cbd5e1' }} />}
                  styles={{ image: { height: 60 } }}
                  description={
                    <span style={{ color: '#8c8c8c' }}>
                      {apps.length === 0 ? '还没有注册任何应用' : '没有符合条件的应用'}
                    </span>
                  }
                >
                  {apps.length === 0 && (
                    <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>
                      注册第一个应用
                    </Button>
                  )}
                </Empty>
              ),
            }}
            pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个应用` }}
          />

          {/* 新增/编辑抽屉 */}
          <Drawer
            title={editingId ? '编辑应用' : '新增应用'}
            open={drawerOpen}
            onClose={() => { setDrawerOpen(false); setCreateStep(0); setResTargetKeys([]) }}
            width={600}
            footer={
              <Flex justify="space-between">
                {/* 新建第2步时显示「上一步」 */}
                {!editingId && createStep === 1 ? (
                  <Button icon={<ArrowLeftOutlined />} onClick={() => setCreateStep(0)}>上一步</Button>
                ) : <span />}
                <Space>
                  <Button onClick={() => { setDrawerOpen(false); setCreateStep(0); setResTargetKeys([]) }}>取消</Button>
                  {!editingId && createStep === 0 ? (
                    <Button type="primary" icon={<ArrowRightOutlined />} onClick={handleNext}>下一步</Button>
                  ) : (
                    <Button type="primary" onClick={handleSave} loading={createMut.isPending || updateMut.isPending}>
                      {!editingId && resTargetKeys.length > 0 ? `保存并绑定 ${resTargetKeys.length} 个资源` : '保存'}
                    </Button>
                  )}
                </Space>
              </Flex>
            }
          >
            {/* 新增时显示步骤条；编辑时不显示 */}
            {!editingId && (
              <Steps
                current={createStep}
                size="small"
                style={{ marginBottom: 24 }}
                items={[
                  { title: '基本信息', description: '名称、分类、环境' },
                  { title: '绑定资源', description: '关联主机、虚机（可跳过）' },
                ]}
              />
            )}

            {/* 步骤1 / 编辑态：基本信息表单 */}
            <div style={{ display: !editingId && createStep === 1 ? 'none' : 'block' }}>
              <Form form={form} layout="vertical">
                <Form.Item name="name" label="应用名称" rules={[{ required: true }]}>
                  <Input placeholder="例如 订单服务" />
                </Form.Item>
                <Form.Item name="description" label="描述">
                  <Input.TextArea rows={2} placeholder="简要描述应用功能" />
                </Form.Item>
                <Flex gap={16}>
                  <Form.Item name="category" label="分类" style={{ flex: 1 }} initialValue="业务应用">
                    <Select options={CATEGORY_OPTIONS} />
                  </Form.Item>
                  <Form.Item name="environment" label="环境" style={{ flex: 1 }} initialValue="prod">
                    <Select options={ENV_OPTIONS} />
                  </Form.Item>
                </Flex>
                <Flex gap={16}>
                  <Form.Item name="status" label="状态" style={{ flex: 1 }} initialValue="active">
                    <Select options={STATUS_OPTIONS} />
                  </Form.Item>
                  <Form.Item name="owner" label="负责人" style={{ flex: 1 }}>
                    <Input placeholder="负责人姓名" />
                  </Form.Item>
                </Flex>
              <Form.Item
                name="entry_url"
                label="访问地址"
                rules={[{
                  validator: (_, v) => {
                    if (!v || v === '') return Promise.resolve()
                    if (v.startsWith('/') || v.startsWith('http://') || v.startsWith('https://'))
                      return Promise.resolve()
                    return Promise.reject(new Error('请输入 http(s):// 开头的完整地址或 / 开头的站内路径'))
                  },
                }]}
              >
                  <Input placeholder="https://example.com" />
                </Form.Item>
                <Form.Item name="remark" label="备注">
                  <Input.TextArea rows={2} />
                </Form.Item>
              </Form>
            </div>

            {/* 步骤2：绑定资源（仅新建显示） */}
            {!editingId && createStep === 1 && (
              <div>
                <div style={{ marginBottom: 12, color: '#52525b', fontSize: 13 }}>
                  从现有主机和虚机中选择要关联到此应用的资源，也可以跳过，后续在应用详情页绑定。
                </div>
                <Divider style={{ margin: '8px 0 16px' }} />
                <Transfer
                  dataSource={resourceDataSource}
                  targetKeys={resTargetKeys}
                  onChange={(keys) => setResTargetKeys(keys as string[])}
                  render={(item) => (
                    <Flex justify="space-between" style={{ width: '100%', paddingRight: 8 }}>
                      <span>{item.title}</span>
                      <Tag color={item.description === '主机' ? 'geekblue' : 'purple'} style={{ margin: 0 }}>
                        {item.description}
                      </Tag>
                    </Flex>
                  )}
                  showSearch
                  filterOption={(input, item) => item.title?.toLowerCase().includes(input.toLowerCase()) ?? false}
                  titles={['全部资源', '已选资源']}
                  listStyle={{ width: '100%', height: 320 }}
                  style={{ width: '100%' }}
                />
              </div>
            )}
          </Drawer>
        </Flex>
      </Flex>
    </Flex>
  )
}
