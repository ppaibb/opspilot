import { useState, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Drawer, Form, Select, Card, Statistic, Row, Col,
  InputNumber, Tooltip, Badge, Flex, App, Modal, Switch, Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { TableRowSelection } from 'antd/es/table/interface'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined,
  DeleteOutlined, EditOutlined,
  PoweroffOutlined, SyncOutlined, CopyOutlined,
  HddOutlined, CheckCircleOutlined, StopOutlined, ThunderboltOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import { vmsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import { useNotificationStore } from '@/store/notifications'
import { useRiskConfirm } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import type { VMResponse, VMCreate, VMBatchDeleteResult } from '@/types/api'

const STATUS_MAP: Record<string, { color: string; text: string }> = {
  running: { color: 'success',    text: '运行中' },
  stopped: { color: 'default',    text: '已停止' },
  paused:  { color: 'warning',    text: '已暂停' },
  pending: { color: 'processing', text: '启动中' },
  error:   { color: 'error',      text: '异常' },
  unknown: { color: 'default',    text: '未知' },
}

export default function VmsPage() {
  const queryClient = useQueryClient()
  const { confirm } = useRiskConfirm()
  const { message, modal } = App.useApp()
  const pushLocal = useNotificationStore((s) => s.pushLocal)

  const [search, setSearch] = useState('')
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [form] = Form.useForm<VMCreate>()

  // 克隆 Modal 状态
  const [cloneModalOpen, setCloneModalOpen] = useState(false)
  const [cloneSourceVm, setCloneSourceVm] = useState<VMResponse | null>(null)
  const [cloneForm] = Form.useForm<{ name: string }>()

  // ── Queries ──
  const { data: vms = [], isLoading } = useQuery({ queryKey: ['vms'], queryFn: () => vmsApi.list() })

  // PVE 创建资源下拉：仅在「新增 + Proxmox + 抽屉打开」时拉取
  const providerWatch = Form.useWatch('provider', form)
  const nodeWatch = Form.useWatch('node', form) || ''
  const pveCreate = drawerOpen && !editingId && providerWatch === 'proxmox'
  const { data: pveNodes = [] } = useQuery({ queryKey: ['vms-nodes'], queryFn: () => vmsApi.getNodes(), enabled: pveCreate })
  const { data: pveStorages = [] } = useQuery({ queryKey: ['vms-storages', nodeWatch], queryFn: () => vmsApi.getStorages(nodeWatch), enabled: pveCreate })
  const { data: pveIsos = [] } = useQuery({ queryKey: ['vms-isos', nodeWatch], queryFn: () => vmsApi.getIsos(nodeWatch), enabled: pveCreate })
  const { data: pveBridges = [] } = useQuery({ queryKey: ['vms-bridges', nodeWatch], queryFn: () => vmsApi.getBridges(nodeWatch), enabled: pveCreate })
  const { data: pveTemplates = [] } = useQuery({ queryKey: ['vms-templates'], queryFn: () => vmsApi.getTemplates(), enabled: pveCreate })

  // ── Mutations ──
  const { saveMut, deleteMut } = useCrudResource<VMCreate, string>({
    queryKey: ['vms'],
    entityName: '虚拟机',
    createFn: vmsApi.create,
    updateFn: vmsApi.update,
    deleteFn: vmsApi.delete,
    onSaved: () => setDrawerOpen(false),
  })

  const powerMut = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) => vmsApi.power(id, action),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['vms'] }),
  })

  const cloneMut = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => vmsApi.clone(id, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['vms'] })
      setCloneModalOpen(false)
      cloneForm.resetFields()
      message.success('已触发虚拟机克隆任务')
    },
    onError: () => message.error('虚拟机克隆失败'),
  })

  // ── 统计指标 ──
  const totalCount = vms.length
  const runningCount = vms.filter((v) => v.status === 'running').length
  const stoppedCount = vms.filter((v) => v.status === 'stopped').length
  const totalCpuCores = vms.reduce((acc, v) => acc + (v.cpu_cores || 0), 0)

  // ── 筛选 ──
  const filtered = vms.filter((v) => {
    return !search || v.name.includes(search) || (v.ip && v.ip.includes(search))
  })

  // ── 打开抽屉 ──
  const openDrawer = useCallback((vm?: VMResponse) => {
    if (vm) {
      setEditingId(vm.id)
      form.setFieldsValue({
        name: vm.name,
        provider: vm.provider,
        node: vm.node,
        ip: vm.ip,
        os: vm.os,
        cpu_cores: vm.cpu_cores,
        memory_gb: vm.memory_gb,
        system_disk_gb: vm.system_disk_gb,
        description: vm.description,
      })
    } else {
      setEditingId(null)
      form.resetFields()
    }
    setDrawerOpen(true)
  }, [form])

  const openCloneModal = (vm: VMResponse) => {
    setCloneSourceVm(vm)
    cloneForm.setFieldsValue({ name: `${vm.name}-copy` })
    setCloneModalOpen(true)
  }

  const handleSave = () => {
    form.validateFields().then((values) => {
      // 模板克隆：克隆盘跟随所选存储池落盘（后端 target_storage，缺省时后端用模板原盘位置）
      if (!editingId && values.provider === 'proxmox' && values.template_vmid) {
        values.target_storage = values.storage || 'local-lvm'
      }
      saveMut.mutate({ id: editingId ?? undefined, data: values })
    })
  }

  // ── 表格列 ──
  const columns: ColumnsType<VMResponse> = [
    {
      title: '虚拟机名称', dataIndex: 'name', width: 160, fixed: 'left',
      render: (name: string, r) => <a onClick={() => openDrawer(r)}>{name || '-'}</a>,
    },
    { title: 'IP 地址', dataIndex: 'ip', width: 140, render: (v: string) => v || '-' },
    { title: '操作系统', dataIndex: 'os', width: 140, render: (v: string) => v || '-' },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (status: string) => {
        const s = STATUS_MAP[status] ?? STATUS_MAP.unknown
        return <Badge status={s.color as 'success' | 'default' | 'warning' | 'processing' | 'error'} text={s.text} />
      },
    },
    { title: '宿主节点', dataIndex: 'node', width: 120, render: (v: string) => v || '-' },
    { title: '提供商', dataIndex: 'provider', width: 100, render: (v: string) => v || '-' },
    { title: 'CPU', dataIndex: 'cpu_cores', width: 70, render: (v: number) => v ? `${v}核` : '-' },
    { title: '内存(GB)', dataIndex: 'memory_gb', width: 90, render: (v: number) => v ? `${v} GB` : '-' },
    { title: '磁盘(GB)', dataIndex: 'system_disk_gb', width: 90, render: (v: number) => v ? `${v} GB` : '-' },
    {
      title: '操作', key: 'action', width: 120, fixed: 'right',
      render: (_: unknown, r) => {
        const moreItems: MenuProps['items'] = [
          { key: 'stop', icon: <StopOutlined />, label: '关机', danger: r.status === 'running' },
          { key: 'clone', icon: <CopyOutlined />, label: '克隆虚拟机' },
          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'stop') {
            confirm('vms:poweroff', () => powerMut.mutate({ id: r.id, action: 'stop' }), '确认要执行电源操作（关机）？')
          } else if (key === 'clone') {
            openCloneModal(r)
          } else if (key === 'delete') {
            modal.confirm({
              title: `删除虚拟机 ${r.name}`,
              content: '此操作不可恢复，确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('vms:delete', () => deleteMut.mutate(r.id)),
            })
          }
        }
        return (
          <Space size={4}>
            <Tooltip title={r.status === 'running' ? '运行中' : '开机'}>
              <Button
                size="small" type="text" icon={<PoweroffOutlined />}
                disabled={r.status === 'running'}
                onClick={() => confirm('vms:poweroff', () => powerMut.mutate({ id: r.id, action: 'start' }), '确认要执行电源操作（开机）？')}
              />
            </Tooltip>
            <Tooltip title="编辑">
              <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openDrawer(r)} />
            </Tooltip>
            <Dropdown menu={{ items: moreItems, onClick: onMore }} trigger={['click']}>
              <Button size="small" type="text" icon={<MoreOutlined />} />
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  const rowSelection: TableRowSelection<VMResponse> = {
    selectedRowKeys: selectedKeys,
    onChange: (keys) => setSelectedKeys(keys as string[]),
  }

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      {/* 顶部概览统计卡片 */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
            <Statistic
              title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>虚拟机总数</span>}
              value={totalCount}
              prefix={<HddOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
              valueStyle={{ fontWeight: 600, fontSize: 20 }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
            <Statistic
              title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>运行中</span>}
              value={runningCount}
              prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
              valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
            <Statistic
              title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>已停止</span>}
              value={stoppedCount}
              prefix={<StopOutlined style={{ color: '#8c8c8c', marginRight: 6 }} />}
              valueStyle={{ fontWeight: 600, fontSize: 20 }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
            <Statistic
              title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>CPU 总算力</span>}
              value={totalCpuCores}
              suffix="核"
              prefix={<ThunderboltOutlined style={{ color: '#722ed1', marginRight: 6 }} />}
              valueStyle={{ fontWeight: 600, fontSize: 20 }}
            />
          </Card>
        </Col>
      </Row>

      {/* 操作栏与主表格 */}
      <Flex vertical gap={12} style={{ flex: 1, minWidth: 0 }}>
        <Flex justify="space-between" wrap gap={8}>
          <Space>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索虚拟机名称或 IP"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              allowClear
              style={{ width: 240 }}
            />
            {selectedKeys.length > 0 && (
              <Button danger icon={<DeleteOutlined />}
                onClick={() => confirm('vms:delete', async () => {
                  try {
                    // 后端逐条返回失败原因（如 PVE 配置未完成），必须如实上报而不是谎报全部成功
                    const res = await vmsApi.batchDelete(selectedKeys) as VMBatchDeleteResult
                    queryClient.invalidateQueries({ queryKey: ['vms'] })
                    setSelectedKeys([])
                    const okCount = res?.success_count ?? 0
                    const failed = res?.failed ?? []
                    if (failed.length > 0) {
                      message.warning(`已删除 ${okCount} 台，${failed.length} 台失败：${failed[0]?.reason || '未知原因'}`)
                      pushLocal({
                        type: 'warning', category: 'vm', title: '虚拟机批量删除部分失败',
                        content: `${okCount} 台删除成功，${failed.length} 台失败（${failed[0]?.reason || '未知原因'}）`, link: '/assets/vms',
                      })
                    } else {
                      message.success(`已删除 ${okCount} 台虚拟机`)
                    }
                  } catch (err) {
                    message.error(extractAxiosDetail(err) ?? '虚拟机批量删除失败')
                  }
                })}
              >
                批量删除 ({selectedKeys.length})
              </Button>
            )}
          </Space>
          <Space>
            <Button icon={<SyncOutlined />} onClick={() => {
              message.loading({ content: '正在同步...', key: 'vm-sync' })
              vmsApi.sync()
                .then((res: { message?: string } | undefined) => {
                  queryClient.invalidateQueries({ queryKey: ['vms'] })
                  const msg = res?.message || '虚拟机同步完成'
                  message.success({ content: msg, key: 'vm-sync' })
                  pushLocal({ type: 'success', category: 'vm', title: '虚拟机同步完成', content: msg, link: '/assets/vms' })
                })
                .catch((err: unknown) => {
                  const errMsg = extractAxiosDetail(err) ?? '虚拟机同步失败'
                  message.error({ content: errMsg, key: 'vm-sync' })
                  pushLocal({ type: 'error', category: 'vm', title: '虚拟机同步失败', content: errMsg, link: '/assets/vms' })
                })
            }}>
              云端同步
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新增虚机</Button>
          </Space>
        </Flex>

        <Table<VMResponse>
          rowKey="id"
          rowSelection={rowSelection}
          columns={columns}
          dataSource={filtered}
          loading={isLoading}
          size="middle"
          scroll={{ x: 1250 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 台虚拟机` }}
        />

        {/* 新增/编辑抽屉 */}
        <Drawer
          title={editingId ? '编辑虚拟机' : '新增虚拟机'}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width={480}
          extra={
            <Button type="primary" onClick={handleSave} loading={saveMut.isPending}>保存</Button>
          }
        >
          <Form form={form} layout="vertical">
            <Form.Item name="name" label="虚拟机名称" rules={[{ required: true, message: '请输入名称' }]}>
              <Input placeholder="例如 vm-web-01" />
            </Form.Item>
            <Flex gap={16}>
              <Form.Item name="provider" label="提供商" style={{ flex: 1 }} initialValue="proxmox">
                <Select options={[
                  { value: 'proxmox', label: 'Proxmox' },
                  { value: 'vmware', label: 'VMware' },
                  { value: 'aliyun', label: '阿里云' },
                  { value: 'tencent', label: '腾讯云' },
                ]} />
              </Form.Item>
              <Form.Item name="node" label="宿主节点" style={{ flex: 1 }}>
                {pveCreate ? (
                  <Select
                    showSearch
                    allowClear
                    placeholder="留空使用 PVE 默认节点"
                    options={pveNodes.map((n) => ({ value: n.node }))}
                  />
                ) : (
                  <Input placeholder="例如 pve-node-01" />
                )}
              </Form.Item>
            </Flex>
            {pveCreate && (
              <>
                <Flex gap={16}>
                  <Form.Item name="storage" label="存储池" style={{ flex: 1 }} initialValue="local-lvm">
                    <Select
                      showSearch
                      allowClear
                      placeholder="默认 local-lvm"
                      options={pveStorages.map((s) => ({ value: s.storage }))}
                    />
                  </Form.Item>
                  <Form.Item name="bridge" label="网桥" style={{ flex: 1 }} initialValue="vmbr0">
                    <Select
                      showSearch
                      allowClear
                      placeholder="默认 vmbr0"
                      options={pveBridges.map((b) => ({ value: b.bridge }))}
                    />
                  </Form.Item>
                </Flex>
                <Flex gap={16}>
                  <Form.Item name="template_vmid" label="模板" style={{ flex: 1 }}>
                    <Select
                      showSearch
                      allowClear
                      placeholder="可选，选择后从模板克隆"
                      options={pveTemplates.map((t) => ({ value: t.vmid, label: t.name }))}
                    />
                  </Form.Item>
                  <Form.Item
                    name="boot_iso" label="ISO 镜像" style={{ flex: 1 }}
                    dependencies={['template_vmid']}
                    rules={[{
                      validator: (_, v) => (v || form.getFieldValue('template_vmid'))
                        ? Promise.resolve()
                        : Promise.reject(new Error('模板与 ISO 至少选择一个（全新创建必须挂 ISO）')),
                    }]}
                  >
                    <Select
                      showSearch
                      allowClear
                      placeholder="全新创建时必选"
                      options={pveIsos.map((i) => ({ value: i.volid, label: i.file_name }))}
                    />
                  </Form.Item>
                </Flex>
                <Form.Item name="start_after_create" label="创建后自动开机" valuePropName="checked">
                  <Switch size="small" />
                </Form.Item>
              </>
            )}
            <Form.Item name="ip" label="IP 地址">
              <Input placeholder="192.168.1.100" />
            </Form.Item>
            <Form.Item name="os" label="操作系统">
              <Input placeholder="例如 Ubuntu 22.04" />
            </Form.Item>
            <Flex gap={16}>
              <Form.Item name="cpu_cores" label="CPU 核数" style={{ flex: 1 }}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="memory_gb" label="内存(GB)" style={{ flex: 1 }}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="system_disk_gb" label="磁盘(GB)" style={{ flex: 1 }}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Flex>
            <Form.Item name="description" label="备注">
              <Input.TextArea rows={3} />
            </Form.Item>
          </Form>
        </Drawer>

        {/* 克隆 Modal */}
        <Modal
          title={`克隆虚拟机 · ${cloneSourceVm?.name || ''}`}
          open={cloneModalOpen}
          onCancel={() => setCloneModalOpen(false)}
          onOk={() => {
            cloneForm.validateFields().then((v) => {
              if (cloneSourceVm) {
                cloneMut.mutate({ id: cloneSourceVm.id, name: v.name })
              }
            })
          }}
          confirmLoading={cloneMut.isPending}
          okText="确认克隆"
        >
          <Form form={cloneForm} layout="vertical" style={{ marginTop: 16 }}>
            <Form.Item name="name" label="新虚拟机名称" rules={[{ required: true, message: '请输入新名称' }]}>
              <Input placeholder="例如 vm-web-01-copy" />
            </Form.Item>
          </Form>
        </Modal>
      </Flex>
    </Flex>
  )
}
