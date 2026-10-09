import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Typography, Flex, Select, Tooltip, Popconfirm,
  Spin, Input, Drawer, App,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, CodeOutlined, FileTextOutlined, MedicineBoxOutlined,
  DeleteOutlined,
} from '@ant-design/icons'
import { clusterDetailApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import PodLogViewer from '@/components/PodLogViewer'
import PodExecTerminal from '@/components/PodExecTerminal'
import YamlPre from '@/components/k8s/YamlPre'
import type { K8sPod } from '@/types/api'
import { statusTag } from './shared'
import { useUrlFilter, useUrlFocus, focusRowClass } from './useTabParams'

const { Text } = Typography

const POD_STATUS_GROUPS: Array<{ key: string; label: string; match: (status: string) => boolean }> = [
  { key: '_all', label: '全部', match: () => true },
  { key: 'running', label: 'Running', match: (s) => s.toLowerCase() === 'running' },
  { key: 'not_running', label: '非 Running', match: (s) => s.toLowerCase() !== 'running' },
  { key: 'pending', label: 'Pending', match: (s) => s.toLowerCase() === 'pending' },
  { key: 'failed', label: 'Failed/CrashLoop', match: (s) => ['failed', 'crashloopbackoff', 'error', 'evicted'].includes(s.toLowerCase()) },
]

export default function PodsTab({ clusterId, namespaces }: { clusterId: string; namespaces: string[] }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.assets.clusters.manage')

  // ns / 关键字筛选入 URL：搜索跳转带参数进来时直接预填并高亮目标行，刷新不丢。
  const [nsFilter, setNsFilter] = useUrlFilter('ns', '_all')
  const [search, setSearch] = useUrlFilter('q')
  const [statusFilter, setStatusFilter] = useState('_all')
  const [diagPod, setDiagPod] = useState<K8sPod | null>(null)
  const [logPod, setLogPod] = useState<K8sPod | null>(null)
  const [execPod, setExecPod] = useState<K8sPod | null>(null)
  const focus = useUrlFocus()

  const { data: pods = [], isLoading, refetch } = useQuery({
    queryKey: ['cluster-pods', clusterId, nsFilter],
    queryFn: () => clusterDetailApi.pods(clusterId, nsFilter),
    refetchInterval: 45 * 1000,
  })

  // 日志/终端抽屉共用容器清单：打开任一时拉 Pod 详情。
  const focusPod = logPod || execPod
  const { data: logPodDetail } = useQuery({
    queryKey: ['cluster-pod-detail', clusterId, focusPod?.namespace, focusPod?.name],
    queryFn: () => clusterDetailApi.podDetail(clusterId, focusPod!.namespace, focusPod!.name),
    enabled: !!focusPod,
  })

  const { data: diagnostics, isLoading: loadingDiag } = useQuery({
    queryKey: ['cluster-pod-diagnostics', clusterId, diagPod?.namespace, diagPod?.name],
    queryFn: () => clusterDetailApi.podDiagnostics(clusterId, diagPod!.namespace, diagPod!.name),
    enabled: !!diagPod,
  })

  const deleteMutation = useMutation({
    mutationFn: (pod: K8sPod) => clusterDetailApi.deletePod(clusterId, pod.namespace, pod.name),
    onSuccess: () => { message.success('Pod 已删除'); queryClient.invalidateQueries({ queryKey: ['cluster-pods', clusterId] }) },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const logContainers = useMemo(() => {
    const containers = logPodDetail?.containers
    if (Array.isArray(containers)) {
      return containers
        .map((c) => (typeof c === 'string' ? c : (c as { name?: string })?.name))
        .filter((v): v is string => !!v)
    }
    return []
  }, [logPodDetail])

  const filtered = pods.filter((p) => {
    if (nsFilter !== '_all' && p.namespace !== nsFilter) return false
    const group = POD_STATUS_GROUPS.find((g) => g.key === statusFilter)
    if (group && !group.match(p.status)) return false
    if (search && !`${p.namespace}/${p.name}`.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const columns: ColumnsType<K8sPod> = [
    {
      title: 'Pod', dataIndex: 'name', width: 250, ellipsis: true,
      render: (v: string) => <Text strong style={{ fontSize: 12 }}>{v}</Text>,
    },
    { title: 'Namespace', dataIndex: 'namespace', width: 130 },
    { title: '状态', dataIndex: 'status', width: 140, render: (v: string) => statusTag(v) },
    { title: 'Ready', dataIndex: 'ready', width: 75 },
    {
      title: '重启', dataIndex: 'restarts', width: 70, align: 'right',
      render: (v: number) => <Text type={v > 10 ? 'danger' : v > 0 ? 'warning' : undefined}>{v}</Text>,
    },
    {
      title: '诊断', dataIndex: 'diagnosis', ellipsis: true,
      render: (v: string) => v || <Text type="secondary">-</Text>,
    },
    { title: '节点', dataIndex: 'node', width: 135, ellipsis: true },
    { title: '运行时长', dataIndex: 'age', width: 100 },
    {
      title: '操作', key: 'ops', width: 160, fixed: 'right',
      render: (_, record) => (
        <Space size={2}>
          <Tooltip title="终端（exec /bin/sh）">
            <Button size="small" type="text" icon={<CodeOutlined />} disabled={!canManage} onClick={() => setExecPod(record)} />
          </Tooltip>
          <Tooltip title="日志（实时流）">
            <Button size="small" type="text" icon={<FileTextOutlined />} onClick={() => setLogPod(record)} />
          </Tooltip>
          <Tooltip title="诊断详情（事件/重启/探针）">
            <Button size="small" type="text" icon={<MedicineBoxOutlined />} onClick={() => setDiagPod(record)} />
          </Tooltip>
          <Popconfirm
            title={`删除 Pod ${record.name}？`} description="删除后由控制器重建（如 Deployment）。"
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => riskConfirm.confirm('clusters:delete', () => deleteMutation.mutate(record),
              `将删除 Pod ${record.namespace}/${record.name}，删除后由所属控制器重建`)}
            disabled={!canManage}
          >
            <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={!canManage} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card size="small">
      <Flex justify="space-between" wrap gap="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <Select
            value={nsFilter} onChange={setNsFilter} style={{ width: 160 }} showSearch
            options={[{ value: '_all', label: '全部 Namespace' }, ...namespaces.map((ns) => ({ value: ns, label: ns }))]}
          />
          <Select
            value={statusFilter} onChange={setStatusFilter} style={{ width: 135 }}
            options={POD_STATUS_GROUPS.map((g) => ({ value: g.key, label: g.label }))}
          />
          <Input.Search
            placeholder="搜索 Pod" allowClear style={{ width: 180 }}
            value={search} onChange={(e) => setSearch(e.target.value)}
          />
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
      </Flex>
      <Table<K8sPod>
        rowKey={(r) => `${r.namespace}/${r.name}`}
        size="small"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        rowClassName={(r) => focusRowClass(focus, 'Pod', r.namespace, r.name)}
        pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 个` }}
        scroll={{ x: 1100 }}
      />

      <PodLogViewer
        open={!!logPod}
        clusterId={clusterId}
        namespace={logPod?.namespace || ''}
        podName={logPod?.name || ''}
        containers={logContainers}
        onClose={() => setLogPod(null)}
      />

      <PodExecTerminal
        open={!!execPod}
        clusterId={clusterId}
        namespace={execPod?.namespace || ''}
        podName={execPod?.name || ''}
        containers={logContainers}
        onClose={() => setExecPod(null)}
      />

      <Drawer
        title={`Pod 诊断 - ${diagPod?.name || ''}`}
        open={!!diagPod}
        onClose={() => setDiagPod(null)}
        width={680}
      >
        {loadingDiag ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="分析中..." /></div>
        ) : (
          <YamlPre
            maxHeight="70vh"
            text={typeof diagnostics === 'string' ? diagnostics : JSON.stringify(diagnostics, null, 2)}
          />
        )}
      </Drawer>
    </Card>
  )
}
