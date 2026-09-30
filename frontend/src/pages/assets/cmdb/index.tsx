import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Flex, Select, Input, Descriptions, Tag, Empty, Spin, Button, Space } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import client from '@/api/client'

const TYPE_LABEL: Record<string, string> = {
  application: '应用',
  host: '主机',
  vm: '虚拟机',
  domain: '域名',
  certificate: '证书',
}

const TYPE_COLOR: Record<string, string> = {
  application: '#1677ff',
  host: '#52c41a',
  vm: '#722ed1',
  domain: '#fa8c16',
  certificate: '#eb2f96',
}

interface CmdbNode {
  type: string
  id: string
  label: string
  status?: string
  owner?: string
  environment?: string
  category?: string
  ip?: string
  tags?: string[]
}

interface CmdbEdge {
  source: string
  target: string
  kind?: string
}

interface CmdbGraph {
  nodes: CmdbNode[]
  edges: CmdbEdge[]
  total_nodes: number
  total_edges: number
}

interface CmdbImpact {
  node: CmdbNode
  apps: CmdbNode[]
  resources: CmdbNode[]
  owners: string[]
  impact_summary?: {
    app_count: number
    owner_count: number
    environments: string[]
  }
}

const ENV_OPTIONS = [
  { value: '', label: '全部环境' },
  { value: 'prod', label: '生产' },
  { value: 'test', label: '测试' },
  { value: 'dev', label: '开发' },
  { value: 'mixed', label: '混合' },
]

