import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Drawer, Form, Select, InputNumber,
  Tooltip, Flex, App, Badge, Tag, Steps, Alert, Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, EditOutlined,
  DeleteOutlined, InfoCircleOutlined, CloudServerOutlined, HeartOutlined, ArrowRightOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { clustersApi } from '@/api'
import { useRiskConfirm } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import ClusterHealthModal from '@/components/ClusterHealthModal'
import type { ClusterResponse, ClusterCreate } from '@/types/api'

const STATUS_MAP: Record<string, { color: 'success' | 'error' | 'warning' | 'default' | 'processing'; text: string }> = {
  // 后端 create/refresh 成功写 running、失败写 disconnected（与模型默认一致）
  running:      { color: 'success',    text: '运行中' },
  connected:    { color: 'success',    text: '已连接' }, // 历史数据兼容
  disconnected: { color: 'error',      text: '断开' },
  unknown:      { color: 'default',    text: '未知' },
  pending:      { color: 'processing', text: '连接中' },
}

const PROVIDER_OPTIONS = [
  { value: 'k8s',     label: 'Kubernetes' },
  { value: 'k3s',     label: 'K3s' },
  { value: 'eks',     label: 'AWS EKS' },
  { value: 'gke',     label: 'GCP GKE' },
  { value: 'aks',     label: 'Azure AKS' },
  { value: 'rancher', label: 'Rancher' },
  { value: 'other',   label: '其他' },
]

