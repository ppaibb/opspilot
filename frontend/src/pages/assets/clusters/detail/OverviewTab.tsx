import type { ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Typography, Flex, Row, Col, Alert, App,
  Tooltip, Badge, Empty, Progress,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  SyncOutlined, WarningOutlined, CheckCircleOutlined,
  CloudServerOutlined, DesktopOutlined, AlertOutlined,
} from '@ant-design/icons'
import { clusterDetailApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type {
  ClusterResponse, K8sComponentHealth, K8sHealthIssue,
} from '@/types/api'
import { focusValue } from './useTabParams'

const { Text } = Typography

/** 搜索/下钻共用的负载类 Kind。 */
const WORKLOAD_KINDS = ['Deployment', 'StatefulSet', 'DaemonSet']

/** 诊断问题 → 目标 Tab 的 URL 定位参数；无法定位（无资源名）返回 null。 */
function issueLocate(issue: K8sHealthIssue): Record<string, string> | null {
  const ns = issue.namespace || ''
  const name = issue.name || ''
  if (!name) return null
  if (issue.kind === 'Pod') return { ns, q: name, focus: focusValue('Pod', ns, name) }
  if (issue.kind && WORKLOAD_KINDS.includes(issue.kind)) {
    return { ns, kind: issue.kind, q: name, focus: focusValue(issue.kind, ns, name) }
  }
  if (issue.kind === 'Node') return { focus: focusValue('Node', '', name) }
  if (issue.kind) return { ns, kind: issue.kind, focus: focusValue(issue.kind, ns, name) }
  return null
}

/** 诊断问题类别 → 目标 Tab。 */
function issueTab(issue: K8sHealthIssue): string {
  if (issue.kind === 'Pod') return 'pods'
  if (issue.kind === 'Node') return 'nodes'
  if (issue.kind && WORKLOAD_KINDS.includes(issue.kind)) return 'workloads'
  return 'resources'
}

// ───────────────────────── 概览 Tab ─────────────────────────

export default function OverviewTab({ cluster, onGoTab }: {
  cluster: ClusterResponse
  /** 带定位参数跳 Tab：?tab=x&ns&kind&q&focus。 */
  onGoTab: (tab: string, locate?: Record<string, string>) => void
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.assets.clusters.manage')

  const { data: health, isLoading: loadingHealth } = useQuery({
    queryKey: ['cluster-health', cluster.cluster_id],
    queryFn: () => clusterDetailApi.healthSummary(cluster.cluster_id),
    refetchInterval: 60 * 1000,
  })

  const { data: components = [], isLoading: loadingComponents } = useQuery({
    queryKey: ['cluster-components', cluster.cluster_id],
    queryFn: () => clusterDetailApi.componentsHealth(cluster.cluster_id),
    refetchInterval: 60 * 1000,
  })

  const refreshMutation = useMutation({
    mutationFn: () => clusterDetailApi.refresh(cluster.cluster_id),
    onSuccess: (updated) => {
      message.success('集群信息已刷新同步')
      queryClient.invalidateQueries({ queryKey: ['clusters'] })
      queryClient.invalidateQueries({ queryKey: ['cluster-health', cluster.cluster_id] })
      queryClient.setQueryData(['cluster-detail', cluster.cluster_id], updated)
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '刷新失败'),
  })

  const nodeStats = health?.node_stats
  const podStats = health?.pod_stats
  const wlStats = health?.workload_stats
  // 后端 workload_stats 按 Kind 分组，每个 Kind 是 {total, degraded} 对象而非数字。
  const depStats = wlStats?.deployments ?? { total: 0, degraded: 0 }
  const stsStats = wlStats?.statefulsets ?? { total: 0, degraded: 0 }
  const dsStats = wlStats?.daemonsets ?? { total: 0, degraded: 0 }
  const wlTotal = wlStats?.total ?? (depStats.total + stsStats.total + dsStats.total)
  const podPct = cluster.pod_capacity ? Math.round((cluster.pod_usage / cluster.pod_capacity) * 100) : 0

  // 组件状态文案（external / not_found 见下方「Pod / 实例」列的处理）。
  const COMPONENT_STATUS: Record<string, { color: 'success' | 'error' | 'processing' | 'default'; text: string; tip?: string }> = {
    healthy: { color: 'success', text: '正常' },
    unhealthy: { color: 'error', text: '异常' },
    external: { color: 'processing', text: '控制面外置', tip: '该组件未以 kube-system Pod 形态运行（托管控制面 / 内嵌 etcd / 静态 Pod），属正常现象' },
    not_found: { color: 'default', text: '未发现', tip: '集群内未发现该组件，请确认拓扑是否预期' },
  }

  const componentColumns: ColumnsType<K8sComponentHealth> = [
    { title: '组件', dataIndex: 'component', width: 160 },
    {
      title: 'Pod / 实例', dataIndex: 'name', ellipsis: true,
      render: (v: string, r) => {
        // 占位行（external/not_found）没有真实 Pod，渲染为「—」，
        // 否则会把 "apiserver" 这种目标名当成 Pod 名展示。
        if (r.status === 'external' || r.status === 'not_found') return <Text type="secondary">—</Text>
        // 异常组件一键定位：跳到 Pods Tab 并高亮该 Pod（都在 kube-system）。
        if (r.status === 'unhealthy' && v) {
          return (
            <Tooltip title="点击定位到该 Pod（Pods Tab）">
              <a onClick={() => onGoTab('pods', {
                ns: 'kube-system', q: v, focus: focusValue('Pod', 'kube-system', v),
              })}>
                <Text code style={{ fontSize: 12 }}>{v}</Text>
              </a>
            </Tooltip>
          )
        }
        return <Text code style={{ fontSize: 12 }}>{v}</Text>
      },
    },
    {
      title: '状态', dataIndex: 'status', width: 130,
      render: (v: string) => {
        const s = COMPONENT_STATUS[v] ?? { color: 'default' as const, text: v }
        return (
          <Tooltip title={s.tip}>
            <Badge status={s.color} text={s.text} />
          </Tooltip>
        )
      },
    },
    { title: 'Phase', dataIndex: 'phase', width: 100 },
  ]

  /** KubeSphere 风格四象限：卡片即入口，点击跳对应 Tab。 */
  const quadrant: Array<{ key: string; icon: ReactNode; title: string; main: string; sub: string; subTip?: string; danger: boolean }> = [
    {
      key: 'nodes', icon: <DesktopOutlined />, title: '节点',
      main: `${nodeStats?.ready ?? 0}/${nodeStats?.total ?? 0}`,
      sub: `就绪 · ${nodeStats?.unschedulable ?? 0} 台不可调度`,
      danger: (nodeStats?.not_ready ?? 0) > 0,
    },
    {
      key: 'workloads', icon: <CloudServerOutlined />, title: '工作负载',
      main: String(wlTotal),
      sub: `未就绪 ${wlStats?.degraded ?? 0} · D ${depStats.total}/${depStats.degraded} · S ${stsStats.total}/${stsStats.degraded} · DS ${dsStats.total}/${dsStats.degraded}`,
      subTip: 'D=Deployment，S=StatefulSet，DS=DaemonSet；分数为 未就绪数/总数',
      danger: (wlStats?.degraded ?? 0) > 0,
    },
    {
      key: 'pods', icon: <AlertOutlined />, title: 'Pods',
      main: String((podStats?.running ?? 0) + (podStats?.abnormal ?? 0) + (podStats?.pending ?? 0)),
      sub: `异常 ${podStats?.abnormal ?? 0} · CrashLoop ${podStats?.crash_loop ?? 0} · Pending ${podStats?.pending ?? 0}`,
      danger: (podStats?.abnormal ?? 0) > 0 || (podStats?.crash_loop ?? 0) > 0,
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={[16, 16]}>
        {quadrant.map((q) => (
          <Col xs={24} sm={8} key={q.key}>
            <Card
              size="small" hoverable
              onClick={() => onGoTab(q.key)}
              styles={{ body: { padding: '12px 16px' } }}
            >
              <Flex justify="space-between" align="center">
                <Space size={10}>
                  {q.icon}
                  <Space direction="vertical" size={0}>
                    <Text type="secondary" style={{ fontSize: 12 }}>{q.title}</Text>
                    <Text strong style={{ fontSize: 22, lineHeight: 1.2, color: q.danger ? '#fa8c16' : undefined }}>
                      {q.main}
                    </Text>
                  </Space>
                </Space>
                <Text type="secondary" style={{ fontSize: 11, textAlign: 'right' }}>
                  {q.subTip ? <Tooltip title={q.subTip}>{q.sub}</Tooltip> : q.sub}
                </Text>
              </Flex>
            </Card>
          </Col>
        ))}
      </Row>

      <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
        <Row gutter={16}>
          <Col xs={24} sm={8}>
            <Flex justify="space-between"><Text type="secondary">Pod 水位</Text><Text>{cluster.pod_usage}/{cluster.pod_capacity}</Text></Flex>
            <Progress percent={podPct} size="small" status={podPct > 80 ? 'exception' : podPct > 60 ? 'active' : 'normal'} />
          </Col>
          <Col xs={24} sm={8}>
            <Flex justify="space-between"><Text type="secondary">CPU 容量</Text><Text>{cluster.cpu_cores} 核</Text></Flex>
            <Progress percent={100} size="small" showInfo={false} strokeColor="#f0f0f0" />
          </Col>
          <Col xs={24} sm={8}>
            <Flex justify="space-between"><Text type="secondary">内存容量</Text><Text>{cluster.mem_gb} Gi</Text></Flex>
            <Progress percent={100} size="small" showInfo={false} strokeColor="#f0f0f0" />
          </Col>
        </Row>
      </Card>

      <Card
        size="small"
        title="集群健康"
        extra={
          <Tooltip title="重新探测集群，更新版本/节点数/水位等快照信息">
            <Button
              size="small" icon={<SyncOutlined />} loading={refreshMutation.isPending}
              disabled={!canManage} onClick={() => refreshMutation.mutate()}
            >
              刷新同步
            </Button>
          </Tooltip>
        }
        loading={loadingHealth && !health}
      >
        {health?.api_reachable === false ? (
          <Alert type="error" showIcon message={`API 不可达：${health.api_error || '未知错误'}`} />
        ) : (
          health?.summary && (
            <Text type={health.top_issues?.length ? 'warning' : 'secondary'}>
              <CheckCircleOutlined style={{ color: health.top_issues?.length ? '#fa8c16' : '#52c41a', marginRight: 6 }} />
              {health.summary}
            </Text>
          )
        )}
      </Card>

      {!!health?.top_issues?.length && (
        <Card title={<Space><WarningOutlined style={{ color: '#fa8c16' }} />Top 诊断问题</Space>} size="small">
          {health.top_issues.map((issue: K8sHealthIssue, idx: number) => {
            const locate = issueLocate(issue)
            const title = locate ? `点击定位到「${issueTab(issue) === 'pods' ? 'Pods' : issueTab(issue) === 'nodes' ? '节点' : issueTab(issue) === 'workloads' ? '工作负载' : '资源浏览'}」Tab 并高亮该资源` : undefined
            return (
              <Tooltip key={idx} title={title}>
                <Flex
                  gap={8} align="flex-start"
                  style={{
                    padding: '4px 0',
                    borderBottom: idx < health.top_issues.length - 1 ? '1px solid #f5f5f5' : undefined,
                    cursor: locate ? 'pointer' : undefined,
                  }}
                  onClick={() => { if (locate) onGoTab(issueTab(issue), locate) }}
                >
                  <Tag color={issue.level === 'critical' ? 'red' : 'orange'} style={{ flexShrink: 0 }}>{issue.level}</Tag>
                  <Space direction="vertical" size={0} style={{ flex: 1, minWidth: 0 }}>
                    <Text strong style={{ fontSize: 12 }}>
                      {issue.kind ? `${issue.kind} ` : ''}{issue.namespace ? `${issue.namespace}/` : ''}{issue.name || issue.type}
                      {issue.count ? ` ×${issue.count}` : ''}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>{issue.message || issue.detail}</Text>
                    {issue.suggestion && <Text type="secondary" style={{ fontSize: 12 }}>建议：{issue.suggestion}</Text>}
                  </Space>
                  {locate && <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>定位 →</Text>}
                </Flex>
              </Tooltip>
            )
          })}
        </Card>
      )}

      <Card title="核心组件健康（kube-system）" size="small">
        <Table<K8sComponentHealth>
          rowKey="name"
          size="small"
          columns={componentColumns}
          dataSource={components}
          loading={loadingComponents}
          pagination={false}
          locale={{ emptyText: <Empty description="暂无组件数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        />
      </Card>
    </Space>
  )
}
