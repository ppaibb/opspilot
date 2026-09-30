/**
 * 集群详情页（/assets/clusters/:clusterId）。
 *
 * v2 —— 对照 Rancher / KubeSphere 的 UX 重构：
 * 1. Tab 同步 URL（?tab=nodes），刷新/分享不丢位置；
 * 2. 概览统计卡可点击下钻到对应 Tab，附 Pod 水位容量进度条；
 * 3. 节点列表内嵌实时用量进度条（/nodes/metrics，metrics-server 缺失时优雅降级），
 *    节点详情抽屉；
 * 4. 工作负载：namespace 动态筛选 + YAML 查看 + 扩缩容步进器；
 * 5. Pods：状态筛选（Running/Pending/Failed）+ 实时日志查看器（PodLogViewer，WS 流）；
 * 6. 资源浏览：namespace 动态下拉。
 *
 * 保留约定：全部实时拉取，破坏性操作走 useRiskConfirm。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Tabs, Button, Space, Tag, Typography, Flex, Spin, Empty, Tooltip,
} from 'antd'
import {
  ArrowLeftOutlined, CodeOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { clustersApi, clusterDetailApi } from '@/api'
import { usePermission } from '@/hooks/usePermission'
import ClusterYamlWorkbench from '@/components/ClusterYamlWorkbench'
import ClusterLiveFeed from '@/components/ClusterLiveFeed'
import { statusTag } from './detail/shared'
import { TAB_KEYS } from './detail/types'
import OverviewTab from './detail/OverviewTab'
import NodesTab from './detail/NodesTab'
import WorkloadsTab from './detail/WorkloadsTab'
import PodsTab from './detail/PodsTab'
import ResourcesTab from './detail/ResourcesTab'
import NamespacesTab from './detail/NamespacesTab'
import AuditTab from './detail/AuditTab'
import GlobalSearchBox from './detail/GlobalSearchBox'

const { Text } = Typography

// ───────────────────────── 页面主体 ─────────────────────────

export default function ClusterDetailPage() {
  const { clusterId } = useParams<{ clusterId: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') || 'overview'
  // 非法 tab 参数回落概览。
  const activeTab = (TAB_KEYS as readonly string[]).includes(tabParam) ? tabParam : 'overview'

  const { data: cluster, isLoading } = useQuery({
    queryKey: ['cluster-detail', clusterId],
    queryFn: async () => {
      // 后端无单集群 GET，从列表中取（列表本身有缓存）。
      const list = await clustersApi.list()
      return list.find((c) => c.cluster_id === clusterId) || null
    },
    enabled: !!clusterId,
  })

  // 全页共享的 namespace 清单（工作负载/Pod/资源浏览三个 Tab 复用）。
  const { data: namespaces = [] } = useQuery({
    queryKey: ['cluster-namespaces', clusterId],
    queryFn: () => clusterDetailApi.namespaces(clusterId!),
    enabled: !!clusterId,
    staleTime: 5 * 60 * 1000,
  })
  const namespaceNames = namespaces
    .map((n) => n.name || n)
    .filter((v): v is string => typeof v === 'string')

  const changeTab = (tab: string) => {
    setSearchParams(tab === 'overview' ? {} : { tab }, { replace: false })
  }

  const [yamlOpen, setYamlOpen] = useState(false)
  const [liveOpen, setLiveOpen] = useState(false)
  const { has } = usePermission()
  const canManage = has('action.assets.clusters.manage')

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: '120px 0' }}><Spin size="large" tip="加载集群..." /></div>
  }

  if (!cluster) {
    return (
      <Card>
        <Empty description="集群不存在或已被解除纳管">
          <Button type="primary" onClick={() => navigate('/assets/clusters')}>返回集群列表</Button>
        </Empty>
      </Card>
    )
  }

  return (
    <Flex vertical gap={16}>
      <Flex justify="space-between" align="center" wrap gap="small">
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/assets/clusters')} />
          <Space direction="vertical" size={0}>
            <Space size={8}>
              <Typography.Title level={4} style={{ margin: 0 }}>{cluster.name}</Typography.Title>
              <Tag color="blue">{cluster.provider}</Tag>
              {statusTag(cluster.status)}
            </Space>
            <Text type="secondary">
              v{cluster.version} · {cluster.node_count} 节点 · Pod {cluster.pod_usage}/{cluster.pod_capacity} · {cluster.cpu_cores}C / {cluster.mem_gb}G
            </Text>
          </Space>
        </Space>
        <Space>
          <GlobalSearchBox clusterId={cluster.cluster_id} onNavigate={(_ns, kind) => {
            // 搜索结果跳转：Pod → Pods Tab，其余 → 资源浏览 Tab。
            if (kind === 'Pod') changeTab('pods')
            else changeTab('resources')
          }} />
          <Tooltip title="粘贴 YAML 部署任意资源（先试运行后应用）">
            <Button icon={<CodeOutlined />} disabled={!canManage} onClick={() => setYamlOpen(true)}>
              YAML 部署
            </Button>
          </Tooltip>
          <Tooltip title="Informer 实时推送的资源变更流（对标 kubectl get -w）">
            <Button icon={<ThunderboltOutlined />} onClick={() => setLiveOpen(true)}>
              实时动态
            </Button>
          </Tooltip>
        </Space>
      </Flex>

      <Tabs
        activeKey={activeTab}
        onChange={changeTab}
        items={[
          { key: 'overview', label: '概览', children: <OverviewTab cluster={cluster} onGoTab={changeTab} /> },
          { key: 'nodes', label: '节点', children: <NodesTab clusterId={cluster.cluster_id} /> },
          { key: 'workloads', label: '工作负载', children: <WorkloadsTab clusterId={cluster.cluster_id} namespaces={namespaceNames} /> },
          { key: 'pods', label: 'Pods', children: <PodsTab clusterId={cluster.cluster_id} namespaces={namespaceNames} /> },
          { key: 'resources', label: '资源浏览', children: <ResourcesTab clusterId={cluster.cluster_id} namespaces={namespaceNames} /> },
          { key: 'namespaces', label: '命名空间', children: <NamespacesTab clusterId={cluster.cluster_id} /> },
          { key: 'audit', label: '操作审计', children: <AuditTab clusterId={cluster.cluster_id} /> },
        ]}
      />

      <ClusterYamlWorkbench
        open={yamlOpen}
        clusterId={cluster.cluster_id}
        clusterName={cluster.name}
        onClose={() => setYamlOpen(false)}
      />

      <ClusterLiveFeed
        open={liveOpen}
        clusterId={cluster.cluster_id}
        namespaces={namespaceNames}
        onClose={() => setLiveOpen(false)}
      />
    </Flex>
  )
}
