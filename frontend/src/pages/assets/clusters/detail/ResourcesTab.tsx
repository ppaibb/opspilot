import { useState } from 'react'
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Alert, Drawer, Modal, App,
  Select, Tooltip, Empty, Spin, Input, Popconfirm,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, FileTextOutlined, DeploymentUnitOutlined, EyeOutlined,
  WarningOutlined, DeleteOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import { clusterDetailApi, clusterOpsApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import ResourceEditDrawer, { type EditTarget } from '@/components/k8s/ResourceEditDrawer'
import YamlPre from '@/components/k8s/YamlPre'
import type { K8sResourceItem, K8sResourceEvent } from '@/types/api'
import { statusTag } from './shared'
import { BROWSABLE_KINDS } from './types'
import { useUrlFilter, useUrlFocus, focusRowClass } from './useTabParams'

const { Text } = Typography

/** 后端单页上限 500，取 100 兼顾首屏速度与翻页次数。 */
const RESOURCE_PAGE_SIZE = 100

// ───────────────────────── 资源浏览 Tab ─────────────────────────

export default function ResourcesTab({ clusterId, namespaces }: { clusterId: string; namespaces: string[] }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.assets.clusters.manage')

  // kind / ns 入 URL：搜索跳转带参数预填并高亮目标行，刷新不丢。
  const [kind, setKind] = useUrlFilter('kind', 'Service')
  const [namespace, setNamespace] = useUrlFilter('ns', '_all')
  const [yamlView, setYamlView] = useState<K8sResourceItem | null>(null)
  const [eventsView, setEventsView] = useState<K8sResourceItem | null>(null)
  const [resizeTarget, setResizeTarget] = useState<K8sResourceItem | null>(null)
  const [newSize, setNewSize] = useState('10Gi')
  const [relatedView, setRelatedView] = useState<K8sResourceItem | null>(null)
  // YAML 编辑模式：编辑态保存修改后的文本，保存走 server-side apply（create-or-update）。
  const [yamlEditing, setYamlEditing] = useState(false)
  const [yamlDraft, setYamlDraft] = useState('')
  // 可视化编辑目标（KubeSphere 式表单编辑，支持 Deployment/StatefulSet/DaemonSet/Service/ConfigMap）。
  const [editTarget, setEditTarget] = useState<EditTarget | null>(null)

  // 后端 continue_token 分页：大集群不再被首屏 200 条截断，「加载更多」继续翻。
  const nsParam = namespace === '__none__' ? '_all' : namespace
  const {
    data, isLoading, isFetching, refetch, fetchNextPage, hasNextPage, isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['cluster-resources', clusterId, kind, nsParam],
    queryFn: ({ pageParam }) =>
      clusterDetailApi.resourcesPage(clusterId, kind, nsParam, RESOURCE_PAGE_SIZE, pageParam),
    initialPageParam: '',
    getNextPageParam: (last) => last.continue_token || undefined,
    placeholderData: (prev) => prev,
  })
  const resources = (data?.pages.flatMap((p) => p.items) ?? [])
    .filter((i) => (namespace === '__none__' ? !i.namespace : true))

  const { data: yaml } = useQuery({
    queryKey: ['cluster-resource-yaml', clusterId, yamlView?.namespace, kind, yamlView?.name],
    queryFn: () => clusterDetailApi.resourceYaml(clusterId, yamlView!.namespace || 'default', kind, yamlView!.name),
    enabled: !!yamlView,
  })

  const { data: events = [] } = useQuery({
    queryKey: ['cluster-resource-events', clusterId, eventsView?.namespace, kind, eventsView?.name],
    queryFn: () => clusterDetailApi.resourceEvents(clusterId, eventsView!.namespace || 'default', kind, eventsView!.name),
    enabled: !!eventsView,
  })

  const deleteMutation = useMutation({
    mutationFn: (item: K8sResourceItem) =>
      clusterDetailApi.deleteResource(clusterId, item.namespace || 'default', kind, item.name),
    onSuccess: () => { message.success('资源已删除'); queryClient.invalidateQueries({ queryKey: ['cluster-resources', clusterId] }) },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  /** YAML 编辑保存：对现有资源 server-side apply（create-or-update）。 */
  const applyYamlMutation = useMutation({
    mutationFn: () => clusterOpsApi.apply(clusterId, yamlDraft),
    onSuccess: () => {
      message.success('YAML 已应用（server-side apply）')
      setYamlEditing(false)
      queryClient.invalidateQueries({ queryKey: ['cluster-resource-yaml', clusterId] })
      refetch()
    },
    onError: (error: unknown) => {
      // detail 多为字符串；对象型（如校验错误）尽量抽出 message
      const data = axios.isAxiosError(error)
        ? (error.response?.data as { detail?: string | { message?: string } } | undefined)
        : undefined
      const detail = data?.detail
      message.error(typeof detail === 'string' ? detail : detail?.message || '应用失败（dry-run 未通过或字段冲突）')
    },
  })

  const resizeMutation = useMutation({
    mutationFn: () => clusterOpsApi.resizePvc(clusterId, resizeTarget!.namespace || 'default', resizeTarget!.name, newSize),
    onSuccess: () => { message.success('扩容请求已提交（需 StorageClass 支持 Expand）'); setResizeTarget(null); refetch() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '扩容失败'),
  })

  const { data: related } = useQuery({
    queryKey: ['cluster-resource-related', clusterId, kind, relatedView?.namespace, relatedView?.name],
    queryFn: () => clusterOpsApi.resourceRelated(clusterId, relatedView!.namespace || 'default', kind, relatedView!.name),
    enabled: !!relatedView,
  })

  const { data: tlsStatus } = useQuery({
    queryKey: ['cluster-ingress-tls', clusterId, kind, eventsView?.namespace, eventsView?.name],
    queryFn: () => clusterOpsApi.ingressTlsStatus(clusterId, eventsView!.namespace || 'default', eventsView!.name),
    enabled: kind === 'Ingress' && !!eventsView,
  })

  // 表单编辑支持的 Kind（与后端 _resource_edit_payload 的 fields 矩阵一致）。
  const VISUAL_EDIT_KINDS = ['Deployment', 'StatefulSet', 'DaemonSet', 'Service', 'ConfigMap']

  const focus = useUrlFocus()

  const columns: ColumnsType<K8sResourceItem> = [
    {
      title: '名称', dataIndex: 'name', width: 260, ellipsis: true,
      render: (v: string, record) => (
        VISUAL_EDIT_KINDS.includes(kind) ? (
          <Tooltip title="点击可视化编辑">
            <a onClick={() => setEditTarget({ clusterId, namespace: record.namespace || '', kind, name: v })}>{v}</a>
          </Tooltip>
        ) : (
          <Text strong>{v}</Text>
        )
      ),
    },
    {
      title: 'Namespace', dataIndex: 'namespace', width: 140,
      render: (v: string) => v || <Text type="secondary">集群级</Text>,
    },
    { title: '状态', dataIndex: 'status', width: 115, render: (v: string) => (v ? statusTag(v) : '-') },
    { title: '详情', dataIndex: 'detail', ellipsis: true, render: (v: string) => v || '-' },
    { title: '创建', dataIndex: 'age', width: 105 },
    {
      title: '操作', key: 'ops', width: 160, fixed: 'right',
      render: (_, record) => (
        <Space size={2}>
          <Tooltip title="查看 YAML">
            <Button size="small" type="text" icon={<FileTextOutlined />} onClick={() => setYamlView(record)} />
          </Tooltip>
          {kind === 'PVC' && (
            <Tooltip title="扩容">
              <Button size="small" type="text" icon={<DeploymentUnitOutlined />}
                disabled={!canManage} onClick={() => { setResizeTarget(record); setNewSize('10Gi') }} />
            </Tooltip>
          )}
          <Tooltip title={kind === 'Ingress' ? 'TLS / 事件' : '关联资源'}>
            <Button size="small" type="text" icon={<EyeOutlined />} onClick={() => setRelatedView(record)} />
          </Tooltip>
          <Tooltip title="事件">
            <Button size="small" type="text" icon={<WarningOutlined />} onClick={() => setEventsView(record)} />
          </Tooltip>
          <Popconfirm
            title={`删除 ${record.name}？`} description="删除后不可恢复。"
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => riskConfirm.confirm('clusters:delete', () => deleteMutation.mutate(record),
              `将删除 ${kind} ${namespace === '_all' ? '' : `${namespace}/`}${record.name}，删除后不可恢复`)}
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
          <Select value={kind} onChange={setKind} style={{ width: 170 }}
            options={BROWSABLE_KINDS.map((k) => ({ value: k, label: k }))} />
          <Select
            value={namespace} onChange={setNamespace} style={{ width: 170 }} showSearch
            options={[
              { value: '_all', label: '全部' },
              ...namespaces.map((ns) => ({ value: ns, label: ns })),
              { value: '__none__', label: '仅集群级资源' },
            ]}
          />
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => refetch()} loading={isFetching}>刷新</Button>
      </Flex>
      <Table<K8sResourceItem>
        rowKey={(r) => `${r.namespace || ''}/${r.name}`}
        size="small"
        columns={columns}
        dataSource={resources}
        loading={isLoading}
        rowClassName={(r) => focusRowClass(focus, kind, r.namespace || '', r.name)}
        pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 个` }}
        scroll={{ x: 940 }}
      />
      {hasNextPage && (
        <Flex justify="center" style={{ marginTop: 10 }}>
          <Button loading={isFetchingNextPage} onClick={() => fetchNextPage()}>
            加载更多（每页 {RESOURCE_PAGE_SIZE} 条）
          </Button>
        </Flex>
      )}

      <Drawer
        title={
          <Space>
            <span>YAML - {yamlView?.name || ''}</span>
            {yamlEditing && <Tag color="processing">编辑中</Tag>}
          </Space>
        }
        open={!!yamlView}
        onClose={() => { setYamlView(null); setYamlEditing(false) }}
        width={760}
        extra={
          yamlView && !yamlEditing ? (
            <Button size="small" type="primary" ghost disabled={!canManage}
              onClick={() => { setYamlDraft(typeof yaml === 'string' ? yaml : JSON.stringify(yaml, null, 2)); setYamlEditing(true) }}>
              编辑
            </Button>
          ) : yamlEditing ? (
            <Space>
              <Button size="small" onClick={() => setYamlEditing(false)}>放弃</Button>
              <Button size="small" type="primary" danger loading={applyYamlMutation.isPending}
                onClick={() => riskConfirm.confirm('clusters:apply', () => applyYamlMutation.mutate())}>
                保存并应用
              </Button>
            </Space>
          ) : null
        }
      >
        {yamlView && yaml === undefined ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="加载 YAML..." /></div>
        ) : yamlEditing ? (
          <>
            <Alert
              type="warning" showIcon style={{ marginBottom: 8 }}
              message="保存将通过 server-side apply 更新该资源（create-or-update 语义）；字段冲突会被 API 拒绝。"
            />
            <Input.TextArea
              value={yamlDraft}
              onChange={(e) => setYamlDraft(e.target.value)}
              rows={22}
              spellCheck={false}
              style={{ fontFamily: 'ui-monospace, SFMono-Regular, monospace', fontSize: 12 }}
            />
          </>
        ) : (
          <YamlPre
            text={typeof yaml === 'string' ? yaml : yaml?.yaml || yaml?.content || JSON.stringify(yaml, null, 2)}
          />
        )}
      </Drawer>

      <Modal
        title={`PVC 扩容 - ${resizeTarget?.name || ''}`}
        open={!!resizeTarget}
        onCancel={() => setResizeTarget(null)}
        // 后端 resize_pvc 守卫动作，需确认 + 一次性票据
        onOk={() => riskConfirm.confirm('clusters:resize_pvc', () => resizeMutation.mutate())}
        okText="提交扩容" okButtonProps={{ danger: true }} cancelText="取消"
        confirmLoading={resizeMutation.isPending}
      >
        <Alert
          type="warning" showIcon style={{ marginBottom: 12 }}
          message="仅可扩大不可缩小；需要 StorageClass 开启 allowVolumeExpansion。"
        />
        <Space>
          <Text strong>新容量：</Text>
          <Input value={newSize} onChange={(e) => setNewSize(e.target.value)} placeholder="10Gi" style={{ width: 140, fontFamily: 'monospace' }} />
        </Space>
      </Modal>

      <Drawer
        title={`关联资源 - ${relatedView?.name || ''}`}
        open={!!relatedView}
        onClose={() => setRelatedView(null)}
        width={720}
      >
        {related ? (
          <YamlPre text={JSON.stringify(related, null, 2)} />
        ) : (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="加载关联资源..." /></div>
        )}
      </Drawer>

      <ResourceEditDrawer
        target={editTarget}
        onClose={() => setEditTarget(null)}
      />

      <Drawer
        title={`事件${kind === 'Ingress' ? ' / TLS 状态' : ''} - ${eventsView?.name || ''}`}
        open={!!eventsView}
        onClose={() => setEventsView(null)}
        width={720}
      >
        {kind === 'Ingress' && tlsStatus && (
          <Alert
            style={{ marginBottom: 12 }}
            type={tlsStatus.status === 'valid' || tlsStatus.status === 'Valid' ? 'success' : 'warning'}
            showIcon
            message={`TLS：${tlsStatus.secret_name || '无 Secret'} · ${tlsStatus.days_remaining ?? '?'} 天后到期`}
            description={Array.isArray(tlsStatus.hosts) ? (tlsStatus.hosts as string[]).join('、') : ''}
          />
        )}
        {events.length === 0 ? (
          <Empty description="暂无关联事件" style={{ padding: '40px 0' }} />
        ) : (
          (events as K8sResourceEvent[]).map((ev, idx) => (
            <Flex key={idx} gap={8} align="flex-start" style={{ padding: '6px 0', borderBottom: '1px solid #f5f5f5' }}>
              <Tag color={ev.type === 'Warning' ? 'orange' : 'blue'} style={{ flexShrink: 0 }}>{ev.type || 'Normal'}</Tag>
              <Space direction="vertical" size={0} style={{ flex: 1, minWidth: 0 }}>
                <Text strong style={{ fontSize: 12 }}>{ev.reason} {ev.count > 1 ? `×${ev.count}` : ''}</Text>
                <Text style={{ fontSize: 12, wordBreak: 'break-all' }}>{ev.message}</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>{ev.last_timestamp}</Text>
              </Space>
            </Flex>
          ))
        )}
      </Drawer>
    </Card>
  )
}
