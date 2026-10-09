import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Alert, Drawer, Modal, App,
  Tooltip, Empty, Spin, Progress, Descriptions, Input,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, PauseCircleOutlined, PlayCircleOutlined,
  DragOutlined, TagsOutlined,
} from '@ant-design/icons'
import { clusterDetailApi, clusterOpsApi } from '@/api'
import {
  usePermission, useRiskConfirm, extractAxiosDetail, fetchRiskTicket,
} from '@/hooks/usePermission'
import type { K8sNode, K8sNodeDetail } from '@/types/api'
import { statusTag } from './shared'
import { useUrlFocus, focusRowClass } from './useTabParams'

const { Text } = Typography

// ── K8s quantity 解析（节点用量条用） ──

/** "4" → 4000m；"32250m" → 32250m。返回 millicores。
 *  后端 cpu_capacity 实际是 float（parse_k8s_cpu 已解析的核数），数字按核数处理。 */
function parseCpuToMillicores(value: string | number | undefined): number {
  if (value == null || value === '') return 0
  if (typeof value === 'number') return value * 1000
  const v = String(value).trim()
  if (!v) return 0
  if (v.endsWith('m')) return parseFloat(v) || 0
  return (parseFloat(v) || 0) * 1000
}

/** "16Gi"/"16379888Ki" → bytes。
 *  后端 mem_capacity 实际是 float（parse_k8s_memory_to_gib 已解析的 GiB），数字按 GiB 处理。 */
function parseMemToBytes(value: string | number | undefined): number {
  if (value == null || value === '') return 0
  if (typeof value === 'number') return value * 1024 ** 3
  const v = String(value).trim()
  if (!v) return 0
  const units: Array<[string, number]> = [
    ['Ki', 1024], ['Mi', 1024 ** 2], ['Gi', 1024 ** 3], ['Ti', 1024 ** 4],
  ]
  for (const [suffix, mult] of units) {
    if (v.endsWith(suffix)) return (parseFloat(v) || 0) * mult
  }
  return parseFloat(v) || 0
}

function formatCpu(millicores: number): string {
  return millicores >= 1000 ? `${(millicores / 1000).toFixed(1)} 核` : `${Math.round(millicores)}m`
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} Gi`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} Mi`
  return `${(bytes / 1024).toFixed(0)} Ki`
}

function usagePercent(used: number, capacity: number): number {
  if (!capacity) return 0
  return Math.min(100, Math.round((used / capacity) * 100))
}

/** 节点 CPU/内存用量条（Rancher 风格）：capacity 来自节点 status，usage 来自 metrics-server。 */
function UsageBar({ used, capacity, format }: { used: number; capacity: number; format: (v: number) => string }) {
  const pct = usagePercent(used, capacity)
  return (
    <Tooltip title={capacity ? `${format(used)} / ${format(capacity)}` : '无容量数据'}>
      <div style={{ minWidth: 110 }}>
        <Progress
          percent={pct} size="small"
          status={pct > 85 ? 'exception' : pct > 65 ? 'active' : 'normal'}
          format={() => (capacity ? `${pct}%` : '-')}
        />
        <Text type="secondary" style={{ fontSize: 11 }}>{capacity ? `${format(used)} / ${format(capacity)}` : format(used)}</Text>
      </div>
    </Tooltip>
  )
}

// ───────────────────────── 节点 Tab ─────────────────────────

