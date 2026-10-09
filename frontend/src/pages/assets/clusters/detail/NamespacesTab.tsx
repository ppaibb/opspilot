import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Modal, Select, Tooltip, Spin, App,
  Input, Popconfirm, Form, Alert, Drawer, Descriptions,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, PlusOutlined, TagsOutlined, DeleteOutlined,
} from '@ant-design/icons'
import { clusterDetailApi, clusterOpsApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import type { NamespaceRow } from './types'
import { statusTag, PROTECTED_NAMESPACES } from './shared'

/** 命名空间用量（GET /clusters/{cid}/namespaces/{ns}/usage）。 */
interface NamespaceUsage {
  pod_count: number
  cpu_requests_total: number
  mem_requests_total_gi: number
  quota: { name: string; hard: Record<string, string>; used: Record<string, string> } | null
  top_pods: Array<{ name: string; status: string; cpu_request: number; mem_request: number }>
}

// ───────────────────────── 命名空间 Tab ─────────────────────────

export default function NamespacesTab({ clusterId }: { clusterId: string }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.assets.clusters.manage')
  const [createOpen, setCreateOpen] = useState(false)
  const [form] = Form.useForm()
  const riskConfirm = useRiskConfirm()
  const [labelsTarget, setLabelsTarget] = useState<string | null>(null)

  // queryKey 与详情页共享的 namespace 清单保持一致：创建/删除后 invalidate，
  // 工作负载/Pod/资源浏览三个 Tab 的下拉会同步刷新。
  const { data: namespaces = [], isLoading } = useQuery({
    queryKey: ['cluster-namespaces', clusterId],
    queryFn: () => clusterDetailApi.namespaces(clusterId),
  })
  const invalidateNamespaces = () =>
    queryClient.invalidateQueries({ queryKey: ['cluster-namespaces', clusterId] })

  const deleteNsMutation = useMutation({
    mutationFn: (ns: string) => clusterOpsApi.deleteNamespace(clusterId, ns),
    onSuccess: (_d, ns) => {
      message.success(`命名空间 ${ns} 已进入 Terminating`)
      invalidateNamespaces()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  // 选中行展开用量（ResourceQuota 聚合）。
  const [usageNs, setUsageNs] = useState<string | null>(null)
  const { data: usage } = useQuery({
    queryKey: ['cluster-ns-usage', clusterId, usageNs],
    queryFn: () => clusterOpsApi.namespaceUsage(clusterId, usageNs!),
    enabled: !!usageNs,
  })
  const usageView = usage as NamespaceUsage | undefined

  const createMutation = useMutation({
    mutationFn: (values: { name: string; labels?: string; quota_preset?: string | null }) => {
      const labels: Record<string, string> = {}
      if (values.labels) {
        for (const pair of values.labels.split(',')) {
          const [k, v] = pair.split('=')
          if (k?.trim() && v?.trim()) labels[k.trim()] = v.trim()
        }
      }
      return clusterOpsApi.createNamespace(clusterId, {
        name: values.name,
        labels,
        quota_preset: values.quota_preset && values.quota_preset !== 'none' ? values.quota_preset : null,
      })
    },
    onSuccess: () => {
      message.success('命名空间已创建')
      setCreateOpen(false)
      invalidateNamespaces()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '创建失败'),
  })

  const columns: ColumnsType<NamespaceRow> = [
    {
      title: '命名空间', dataIndex: 'name', width: 200,
      render: (v: string) => (
        <a onClick={() => setUsageNs(v === usageNs ? null : v)}>{v}</a>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 100, render: (v: string) => statusTag(v) },
    { title: '创建时间', dataIndex: 'age', width: 120 },
    {
      title: '操作', key: 'ops', width: 110,
      render: (_: unknown, r) => {
        // 核心命名空间后端 403 硬拒，前端先禁用按钮并说明原因，避免连点两次确认后被拒。
        const protectedNs = PROTECTED_NAMESPACES.includes(r.name)
        return (
          <Space size={2}>
            <Tooltip title="编辑标签">
              <Button size="small" type="text" icon={<TagsOutlined />}
                disabled={!canManage} onClick={() => setLabelsTarget(r.name)} />
            </Tooltip>
            <Popconfirm
              title={`删除命名空间 ${r.name}？`}
              description="其下全部资源（Pod/Service/配置等）将被级联删除，不可恢复！"
              okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
              onConfirm={() => riskConfirm.confirm('clusters:delete', () => deleteNsMutation.mutate(r.name),
                `将删除命名空间 ${r.name} 及其下全部资源（级联删除，不可恢复）`)}
              disabled={!canManage || protectedNs}
            >
              <Tooltip title={protectedNs ? '集群核心命名空间，禁止删除' : '删除（级联）'}>
                <Button size="small" type="text" danger icon={<DeleteOutlined />}
                  disabled={!canManage || protectedNs} />
              </Tooltip>
            </Popconfirm>
          </Space>
        )
      },
    },
  ]

  return (
    <Card
      size="small"
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={invalidateNamespaces}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canManage} onClick={() => { form.resetFields(); setCreateOpen(true) }}>
            创建命名空间
          </Button>
        </Space>
      }
    >
      <Table
        rowKey="name"
        size="small"
        columns={columns}
        dataSource={namespaces}
        loading={isLoading}
        pagination={false}
      />

      {usageNs && (
        <Card size="small" title={`资源用量 - ${usageNs}`} style={{ marginTop: 12 }}>
          {usageView ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions size="small" column={2} bordered items={[
                { key: 'pod', label: 'Pod 数', children: usageView.pod_count ?? 0 },
                { key: 'cpu', label: 'CPU requests 合计', children: `${usageView.cpu_requests_total ?? 0} 核` },
                { key: 'mem', label: '内存 requests 合计', children: `${usageView.mem_requests_total_gi ?? 0} Gi` },
                { key: 'quota', label: 'ResourceQuota', children: usageView.quota ? `${usageView.quota.name}（上限 ${usageView.quota.hard['requests.cpu'] ?? '-'} 核 / ${usageView.quota.hard['requests.memory'] ?? '-'}）` : '未设置' },
              ]} />
              {!!usageView.top_pods?.length && (
                <Table
                  rowKey="name"
                  size="small"
                  pagination={false}
                  dataSource={usageView.top_pods}
                  columns={[
                    { title: 'Pod', dataIndex: 'name', ellipsis: true },
                    { title: '状态', dataIndex: 'status', width: 100, render: (v: string) => statusTag(v) },
                    { title: 'CPU request（核）', dataIndex: 'cpu_request', width: 150, align: 'right' },
                    { title: '内存 request（Gi）', dataIndex: 'mem_request', width: 150, align: 'right' },
                  ]}
                />
              )}
            </Space>
          ) : (
            <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div>
          )}
        </Card>
      )}

      <Modal
        title="创建命名空间"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        // 后端 create_namespace 守卫动作，需确认 + 一次性票据
        onOk={() => riskConfirm.confirm('clusters:create_namespace', () => form.submit())}
        okText="创建" cancelText="取消"
        confirmLoading={createMutation.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(values) => createMutation.mutate(values)}>
          <Form.Item
            name="name" label="名称"
            rules={[
              { required: true, message: '请输入名称' },
              { pattern: /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/, message: '小写字母/数字/中划线' },
            ]}
          >
            <Input placeholder="demo-team" />
          </Form.Item>
          <Form.Item name="labels" label="标签（可选，k=v 逗号分隔）">
            <Input placeholder="team=backend,env=prod" />
          </Form.Item>
          <Form.Item name="quota_preset" label="资源配额" initialValue="none">
            <Select
              options={[
                { value: 'none', label: '不设置' },
                { value: 'small', label: '小（2C / 4Gi / 20 Pod）' },
                { value: 'medium', label: '中（8C / 16Gi / 50 Pod）' },
                { value: 'large', label: '大（32C / 64Gi / 200 Pod）' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <NamespaceLabelsEditor
        clusterId={clusterId}
        namespace={labelsTarget}
        onClose={() => setLabelsTarget(null)}
      />
    </Card>
  )
}

/** 命名空间标签编辑抽屉：与节点标签同语义（每行 k=v，-key 删除，系统前缀后端保护）。 */
function NamespaceLabelsEditor({ clusterId, namespace, onClose }: { clusterId: string; namespace: string | null; onClose: () => void }) {
  const { message } = App.useApp()
  const riskConfirm = useRiskConfirm()
  const [labelsText, setLabelsText] = useState('')

  // namespaces 列表接口不返回 labels，复用 edit-form 只读通道回填现有值。
  const { data: nsMeta } = useQuery({
    queryKey: ['namespace-labels-form', clusterId, namespace],
    queryFn: () => clusterOpsApi.resourceEditForm(clusterId, '_all', 'Namespace', namespace!),
    enabled: !!namespace,
  })

  useEffect(() => {
    if (!namespace) return
    // 后端保护 kubernetes.io / k8s.io 前缀，这里照样展示便于用户知晓。
    // 回填延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => {
      setLabelsText(nsMeta?.labels ? Object.entries(nsMeta.labels).map(([k, v]) => `${k}=${v}`).join('\n') : '')
    })
  }, [namespace, nsMeta])

  const saveMutation = useMutation({
    mutationFn: () => {
      const labels: Record<string, string | null> = {}
      for (const line of labelsText.split('\n')) {
        const row = line.trim()
        if (!row) continue
        if (row.startsWith('-')) {
          const key = row.slice(1).split('=')[0].trim()
          if (key) labels[key] = null
          continue
        }
        const [k, v] = row.split('=')
        if (k?.trim()) labels[k.trim()] = (v ?? '').trim()
      }
      return clusterOpsApi.patchNamespaceLabels(clusterId, namespace!, labels)
    },
    onSuccess: (result) => {
      const applied = Object.keys(result?.applied || {}).length
      message.success(`标签已更新（${applied} 项生效，系统前缀自动跳过）`)
      onClose()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  return (
    <Drawer
      title={`命名空间标签 - ${namespace || ''}`}
      open={!!namespace}
      onClose={onClose}
      width={520}
      extra={
        <Button type="primary" loading={saveMutation.isPending}
          onClick={() => riskConfirm.confirm('clusters:patch_labels', () => saveMutation.mutate())}>
          保存
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert type="info" showIcon message="每行一个 k=v；-key 表示删除该标签；kubernetes.io / k8s.io 前缀受后端保护自动跳过。" />
        <Input.TextArea
          rows={6}
          value={labelsText}
          onChange={(e) => setLabelsText(e.target.value)}
          placeholder={'team=backend\nenv=prod\n-old-label'}
          style={{ fontFamily: 'monospace', fontSize: 12 }}
        />
      </Space>
    </Drawer>
  )
}
