import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Space, Typography, Tag, Flex, Input, Select, Drawer, Empty } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { clusterOpsApi } from '@/api'

const { Text } = Typography

/** 后端 search 实际支持的 Kind（见 app/api/clusters/resources.py 的 _search_* 分支）。 */
const SEARCHABLE_KINDS = [
  'Pod', 'Deployment', 'StatefulSet', 'DaemonSet', 'Service', 'ConfigMap', 'Secret', 'Ingress',
]

// ───────────────────────── 跨资源全局搜索 ─────────────────────────

export default function GlobalSearchBox({ clusterId, onNavigate }: { clusterId: string; onNavigate: (ns: string, kind: string, name: string) => void }) {
  const [keyword, setKeyword] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [kinds, setKinds] = useState<string[]>(SEARCHABLE_KINDS)
  const kindsKey = kinds.slice().sort().join(',')

  const { data: results = [], isFetching } = useQuery({
    queryKey: ['cluster-global-search', clusterId, submitted, kindsKey],
    queryFn: () => clusterOpsApi.search(clusterId, submitted, '_all', kinds),
    enabled: !!submitted,
    staleTime: 30 * 1000,
  })

  return (
    <Space>
      <Select
        size="small"
        mode="multiple"
        style={{ minWidth: 150, maxWidth: 240 }}
        placeholder="资源类型"
        value={kinds}
        onChange={setKinds}
        options={SEARCHABLE_KINDS.map((k) => ({ value: k, label: k }))}
        maxTagCount="responsive"
      />
      <Input.Search
        size="small"
        placeholder="搜索集群资源（Pod/负载/Service…）"
        style={{ width: 220 }}
        allowClear
        value={keyword}
        onChange={(e) => setKeyword(e.target.value)}
        onSearch={(v) => setSubmitted(v.trim())}
        loading={isFetching}
        prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
      />
      {submitted && (
        <Drawer
          title={`搜索结果 - "${submitted}"（${results.length} 条）`}
          open
          onClose={() => { setSubmitted(''); setKeyword('') }}
          width={640}
        >
          {results.length === 0 ? (
            <Empty description="没有匹配的资源" style={{ padding: '40px 0' }} />
          ) : (
            results.map((item, idx) => (
              <Flex
                key={idx}
                justify="space-between" align="center"
                style={{ padding: '6px 0', borderBottom: '1px solid #f5f5f5', cursor: 'pointer' }}
                onClick={() => { onNavigate(item.namespace || '', item.kind || '', item.name || ''); setSubmitted('') }}
              >
                <Space>
                  <Tag>{item.kind}</Tag>
                  <Text strong>{item.namespace ? `${item.namespace}/` : ''}{item.name}</Text>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>{item.status || item.detail || ''}</Text>
              </Flex>
            ))
          )}
        </Drawer>
      )}
    </Space>
  )
}