export default function NodesTab({ clusterId }: { clusterId: string }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.assets.clusters.manage')

  const [drainNode, setDrainNode] = useState<K8sNode | null>(null)
  const [detailNode, setDetailNode] = useState<string | null>(null)
  const [editNodeMeta, setEditNodeMeta] = useState<K8sNode | null>(null)

  const { data: nodes = [], isLoading, refetch } = useQuery({
    queryKey: ['cluster-nodes', clusterId],
    queryFn: () => clusterDetailApi.nodes(clusterId),
    refetchInterval: 60 * 1000,
  })

  // metrics-server 实时用量：缺省时用量条优雅降级（仅显示容量）。
  const { data: metrics } = useQuery({
    queryKey: ['cluster-node-metrics', clusterId],
    queryFn: () => clusterDetailApi.nodeMetrics(clusterId),
    refetchInterval: 60 * 1000,
    retry: false,
  })

  const { data: nodeDetail } = useQuery({
    queryKey: ['cluster-node-detail', clusterId, detailNode],
    queryFn: () => clusterDetailApi.nodeDetail(clusterId, detailNode!),
    enabled: !!detailNode,
  })

  const { data: drainPreview, isLoading: loadingPreview } = useQuery({
    queryKey: ['cluster-node-drain-preview', clusterId, drainNode?.name],
    queryFn: () => clusterDetailApi.nodeDrainPreview(clusterId, drainNode!.name),
    enabled: !!drainNode,
  })

  const metricsMap = useMemo(() => {
    if (!metrics?.available) return {}
    return Object.fromEntries((metrics.items || []).map((m) => [m.name, m]))
  }, [metrics])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cluster-nodes', clusterId] })

  const cordonMutation = useMutation({
    mutationFn: ({ name, uncordon }: { name: string; uncordon: boolean }) =>
      uncordon ? clusterDetailApi.nodeUncordon(clusterId, name) : clusterDetailApi.nodeCordon(clusterId, name),
    onSuccess: (_d, vars) => {
      message.success(vars.uncordon ? '已恢复调度' : '已设置不可调度')
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '操作失败'),
  })

  const drainMutation = useMutation({
    mutationFn: (name: string) => clusterDetailApi.nodeDrain(clusterId, name),
    onSuccess: () => { message.success('排空完成'); setDrainNode(null); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '排空失败'),
  })

  const focus = useUrlFocus()
  const columns: ColumnsType<K8sNode> = [
    {
      title: '节点', dataIndex: 'name', width: 210, fixed: 'left',
      render: (v: string, record) => (
        <Space size={6}>
          <a onClick={() => setDetailNode(v)}>{v}</a>
          {record.role === 'control-plane' || record.role === 'master' ? <Tag color="purple">控制面</Tag> : null}
        </Space>
      ),
    },
    { title: 'IP', dataIndex: 'ip', width: 125 },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (v: string, record) => (
        <Space size={4}>
          {statusTag(v)}
          {record.unschedulable && <Tooltip title="不可调度（cordoned）"><PauseCircleOutlined style={{ color: '#fa8c16' }} /></Tooltip>}
        </Space>
      ),
    },
    {
      title: 'CPU 用量', key: 'cpu', width: 150,
      render: (_, record) => {
        const m = metricsMap[record.name]
        return <UsageBar used={m?.cpu_millicores ?? 0} capacity={parseCpuToMillicores(record.cpu_capacity)} format={formatCpu} />
      },
    },
    {
      title: '内存用量', key: 'mem', width: 160,
      render: (_, record) => {
        const m = metricsMap[record.name]
        return <UsageBar used={m?.memory_bytes ?? 0} capacity={parseMemToBytes(record.mem_capacity)} format={formatBytes} />
      },
    },
    {
      title: '诊断', dataIndex: 'diagnosis', ellipsis: true,
      render: (v: string) => v || <Text type="secondary">-</Text>,
    },
    { title: '运行时长', dataIndex: 'age', width: 100 },
    {
      title: '操作', key: 'ops', width: 200, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          {record.unschedulable ? (
            <Button size="small" icon={<PlayCircleOutlined />} disabled={!canManage}
              loading={cordonMutation.isPending && cordonMutation.variables?.name === record.name}
              onClick={() => riskConfirm.confirm('clusters:uncordon', () => cordonMutation.mutate({ name: record.name, uncordon: true }))}>
              恢复调度
            </Button>
          ) : (
            <Button size="small" icon={<PauseCircleOutlined />} disabled={!canManage}
              loading={cordonMutation.isPending && cordonMutation.variables?.name === record.name}
              onClick={() => riskConfirm.confirm('clusters:cordon', () => cordonMutation.mutate({ name: record.name, uncordon: false }),
                `将把节点 ${record.name} 标记为不可调度：新 Pod 不再调度到该节点，已有 Pod 保持运行；如目的是下线节点，请改用「排空」`)}>
              停止调度
            </Button>
          )}
          <Button size="small" icon={<DragOutlined />} disabled={!canManage} onClick={() => setDrainNode(record)}>
            排空
          </Button>
          <Tooltip title="标签 / 污点">
            <Button size="small" type="text" icon={<TagsOutlined />} disabled={!canManage} onClick={() => setEditNodeMeta(record)} />
          </Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <Card
      size="small"
      title={
        !metrics?.available ? (
          <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>实时用量需集群安装 metrics-server</Text>
        ) : undefined
      }
      extra={<Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>}
    >
      <Table<K8sNode>
        rowKey="name"
        size="small"
        columns={columns}
        dataSource={nodes}
        loading={isLoading}
        rowClassName={(r) => focusRowClass(focus, 'Node', '', r.name)}
        pagination={false}
        scroll={{ x: 1080 }}
      />

      {/* 节点详情抽屉：Pod 分布 / taints / 分配率 */}
      <Drawer
        title={`节点详情 - ${detailNode || ''}`}
        open={!!detailNode}
        onClose={() => setDetailNode(null)}
        width={720}
      >
        {!nodeDetail ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="加载节点详情..." /></div>
        ) : (
          <NodeDetailContent detail={nodeDetail} />
        )}
      </Drawer>

      <NodeMetaEditor
        clusterId={clusterId}
        node={editNodeMeta}
        onClose={() => setEditNodeMeta(null)}
      />

      <Modal
        title={`排空节点 - ${drainNode?.name || ''}`}
        open={!!drainNode}
        onCancel={() => setDrainNode(null)}
        width={640}
        footer={[
          <Button key="cancel" onClick={() => setDrainNode(null)}>取消</Button>,
          <Button
            key="drain" type="primary" danger
            loading={drainMutation.isPending}
            disabled={loadingPreview || !drainPreview}
            onClick={() => riskConfirm.confirm('clusters:drain', () => drainMutation.mutate(drainNode!.name),
              `将驱逐节点 ${drainNode?.name} 上的 ${drainPreview?.total ?? 0} 个业务 Pod（DaemonSet 与镜像 Pod 跳过），驱逐期间相关服务可能只剩其余副本`)}
          >
            确认排空（{drainPreview?.total ?? '?'} 个 Pod）
          </Button>,
        ]}
      >
        <Alert
          type="warning" showIcon style={{ marginBottom: 12 }}
          message="排空会驱逐该节点上的全部业务 Pod（DaemonSet 与镜像 Pod 会被跳过），请确认业务有副本冗余。"
        />
        {loadingPreview ? (
          <div style={{ textAlign: 'center', padding: 24 }}><Spin tip="获取驱逐预览..." /></div>
        ) : (
          <Table
            rowKey={(r) => `${r.namespace}/${r.name}`}
            size="small"
            pagination={false}
            dataSource={drainPreview?.pods || []}
            columns={[
              { title: 'Namespace', dataIndex: 'namespace', width: 130 },
              { title: 'Pod', dataIndex: 'name', ellipsis: true },
              { title: '控制器', dataIndex: 'controller', width: 180, render: (v: string) => v || '-' },
            ]}
            locale={{ emptyText: <Empty description="该节点没有可驱逐的业务 Pod" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          />
        )}
      </Modal>
    </Card>
  )
}

// ───────────────────────── 节点详情结构化展示 ─────────────────────────

function NodeDetailContent({ detail }: { detail: K8sNodeDetail }) {
  const rows = detail && typeof detail === 'object' ? detail : {}
  // 后端节点详情含 pods 分布 / taints / 分配率，字段名以实际返回为准做兜底。
  const podList = rows.pods || rows.pod_distribution || []
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Descriptions
        size="small" column={2} bordered
        items={Object.entries(rows)
          .filter(([, v]) => typeof v !== 'object' || v === null)
          .slice(0, 16)
          .map(([k, v]) => ({ key: k, label: k, children: String(v ?? '-') }))}
      />
      {Array.isArray(podList) && podList.length > 0 && (
        <Card size="small" title={`Pod 分布（${podList.length}）`}>
          <Table
            rowKey={(r) => r.name || `${r.namespace}-${r.name}`}
            size="small"
            dataSource={podList}
            pagination={false}
            columns={[
              { title: 'Namespace', dataIndex: 'namespace', width: 130 },
              { title: 'Pod', dataIndex: 'name', ellipsis: true },
              { title: '状态', dataIndex: 'status', width: 110, render: (v: string) => statusTag(v) },
            ]}
          />
        </Card>
      )}
      {Array.isArray(rows.taints) && rows.taints.length > 0 && (
        <Card size="small" title="Taints">
          <Space wrap>
            {rows.taints.map((t, idx: number) => (
              <Tag key={idx} color="orange">{t.key}={t.value || ''}:{t.effect}</Tag>
            ))}
          </Space>
        </Card>
      )}
    </Space>
  )
}

