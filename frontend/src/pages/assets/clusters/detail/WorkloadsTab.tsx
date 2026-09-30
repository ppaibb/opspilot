import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Drawer, Modal, App,
  InputNumber, Select, Tooltip, Empty, Spin, Input, Descriptions, Popconfirm,
  Progress, Form,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ReloadOutlined, SyncOutlined, RollbackOutlined, FileTextOutlined,
  DeleteOutlined, NodeIndexOutlined, RocketOutlined, DeploymentUnitOutlined,
  EyeOutlined, ClockCircleOutlined, PartitionOutlined, SendOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import { clusterDetailApi, clusterOpsApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import ResourceEditDrawer, { type EditTarget } from '@/components/k8s/ResourceEditDrawer'
import type { K8sWorkload } from '@/types/api'
import { statusTag } from './shared'

const { Text } = Typography

const WORKLOAD_KIND_COLOR: Record<string, string> = {
  Deployment: 'blue', StatefulSet: 'purple', DaemonSet: 'cyan',
  ReplicaSet: 'geekblue', Job: 'orange', CronJob: 'gold',
}

// ───────────────────────── 工作负载 Tab ─────────────────────────

export default function WorkloadsTab({ clusterId, namespaces }: { clusterId: string; namespaces: string[] }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.assets.clusters.manage')

  const [kindFilter, setKindFilter] = useState<string>('_all')
  const [nsFilter, setNsFilter] = useState('_all')
  const [search, setSearch] = useState('')
  const [scaleTarget, setScaleTarget] = useState<K8sWorkload | null>(null)
  const [scaleReplicas, setScaleReplicas] = useState(1)
  const [historyTarget, setHistoryTarget] = useState<K8sWorkload | null>(null)
  const [yamlTarget, setYamlTarget] = useState<K8sWorkload | null>(null)
  const [imageTarget, setImageTarget] = useState<K8sWorkload | null>(null)
  const [editTarget, setEditTarget] = useState<EditTarget | null>(null)
  const [imageValues, setImageValues] = useState<Record<string, string>>({})
  const [rolloutTarget, setRolloutTarget] = useState<K8sWorkload | null>(null)
  const [cronJobsTarget, setCronJobsTarget] = useState<K8sWorkload | null>(null)

  const { data: workloads = [], isLoading, refetch } = useQuery({
    queryKey: ['cluster-workloads', clusterId, nsFilter],
    queryFn: () => clusterDetailApi.workloads(clusterId, nsFilter),
    refetchInterval: 60 * 1000,
  })

  const { data: history = [] } = useQuery({
    queryKey: ['cluster-rollout-history', clusterId, historyTarget],
    queryFn: () => clusterDetailApi.rolloutHistory(clusterId, historyTarget!.kind, historyTarget!.namespace, historyTarget!.name),
    enabled: !!historyTarget,
  })

  /** 指定修订回滚（后端支持 {revision}；不传则回滚上一版）。 */
  const undoRevisionMutation = useMutation({
    mutationFn: (revision: number) =>
      clusterDetailApi.rolloutUndo(clusterId, historyTarget!.kind, historyTarget!.namespace, historyTarget!.name, { revision }),
    onSuccess: (_d, revision) => {
      message.success(`已提交回滚到修订 #${revision}`)
      setHistoryTarget(null)
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '回滚失败'),
  })

  const { data: yaml } = useQuery({
    queryKey: ['cluster-workload-yaml', clusterId, yamlTarget],
    queryFn: () => clusterDetailApi.resourceYaml(clusterId, yamlTarget!.namespace, yamlTarget!.kind, yamlTarget!.name),
    enabled: !!yamlTarget,
  })

  const { data: rolloutStatus, isLoading: loadingRollout } = useQuery({
    queryKey: ['cluster-rollout-status', clusterId, rolloutTarget],
    queryFn: () => clusterOpsApi.rolloutStatus(clusterId, rolloutTarget!.kind, rolloutTarget!.namespace, rolloutTarget!.name),
    enabled: !!rolloutTarget,
    refetchInterval: () => (rolloutTarget ? 5000 : false),
  })

  const { data: cronJobs = [] } = useQuery({
    queryKey: ['cluster-cronjob-jobs', clusterId, cronJobsTarget],
    queryFn: () => clusterOpsApi.cronjobJobs(clusterId, cronJobsTarget!.namespace, cronJobsTarget!.name),
    enabled: !!cronJobsTarget,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cluster-workloads', clusterId] })

  const scaleMutation = useMutation({
    mutationFn: () => clusterDetailApi.scaleWorkload(clusterId, scaleTarget!.kind, scaleTarget!.namespace, scaleTarget!.name, scaleReplicas),
    onSuccess: () => { message.success('扩缩容已提交'); setScaleTarget(null); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '扩缩容失败'),
  })
  const restartMutation = useMutation({
    mutationFn: (w: K8sWorkload) => clusterDetailApi.restartWorkload(clusterId, w.kind, w.namespace, w.name),
    onSuccess: () => { message.success('滚动重启已提交'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '重启失败'),
  })
  const undoMutation = useMutation({
    mutationFn: (w: K8sWorkload) => clusterDetailApi.rolloutUndo(clusterId, w.kind, w.namespace, w.name),
    onSuccess: () => { message.success('回滚已提交'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '回滚失败'),
  })
  const deleteMutation = useMutation({
    mutationFn: (w: K8sWorkload) => clusterDetailApi.deleteWorkload(clusterId, w.kind, w.namespace, w.name),
    onSuccess: () => { message.success('工作负载已删除'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const scalable = (w: K8sWorkload) => ['Deployment', 'StatefulSet', 'ReplicaSet'].includes(w.kind)

  const setImageMutation = useMutation({
    mutationFn: () => clusterOpsApi.setWorkloadImage(
      clusterId, imageTarget!.kind, imageTarget!.namespace, imageTarget!.name,
      Object.entries(imageValues).filter(([, img]) => img && img !== imageTarget?.image)
        .map(([name, image]) => ({ name, image })),
    ),
    onSuccess: () => { message.success('镜像更新已提交（滚动发布）'); setImageTarget(null); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '镜像更新失败'),
  })

  const pauseMutation = useMutation({
    mutationFn: ({ w, paused }: { w: K8sWorkload; paused: boolean }) =>
      clusterOpsApi.toggleWorkloadPause(clusterId, w.kind, w.namespace, w.name, paused),
    onSuccess: (_d, vars) => { message.success(vars.paused ? '已暂停（副本缩到 0 并记忆）' : '已恢复'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '操作失败'),
  })

  const triggerCronMutation = useMutation({
    mutationFn: (w: K8sWorkload) => clusterOpsApi.triggerCronjob(clusterId, w.namespace, w.name),
    onSuccess: () => { message.success('CronJob 已手动触发一次') },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '触发失败'),
  })

  const suspendCronMutation = useMutation({
    mutationFn: ({ w, suspend }: { w: K8sWorkload; suspend: boolean }) =>
      clusterOpsApi.suspendCronjob(clusterId, w.namespace, w.name, suspend),
    onSuccess: (_d, vars) => { message.success(vars.suspend ? 'CronJob 已挂起' : 'CronJob 已恢复调度'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '操作失败'),
  })

  const filtered = workloads.filter((w) => {
    if (kindFilter !== '_all' && w.kind !== kindFilter) return false
    if (search && !`${w.namespace}/${w.name}`.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const columns: ColumnsType<K8sWorkload> = [
    {
      title: '名称', dataIndex: 'name', width: 240,
      render: (v: string, record) => (
        <Space size={6}>
          <Tag color={WORKLOAD_KIND_COLOR[record.kind] || 'default'}>{record.kind}</Tag>
          <Tooltip title="点击可视化编辑（副本/镜像/标签）">
            <a onClick={() => setEditTarget({ clusterId, namespace: record.namespace, kind: record.kind, name: v })}>{v}</a>
          </Tooltip>
        </Space>
      ),
    },
    { title: 'Namespace', dataIndex: 'namespace', width: 130 },
    { title: 'Ready', dataIndex: 'ready', width: 80 },
    { title: '状态', dataIndex: 'status', width: 100, render: (v: string) => statusTag(v) },
    {
      title: '诊断', dataIndex: 'diagnosis', ellipsis: true,
      render: (v: string) => v || <Text type="secondary">-</Text>,
    },
    {
      title: '镜像', dataIndex: 'image', width: 200, ellipsis: true,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v || '-'}</Text>,
    },
    { title: '创建', dataIndex: 'age', width: 100 },
    {
      title: '操作', key: 'ops', width: 330, fixed: 'right',
      render: (_, record) => (
        <Space size={2}>
          <Tooltip title="扩缩容">
            <Button size="small" type="text" icon={<NodeIndexOutlined />}
              disabled={!canManage || !scalable(record)}
              onClick={() => { setScaleTarget(record); setScaleReplicas(parseInt(record.ready) || 1) }} />
          </Tooltip>
          <Tooltip title="滚动重启">
            <Button size="small" type="text" icon={<SyncOutlined />}
              disabled={!canManage || !scalable(record)}
              onClick={() => riskConfirm.confirm('clusters:restart', () => restartMutation.mutate(record))} />
          </Tooltip>
          <Tooltip title="回滚上一版本">
            <Button size="small" type="text" icon={<RollbackOutlined />}
              disabled={!canManage || !scalable(record)}
              onClick={() => riskConfirm.confirm('clusters:rollout-undo', () => undoMutation.mutate(record))} />
          </Tooltip>
          <Tooltip title="发布历史">
            <Button size="small" type="text" icon={<RocketOutlined />}
              disabled={!scalable(record)} onClick={() => setHistoryTarget(record)} />
          </Tooltip>
          <Tooltip title="修改镜像（滚动发布）">
            <Button size="small" type="text" icon={<DeploymentUnitOutlined />}
              disabled={!canManage || !scalable(record)}
              onClick={() => { setImageTarget(record); setImageValues({ main: record.image || '' }) }} />
          </Tooltip>
          <Tooltip title={record.status === 'Suspended' || record.status === 'Paused' ? '恢复调度' : '暂停（缩到 0 副本）'}>
            <Button size="small" type="text" icon={<PartitionOutlined />}
              disabled={!canManage || !scalable(record)}
              onClick={() => riskConfirm.confirm('clusters:pause', () =>
                pauseMutation.mutate({ w: record, paused: !(record.status === 'Suspended' || record.status === 'Paused') }))} />
          </Tooltip>
          <Tooltip title="滚动发布状态">
            <Button size="small" type="text" icon={<EyeOutlined />}
              disabled={!scalable(record)} onClick={() => setRolloutTarget(record)} />
          </Tooltip>
          <Tooltip title="查看 YAML">
            <Button size="small" type="text" icon={<FileTextOutlined />} onClick={() => setYamlTarget(record)} />
          </Tooltip>
          {record.kind === 'CronJob' && (
            <>
              <Tooltip title="手动触发一次">
                <Button size="small" type="text" icon={<SendOutlined />}
                  disabled={!canManage} onClick={() => riskConfirm.confirm('clusters:trigger_cronjob', () => triggerCronMutation.mutate(record))} />
              </Tooltip>
              <Tooltip title={record.status === 'Suspended' ? '恢复调度' : '挂起'}>
                <Button size="small" type="text" icon={<ClockCircleOutlined />}
                  disabled={!canManage}
                  onClick={() => riskConfirm.confirm('clusters:suspend_cronjob', () =>
                    suspendCronMutation.mutate({ w: record, suspend: record.status !== 'Suspended' }))} />
              </Tooltip>
              <Tooltip title="执行记录（Jobs）">
                <Button size="small" type="text" icon={<ExperimentOutlined />} onClick={() => setCronJobsTarget(record)} />
              </Tooltip>
            </>
          )}
          <Popconfirm
            title={`删除 ${record.namespace}/${record.name}？`}
            description="该工作负载及其 Pod 将被删除，不可恢复。"
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => riskConfirm.confirm('clusters:delete', () => deleteMutation.mutate(record))}
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
            value={nsFilter} onChange={setNsFilter} style={{ width: 165 }} showSearch
            options={[{ value: '_all', label: '全部 Namespace' }, ...namespaces.map((ns) => ({ value: ns, label: ns }))]}
          />
          <Select
            value={kindFilter} onChange={setKindFilter} style={{ width: 140 }}
            options={[
              { value: '_all', label: '全部类型' },
              ...Object.keys(WORKLOAD_KIND_COLOR).map((k) => ({ value: k, label: k })),
            ]}
          />
          <Input.Search
            placeholder="搜索名称" allowClear style={{ width: 180 }}
            value={search} onChange={(e) => setSearch(e.target.value)}
          />
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
      </Flex>
      <Table<K8sWorkload>
        rowKey={(r) => `${r.kind}/${r.namespace}/${r.name}`}
        size="small"
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        pagination={{ pageSize: 15, showSizeChanger: false, showTotal: (t) => `共 ${t} 个` }}
        scroll={{ x: 1150 }}
      />

      <Modal
        title={`修改镜像 - ${imageTarget?.name || ''}`}
        open={!!imageTarget}
        onCancel={() => setImageTarget(null)}
        // 后端 set-image 守卫动作，需确认 + 一次性票据
        onOk={() => riskConfirm.confirm('clusters:set-image', () => setImageMutation.mutate())}
        okText="提交滚动发布" cancelText="取消"
        confirmLoading={setImageMutation.isPending}
      >
        <Descriptions size="small" column={1} style={{ marginBottom: 12 }}>
          <Descriptions.Item label="类型">{imageTarget?.kind} / {imageTarget?.namespace}</Descriptions.Item>
          <Descriptions.Item label="当前镜像"><Text code>{imageTarget?.image || '-'}</Text></Descriptions.Item>
        </Descriptions>
        <Form layout="vertical">
          <Form.Item label="新镜像" help="提交后将触发滚动发布，可在操作列「发布状态」跟踪进度">
            <Input
              value={imageValues.main || ''}
              onChange={(e) => setImageValues((prev) => ({ ...prev, main: e.target.value }))}
              placeholder="nginx:1.27-alpine"
              style={{ fontFamily: 'monospace' }}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={`滚动发布状态 - ${rolloutTarget?.name || ''}`}
        open={!!rolloutTarget}
        onClose={() => setRolloutTarget(null)}
        width={560}
      >
        {loadingRollout ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="查询中..." /></div>
        ) : rolloutStatus ? (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {(() => {
              // 后端契约：desired_replicas / ready_replicas / rollout_status / rollout_message
              // （此前读 done/total 等不存在的字段，进度条恒绿是错的）
              const status = rolloutStatus
              const desired = status.desired_replicas ?? 0
              const ready = status.ready_replicas ?? 0
              const pct = desired ? Math.round((Math.min(ready, desired) / desired) * 100) : 0
              const barStatus = status.rollout_status === 'completed' ? 'success' as const
                : status.rollout_status === 'progressing' ? 'active' as const
                  : status.rollout_status === 'stopped' ? 'normal' as const
                    : 'exception' as const
              return (
                <>
                  <Progress percent={pct} status={barStatus} />
                  {status.rollout_message && (
                    <Text type="secondary" style={{ fontSize: 12 }}>{status.rollout_message}</Text>
                  )}
                  <Descriptions size="small" column={1}>
                    {Object.entries(status)
                      .filter(([, v]) => typeof v !== 'object')
                      .slice(0, 10)
                      .map(([k, v]) => (
                        <Descriptions.Item key={k} label={k}><Text>{String(v)}</Text></Descriptions.Item>
                      ))}
                  </Descriptions>
                  <Text type="secondary" style={{ fontSize: 12 }}>每 5 秒自动刷新，关闭抽屉即停止</Text>
                </>
              )
            })()}
          </Space>
        ) : (
          <Empty description="暂无发布状态" />
        )}
      </Drawer>

      <Drawer
        title={`CronJob 执行记录 - ${cronJobsTarget?.name || ''}`}
        open={!!cronJobsTarget}
        onClose={() => setCronJobsTarget(null)}
        width={680}
      >
        {cronJobs.length > 0 ? (
          <Table
            rowKey={(r) => r.name || `${r.namespace}-${r.start_time}`}
            size="small"
            dataSource={cronJobs}
            pagination={false}
            columns={[
              { title: 'Job', dataIndex: 'name', ellipsis: true },
              { title: '状态', dataIndex: 'status', width: 100, render: (v: string) => statusTag(v) },
              { title: '完成/期望', dataIndex: 'ready', width: 100, render: (v: string) => v || '-' },
              { title: '开始时间', dataIndex: 'age', width: 120, render: (v: string) => v || '-' },
            ]}
          />
        ) : (
          <Empty description="暂无执行记录（可在操作列「触发」手动执行一次）" style={{ padding: '40px 0' }} />
        )}
      </Drawer>

      <Modal
        title={`扩缩容 - ${scaleTarget?.name || ''}`}
        open={!!scaleTarget}
        onCancel={() => setScaleTarget(null)}
        // 后端 scale 守卫动作是 edit（非 delete），确认键与之对齐
        onOk={() => riskConfirm.confirm('clusters:edit', () => scaleMutation.mutate())}
        okText="提交" cancelText="取消"
        confirmLoading={scaleMutation.isPending}
      >
        <Descriptions size="small" column={1} style={{ marginBottom: 12 }}>
          <Descriptions.Item label="类型">{scaleTarget?.kind}</Descriptions.Item>
          <Descriptions.Item label="当前 Ready">{scaleTarget?.ready}</Descriptions.Item>
        </Descriptions>
        <Space align="center" size="middle">
          <Button onClick={() => setScaleReplicas((v) => Math.max(0, v - 1))}>−</Button>
          <InputNumber min={0} max={200} value={scaleReplicas} onChange={(v) => setScaleReplicas(v || 0)} style={{ width: 90 }} />
          <Button onClick={() => setScaleReplicas((v) => Math.min(200, v + 1))}>＋</Button>
          <Text type="secondary">个副本</Text>
        </Space>
      </Modal>

      <Drawer
        title={`发布历史 - ${historyTarget?.name || ''}`}
        open={!!historyTarget}
        onClose={() => setHistoryTarget(null)}
        width={640}
      >
        {Array.isArray(history) && history.length > 0 ? (
          history.map((rev, idx) => (
            <Card
              key={idx}
              size="small"
              style={{ marginBottom: 8 }}
              title={
                <Flex justify="space-between" align="center">
                  <Text strong>{rev.revision != null ? `修订 #${rev.revision}` : `记录 #${idx + 1}`}</Text>
                  {canManage && rev.revision != null && (
                    <Popconfirm
                      title={`回滚到修订 #${rev.revision}？`}
                      description="将触发一次新的滚动发布到该版本的模板。"
                      okText="回滚" okButtonProps={{ danger: true }} cancelText="取消"
                      onConfirm={() => riskConfirm.confirm('clusters:rollout-undo', () => undoRevisionMutation.mutate(rev.revision as number))}
                    >
                      <Button size="small" type="link" icon={<RollbackOutlined />}>回滚到此版本</Button>
                    </Popconfirm>
                  )}
                </Flex>
              }
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                {rev.image || rev.change_cause || JSON.stringify(rev).slice(0, 120)}
              </Text>
            </Card>
          ))
        ) : (
          <Empty description="暂无发布历史" style={{ padding: '40px 0' }} />
        )}
      </Drawer>

      <ResourceEditDrawer
        target={editTarget}
        onClose={() => setEditTarget(null)}
      />

      <Drawer
        title={`YAML - ${yamlTarget ? `${yamlTarget.kind}/${yamlTarget.name}` : ''}`}
        open={!!yamlTarget}
        onClose={() => setYamlTarget(null)}
        width={720}
      >
        {yamlTarget && yaml === undefined ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="加载 YAML..." /></div>
        ) : (
          <pre style={{
            background: '#fafafa', padding: 12, borderRadius: 6, fontSize: 12,
            whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: '75vh', overflow: 'auto',
          }}>
            {typeof yaml === 'string' ? yaml : yaml?.yaml || yaml?.content || JSON.stringify(yaml, null, 2)}
          </pre>
        )}
      </Drawer>
    </Card>
  )
}