export default function ClustersPage() {

  const { confirm } = useRiskConfirm()
  const navigate = useNavigate()
  const { message, modal } = App.useApp()

  const [search, setSearch]         = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingCluster, setEditingCluster] = useState<ClusterResponse | null>(null)
  const [editForm]  = Form.useForm<{ name: string; provider: string }>()
  const [editDrawerOpen, setEditDrawerOpen] = useState(false)
  const [healthCluster, setHealthCluster] = useState<ClusterResponse | null>(null)
  const [scanStep, setScanStep]     = useState<'yaml' | 'info'>('yaml')
  const [scanLoading, setScanLoading] = useState(false)
  const [scanResult, setScanResult] = useState<Record<string, unknown> | null>(null)
  const [form]    = Form.useForm<ClusterCreate>()

  const { data: clusters = [], isLoading } = useQuery({
    queryKey: ['clusters'],
    queryFn: clustersApi.list,
  })

  const { saveMut, deleteMut } = useCrudResource<ClusterCreate, string, { name?: string; provider?: string }>({
    queryKey: ['clusters'],
    entityName: '集群',
    createFn: clustersApi.create,
    updateFn: clustersApi.update,
    deleteFn: clustersApi.delete,
    onSaved: () => {
      setDrawerOpen(false)
      setEditDrawerOpen(false)
      form.resetFields()
      setScanStep('yaml')
      setScanResult(null)
    },
  })

  const openEdit = (r: ClusterResponse) => {
    setEditingCluster(r)
    editForm.setFieldsValue({ name: r.name, provider: r.provider })
    setEditDrawerOpen(true)
  }

  const filtered = clusters.filter((c) =>
    !search || c.name.toLowerCase().includes(search.toLowerCase()),
  )

  const handleScan = async () => {
    const yaml = form.getFieldValue('kubeconfig_yaml')
    if (!yaml?.trim()) { message.warning('请先粘贴 Kubeconfig YAML'); return }
    setScanLoading(true)
    setScanResult(null)
    try {
      const res = await clustersApi.scan(yaml) as Record<string, unknown>
      setScanResult(res)
      // 自动回填基本信息
      if (res) {
        form.setFieldsValue({
          version:      String(res.server_version ?? ''),
          node_count:   Number(res.node_count ?? 0),
          pod_capacity: Number(res.pod_capacity ?? 0),
          pod_usage:    Number(res.pod_usage ?? 0),
          cpu_cores:    Number(res.cpu_cores ?? 0),
          mem_gb:       Number(res.mem_gb ?? 0),
        })
        setScanStep('info')
      }
    } catch {
      message.error('扫描失败，请确认 Kubeconfig 可访问目标集群')
    } finally {
      setScanLoading(false)
    }
  }

  const handleSubmit = () => {
    form.validateFields().then((v) => saveMut.mutate({ data: v }))
  }

  const columns: ColumnsType<ClusterResponse> = [
    {
      title: '集群名称', dataIndex: 'name', width: 180, fixed: 'left',
      render: (v: string, r) => (
        <a onClick={() => navigate(`/assets/clusters/${r.cluster_id}`)}>
          <CloudServerOutlined style={{ marginRight: 6, color: '#1677ff' }} />{v}
        </a>
      ),
    },
    { title: '提供商', dataIndex: 'provider', width: 120, render: (v: string) => PROVIDER_OPTIONS.find(o => o.value === v)?.label ?? v },
    { title: 'K8s 版本', dataIndex: 'version', width: 120 },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (s: string) => {
        const m = STATUS_MAP[s] ?? STATUS_MAP.unknown
        return <Badge status={m.color} text={m.text} />
      },
    },
    { title: '节点数', dataIndex: 'node_count', width: 80 },
    {
      title: 'Pod 水位', key: 'pods', width: 120,
      render: (_: unknown, r) => {
        const pct = r.pod_capacity > 0 ? Math.round((r.pod_usage / r.pod_capacity) * 100) : 0
        return <Tag color={pct > 80 ? 'red' : pct > 60 ? 'orange' : 'green'}>{r.pod_usage}/{r.pod_capacity} ({pct}%)</Tag>
      },
    },
    { title: 'CPU(核)', dataIndex: 'cpu_cores', width: 90 },
    { title: '内存(GB)', dataIndex: 'mem_gb', width: 90 },
    { title: '纳管时间', dataIndex: 'created_at', width: 160, render: (v: string) => v ?? '-' },
    {
      title: '操作', key: 'action', width: 110, fixed: 'right',
      render: (_: unknown, r) => {
        const moreItems: MenuProps['items'] = [
          { key: 'edit', icon: <EditOutlined />, label: '编辑' },
          { key: 'delete', icon: <DeleteOutlined />, label: '解除纳管', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'edit') openEdit(r)
          if (key === 'delete') {
            modal.confirm({
              title: `解除纳管 ${r.name}`,
              content: '将从 OpsPilot 移除该集群，不会影响真实集群。确定继续？',
              okText: '解除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('clusters:delete', () => deleteMut.mutate(r.cluster_id)),
            })
          }
        }
        return (
          <Space size={4}>
            <Tooltip title="进入详情">
              <Button size="small" type="primary" ghost icon={<ArrowRightOutlined />} onClick={() => navigate(`/assets/clusters/${r.cluster_id}`)} />
            </Tooltip>
            <Tooltip title="健康摘要">
              <Button size="small" type="text" icon={<HeartOutlined />} onClick={() => setHealthCluster(r)} />
            </Tooltip>
            <Dropdown menu={{ items: moreItems, onClick: onMore }} trigger={['click']}>
              <Button size="small" type="text" icon={<MoreOutlined />} />
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" wrap gap={8}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索集群名称"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear
          style={{ width: 260 }}
        />
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setScanStep('yaml'); setScanResult(null); setDrawerOpen(true) }}>
            纳管集群
          </Button>
        </Space>
      </Flex>

      <Table<ClusterResponse>
        rowKey="cluster_id"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        size="middle"
        scroll={{ x: 1100 }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个集群` }}
      />

      {/* 纳管抽屉 */}
      <Drawer
        title="纳管 Kubernetes 集群"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={560}
        extra={
          <Space>
            {scanStep === 'yaml'
              ? <Button type="primary" onClick={handleScan} loading={scanLoading} icon={<InfoCircleOutlined />}>探测集群信息</Button>
              : <Button type="primary" onClick={handleSubmit} loading={saveMut.isPending}>确认纳管</Button>
            }
          </Space>
        }
      >
        <Steps
          current={scanStep === 'yaml' ? 0 : 1}
          items={[{ title: '粘贴 Kubeconfig' }, { title: '确认集群信息' }]}
          style={{ marginBottom: 24 }}
        />

        <Form form={form} layout="vertical">
          {/* 步骤 1：Kubeconfig */}
          <Form.Item name="kubeconfig_yaml" label="Kubeconfig YAML" rules={[{ required: true, message: '请粘贴 Kubeconfig' }]}>
            <Input.TextArea
              rows={10}
              placeholder={'apiVersion: v1\nkind: Config\nclusters:\n  - cluster:\n      server: https://...\n...'}
              style={{ fontFamily: 'monospace', fontSize: 12 }}
            />
          </Form.Item>

          {/* 步骤 2：集群基本信息（探测后回填，仍可手动修改） */}
          {scanStep === 'info' && (
            <>
              {scanResult && (
                <Alert type="success" showIcon message="探测成功，已自动填入集群信息，可按需调整后提交。" style={{ marginBottom: 16 }} />
              )}
              <Form.Item name="name" label="集群名称（别名）" rules={[{ required: true }]}>
                <Input placeholder="例如 prod-k8s" />
              </Form.Item>
              <Flex gap={16}>
                <Form.Item name="provider" label="类型" style={{ flex: 1 }} initialValue="k8s" rules={[{ required: true }]}>
                  <Select options={PROVIDER_OPTIONS} />
                </Form.Item>
                <Form.Item name="version" label="K8s 版本" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <Input placeholder="v1.28.0" />
                </Form.Item>
              </Flex>
              <Flex gap={16}>
                <Form.Item name="node_count" label="节点数" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="pod_capacity" label="Pod 容量" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="pod_usage" label="Pod 已用" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
              </Flex>
              <Flex gap={16}>
                <Form.Item name="cpu_cores" label="CPU(核)" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="mem_gb" label="内存(GB)" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
              </Flex>
              {/* kubeconfig_ref 用 kubeconfig_yaml 计算 hash 或后端处理，前端只需传 yaml */}
              <Form.Item name="kubeconfig_ref" initialValue="managed" hidden><Input /></Form.Item>
            </>
          )}
        </Form>
      </Drawer>

      {/* 编辑抽屉 */}
      <Drawer
        title={`编辑集群 · ${editingCluster?.name}`}
        open={editDrawerOpen}
        onClose={() => setEditDrawerOpen(false)}
        width={400}
        extra={
          <Button type="primary" loading={saveMut.isPending} onClick={() =>
            // 后端集群编辑挂 edit 守卫动作，需确认 + 一次性票据
            editForm.validateFields().then((v) =>
              confirm('clusters:edit', () => saveMut.mutate({ id: editingCluster!.cluster_id, data: v }))
            )
          }>保存</Button>
        }
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="name" label="集群名称（别名）" rules={[{ required: true }]}>
            <Input placeholder="例如 prod-k8s" />
          </Form.Item>
          <Form.Item name="provider" label="类型" rules={[{ required: true }]}>
            <Select options={PROVIDER_OPTIONS} />
          </Form.Item>
        </Form>
      </Drawer>

      <ClusterHealthModal
        open={!!healthCluster}
        clusterId={healthCluster?.cluster_id ?? ''}
        clusterName={healthCluster?.name}
        onClose={() => setHealthCluster(null)}
      />
    </Flex>
  )
}