// ───────────────────────── 节点标签 / 污点编辑 ─────────────────────────

function NodeMetaEditor({ clusterId, node, onClose }: { clusterId: string; node: K8sNode | null; onClose: () => void }) {
  const { message } = App.useApp()
  const [labelsText, setLabelsText] = useState('')
  const [taintsText, setTaintsText] = useState('')

  // 打开时从节点详情回填现有 labels/taints。
  const { data: detail } = useQuery({
    queryKey: ['cluster-node-meta', clusterId, node?.name],
    queryFn: () => clusterDetailApi.nodeDetail(clusterId, node!.name),
    enabled: !!node,
  })

  useEffect(() => {
    if (!detail) return
    // 回填延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => {
      const labels = detail.labels || {}
      setLabelsText(Object.entries(labels).map(([k, v]) => `${k}=${v}`).join('\n'))
      const taints = detail.taints || []
      setTaintsText(taints.map((t) => `${t.key}=${t.value || ''}:${t.effect}`).join('\n'))
    })
  }, [detail])

  const parseLabels = (text: string): Record<string, string | null> => {
    // 解析 k=v 行；"-key" 行表示删除该 key。
    const result: Record<string, string | null> = {}
    for (const line of text.split('\n')) {
      const row = line.trim()
      if (!row) continue
      if (row.startsWith('-')) {
        const key = row.slice(1).split('=')[0].trim()
        if (key) result[key] = null
        continue
      }
      const [k, v] = row.split('=')
      if (k?.trim()) result[k.trim()] = (v ?? '').trim()
    }
    return result
  }

  const parseTaints = (text: string): Array<{ key: string; value?: string; effect: string }> =>
    text.split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [kv, effect = 'NoSchedule'] = line.split(':')
        const [key, value] = kv.split('=')
        return { key: (key || '').trim(), value: (value || '').trim(), effect: (effect || '').trim() || 'NoSchedule' }
      })
      .filter((t) => t.key)

  const saveMutation = useMutation({
    // 标签与污点是两条独立 PATCH、对应后端两个守卫动作（patch_labels / patch_taints），
    // 票据一次性消费：各取各的票并随请求头显式携带，不依赖全局待发送槽位。
    mutationFn: async () => {
      const labelsTicket = await fetchRiskTicket('clusters:patch_labels')
      await clusterOpsApi.patchNodeLabels(clusterId, node!.name, parseLabels(labelsText), {
        headers: { 'X-Risk-Ticket': labelsTicket },
      })
      const taintsTicket = await fetchRiskTicket('clusters:patch_taints')
      await clusterOpsApi.patchNodeTaints(clusterId, node!.name, parseTaints(taintsText), {
        headers: { 'X-Risk-Ticket': taintsTicket },
      })
    },
    onSuccess: () => { message.success('节点标签 / 污点已更新'); onClose() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败（kubernetes.io 前缀标签受保护）'),
  })

  return (
    <Drawer
      title={`标签 / 污点 - ${node?.name || ''}`}
      open={!!node}
      onClose={onClose}
      width={560}
      extra={
        <Button type="primary" loading={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
          保存
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div>
          <Text strong style={{ display: 'block', marginBottom: 6 }}>标签（每行一个 k=v；-key 表示删除）</Text>
          <Input.TextArea
            rows={6}
            value={labelsText}
            onChange={(e) => setLabelsText(e.target.value)}
            placeholder={'team=backend\nenv=prod\n-old-label'}
            style={{ fontFamily: 'monospace', fontSize: 12 }}
          />
        </div>
        <div>
          <Text strong style={{ display: 'block', marginBottom: 6 }}>
            污点（每行 key=value:effect，全量替换；effect: NoSchedule / PreferNoSchedule / NoExecute）
          </Text>
          <Input.TextArea
            rows={4}
            value={taintsText}
            onChange={(e) => setTaintsText(e.target.value)}
            placeholder={'dedicated=gpu:NoSchedule\nnode-role.kubernetes.io/master=:NoSchedule'}
            style={{ fontFamily: 'monospace', fontSize: 12 }}
          />
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          kubernetes.io / k8s.io 前缀的标签受后端保护不可修改；污点影响调度，保存前请确认影响面。
        </Text>
      </Space>
    </Drawer>
  )
}
