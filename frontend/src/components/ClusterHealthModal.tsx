/**
 * 集群健康摘要弹窗
 * 展示 /api/clusters/{id}/health-summary 返回的核心运维信号。
 * 响应结构：node_stats / workload_stats / pod_stats 均为嵌套统计对象，
 * workload_stats 下每个 Kind 又是 {total, degraded}，读取时必须逐层取字段，
 * 不能把对象直接交给 React 渲染（否则界面出现 [object Object]）。
 */
import { useQuery } from '@tanstack/react-query'
import { Modal, Spin, Alert, Descriptions, Badge, Tag, Space, Typography } from 'antd'
import { clusterHealthApi } from '@/api'
import type { K8sHealthSummary } from '@/types/api'

const { Text } = Typography

interface Props {
  open: boolean
  clusterId: string
  clusterName?: string
  onClose: () => void
}

export default function ClusterHealthModal({ open, clusterId, clusterName, onClose }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['cluster-health', clusterId],
    queryFn: () => clusterHealthApi.summary(clusterId) as Promise<K8sHealthSummary>,
    enabled: open && !!clusterId,
    staleTime: 30_000,
  })

  const nodeStats = data?.node_stats
  const podStats = data?.pod_stats
  const wlStats = data?.workload_stats

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      title={`集群健康 · ${clusterName ?? clusterId}`}
      width={640}
      centered
    >
      {isLoading && <div style={{ textAlign: 'center', padding: 48 }}><Spin /></div>}
      {error && <Alert type="error" message="获取健康信息失败" showIcon />}
      {data && (
        <Descriptions bordered column={2} size="small">
          <Descriptions.Item label="API 连通性" span={2}>
            <Badge
              status={data.api_reachable ? 'success' : 'error'}
              text={data.api_reachable ? '正常' : '不可达'}
            />
            {data.api_latency_ms ? <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>{data.api_latency_ms}ms</Text> : null}
          </Descriptions.Item>
          {!data.api_reachable && !!data.api_error && (
            <Descriptions.Item label="错误信息" span={2}>
              <Text type="danger" style={{ fontSize: 12 }}>{data.api_error}</Text>
            </Descriptions.Item>
          )}
          <Descriptions.Item label="节点就绪">
            <Text type={nodeStats && nodeStats.not_ready > 0 ? 'danger' : 'success'}>
              {nodeStats ? `${nodeStats.ready} / ${nodeStats.total}` : '-'}
            </Text>
            {nodeStats && nodeStats.unschedulable > 0 && (
              <Text type="warning" style={{ marginLeft: 8, fontSize: 12 }}>{nodeStats.unschedulable} 台不可调度</Text>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="未就绪工作负载">
            <Text type={wlStats && wlStats.degraded > 0 ? 'danger' : 'success'}>
              {wlStats ? `${wlStats.total - wlStats.degraded} / ${wlStats.total}` : '-'}
            </Text>
          </Descriptions.Item>
          <Descriptions.Item label="Pod 异常">
            <Text type={podStats && (podStats.abnormal > 0 || podStats.crash_loop > 0) ? 'danger' : 'success'}>
              {podStats ? podStats.abnormal + podStats.crash_loop : '-'}
            </Text>
            {podStats && podStats.pending > 0 && (
              <Text type="warning" style={{ marginLeft: 8, fontSize: 12 }}>Pending {podStats.pending}</Text>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="近期 Warning Events">
            <Text type={data.recent_warning_events?.length ? 'warning' : undefined}>
              {data.recent_warning_events?.length ?? 0}
            </Text>
          </Descriptions.Item>
          {!!data.top_issues?.length && (
            <Descriptions.Item label="Top 诊断问题" span={2}>
              <Space wrap>
                {data.top_issues.map((issue, idx) => (
                  <Tag key={idx} color="orange">{String(issue.message ?? '')}</Tag>
                ))}
              </Space>
            </Descriptions.Item>
          )}
          {!!data.summary && (
            <Descriptions.Item label="摘要" span={2}>
              <Text type="secondary" style={{ fontSize: 13 }}>{data.summary}</Text>
            </Descriptions.Item>
          )}
        </Descriptions>
      )}
      </Modal>
  )
}