export default function CmdbPage() {
  const [environment, setEnvironment] = useState('')
  const [owner, setOwner] = useState('')
  const [selected, setSelected] = useState<{ type: string; id: string } | null>(null)

  const { data: graph, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['cmdb-graph', environment, owner],
    queryFn: () =>
      client
        .get<CmdbGraph>('/cmdb/graph', { params: { environment, owner } })
        .then((r) => r.data),
  })

  const { data: impact, isLoading: impactLoading } = useQuery({
    queryKey: ['cmdb-impact', selected?.type, selected?.id],
    queryFn: () =>
      client
        .get<CmdbImpact>('/cmdb/impact', { params: { type: selected!.type, id: selected!.id } })
        .then((r) => r.data),
    enabled: !!selected,
  })

  const option = useMemo(() => {
    if (!graph) return {}
    const categories = Array.from(new Set(graph.nodes.map((n) => n.type)))
    return {
      tooltip: {
        // 节点业务字段在 data.raw（CmdbNode），data 本体只有 id/name/category 等图形字段
        formatter: (p: { dataType?: string; data?: { raw?: CmdbNode; name?: string } }) => {
          if (p.dataType === 'edge') return '绑定关系'
          const d = p.data?.raw
          if (!d) return ''
          const lines = [`${TYPE_LABEL[d.type] ?? d.type}：${d.label}`]
          if (d.owner) lines.push(`负责人：${d.owner}`)
          if (d.environment) lines.push(`环境：${d.environment}`)
          if (d.ip) lines.push(`地址：${d.ip}`)
          return lines.join('<br/>')
        },
      },
      legend: [{ data: categories.map((c) => TYPE_LABEL[c] ?? c) }],
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          force: { repulsion: 320, edgeLength: 120, gravity: 0.08 },
          label: { show: true, position: 'right', fontSize: 11 },
          edgeSymbol: ['none', 'arrow'],
          edgeSymbolSize: 8,
          lineStyle: { color: '#b0b8c4', curveness: 0.12, width: 1.2 },
          emphasis: { focus: 'adjacency', lineStyle: { width: 2 } },
          categories: categories.map((c) => ({ name: TYPE_LABEL[c] ?? c })),
          data: graph.nodes.map((n) => ({
            id: n.id,
            name: n.label,
            value: n.label,
            category: categories.indexOf(n.type),
            symbolSize: n.type === 'application' ? 42 : 30,
            itemStyle: { color: TYPE_COLOR[n.type] ?? '#888' },
            raw: n,
          })),
          links: graph.edges.map((e) => ({ source: e.source, target: e.target })),
        },
      ],
    }
  }, [graph])

  const onEvents = useMemo(
    () => ({
      click: (params: { data?: { raw?: CmdbNode } }) => {
        const raw = params.data?.raw
        if (!raw) return
        setSelected({ type: raw.type, id: raw.id.includes(':') ? raw.id.split(':').slice(1).join(':') : raw.id })
      },
    }),
    [],
  )

  return (
    <Flex vertical gap={12}>
      <Card size="small" title="CMDB 关联图谱">
        <Flex gap={12} wrap align="center" style={{ marginBottom: 12 }}>
          <Select
            style={{ width: 140 }}
            value={environment}
            options={ENV_OPTIONS}
            onChange={(v) => setEnvironment(v)}
          />
          <Input.Search
            allowClear
            placeholder="按负责人筛选"
            style={{ width: 200 }}
            onSearch={(v) => setOwner(v.trim())}
          />
          <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()}>刷新</Button>
          <span style={{ color: '#888', fontSize: 12 }}>
            节点 {graph?.total_nodes ?? 0} · 关系 {graph?.total_edges ?? 0}（点击节点看影响面）
          </span>
        </Flex>
        {isLoading ? (
          <Flex justify="center" align="center" style={{ height: 520 }}><Spin /></Flex>
        ) : !graph || graph.nodes.length === 0 ? (
          <Empty description="暂无关联数据（先在应用详情里绑定主机/域名/证书）" style={{ padding: 80 }} />
        ) : (
          <ReactECharts
            option={option}
            style={{ height: 560 }}
            onEvents={onEvents}
            notMerge
          />
        )}
      </Card>

      <Card
        size="small"
        title="影响面"
        extra={
          selected ? (
            <Space>
              <Tag color="blue">{TYPE_LABEL[selected.type] ?? selected.type}</Tag>
              <Button size="small" onClick={() => setSelected(null)}>清空选择</Button>
            </Space>
          ) : null
        }
      >
        {!selected ? (
          <Empty description="点击图中任意节点查看关联应用与责任人" />
        ) : impactLoading ? (
          <Spin />
        ) : impact ? (
          <Flex gap={24} wrap>
            <div style={{ minWidth: 280, flex: 1 }}>
              <Descriptions title="节点" size="small" column={1} bordered>
                <Descriptions.Item label="名称">{impact.node.label}</Descriptions.Item>
                <Descriptions.Item label="类型">{TYPE_LABEL[impact.node.type] ?? impact.node.type}</Descriptions.Item>
                <Descriptions.Item label="状态">{impact.node.status || '-'}</Descriptions.Item>
                {impact.node.ip ? <Descriptions.Item label="地址">{impact.node.ip}</Descriptions.Item> : null}
                {impact.node.owner ? <Descriptions.Item label="负责人">{impact.node.owner}</Descriptions.Item> : null}
                {impact.node.environment ? (
                  <Descriptions.Item label="环境">{impact.node.environment}</Descriptions.Item>
                ) : null}
              </Descriptions>
              {impact.impact_summary ? (
                <Descriptions size="small" column={1} style={{ marginTop: 12 }} bordered>
                  <Descriptions.Item label="关联应用数">{impact.impact_summary.app_count}</Descriptions.Item>
                  <Descriptions.Item label="责任人数">{impact.impact_summary.owner_count}</Descriptions.Item>
                  <Descriptions.Item label="涉及环境">
                    {impact.impact_summary.environments.join('、') || '-'}
                  </Descriptions.Item>
                </Descriptions>
              ) : null}
            </div>
            <div style={{ minWidth: 280, flex: 1 }}>
              <div style={{ marginBottom: 8, fontWeight: 500 }}>关联应用</div>
              {impact.apps.length === 0 ? (
                <Empty description="未绑定到任何应用" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                <Flex vertical gap={8}>
                  {impact.apps.map((a) => (
                    <Card key={a.id} size="small">
                      <Flex justify="space-between" align="center">
                        <span>{a.label}</span>
                        <Space>
                          {a.environment ? <Tag>{a.environment}</Tag> : null}
                          {a.owner ? <Tag color="geekblue">{a.owner}</Tag> : null}
                        </Space>
                      </Flex>
                    </Card>
                  ))}
                </Flex>
              )}
              <div style={{ margin: '12px 0 8px', fontWeight: 500 }}>责任人</div>
              <Space wrap>
                {impact.owners.length === 0 ? (
                  <span style={{ color: '#999' }}>未指定</span>
                ) : (
                  impact.owners.map((o) => <Tag key={o} color="gold">{o}</Tag>)
                )}
              </Space>
            </div>
          </Flex>
        ) : (
          <Empty description="未找到节点" />
        )}
      </Card>
    </Flex>
  )
}
