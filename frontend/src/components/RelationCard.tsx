import { useQuery } from '@tanstack/react-query'
import { Card, Tag, Space, Spin, Typography, Button } from 'antd'
import { LinkOutlined, UserOutlined, PayCircleOutlined, WarningOutlined, AppstoreOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import client from '@/api/client'

const { Text } = Typography

interface RelatedApp {
  id: string
  label: string
  environment?: string
  owner?: string
  status?: string
}

interface RelatedData {
  apps: RelatedApp[]
  owners: string[]
  node?: { label?: string; type?: string }
  impact_summary?: { app_count: number; owner_count: number; environments: string[] }
}

interface CostData {
  month?: string
  pretax_amount?: number
  total?: number
  [key: string]: unknown
}

interface Props {
  /** host / vm / domain / certificate / application */
  type: string
  /** 资源业务 ID（主机 instance_id、虚机 vm_id 等） */
  id: string
  /** 传入后，费用接口用它（云实例 ID 可能与业务 ID 不同） */
  costInstanceId?: string
  title?: string
}

/** 详情页「关联卡片」：应用 / 负责人 / 费用 / 告警，点应用可跳转。 */
export default function RelationCard({ type, id, costInstanceId, title = '关联信息' }: Props) {
  const navigate = useNavigate()

  const { data: related, isLoading } = useQuery({
    queryKey: ['cmdb-related', type, id],
    queryFn: () =>
      client.get<RelatedData>('/cmdb/impact', { params: { type, id } }).then((r) => r.data),
  })

  const { data: cost } = useQuery({
    queryKey: ['instance-cost', costInstanceId || id],
    queryFn: () =>
      client
        .get<CostData>(`/finance/instance/${encodeURIComponent(costInstanceId || id)}/cost`)
        .then((r) => r.data),
    enabled: !!(costInstanceId || id),
    retry: false,
  })

  const { data: activeAlerts } = useQuery({
    queryKey: ['related-alerts', id],
    queryFn: () =>
      client
        .get<{ alerts?: Array<{ fingerprint: string; name?: string; severity?: string; instance?: string }> }>(
          '/monitoring/alerts/active',
        )
        .then((r) => {
          const list = r.data?.alerts ?? []
          return list.filter(
            (a) =>
              a.instance === id ||
              a.instance?.includes(id) ||
              (costInstanceId && a.instance === costInstanceId),
          )
        }),
    retry: false,
  })

  const amount =
    (typeof cost?.pretax_amount === 'number' && cost.pretax_amount) ||
    (typeof cost?.total === 'number' && cost.total) ||
    null

  return (
    <Card size="small" title={title} extra={<Tag>{type}</Tag>}>
      {isLoading ? (
        <Spin size="small" />
      ) : (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space align="center">
            <AppstoreOutlined />
            <Text strong>关联应用</Text>
            <Text type="secondary">{related?.impact_summary?.app_count ?? related?.apps?.length ?? 0}</Text>
          </Space>
          {!related?.apps?.length ? (
            <Text type="secondary">未绑定到应用</Text>
          ) : (
            <Space wrap>
              {related.apps.map((a) => (
                <Tag
                  key={a.id}
                  color="blue"
                  style={{ cursor: 'pointer' }}
                  onClick={() => navigate(`/apps/${a.id}`)}
                >
                  {a.label}
                  {a.environment ? ` · ${a.environment}` : ''}
                </Tag>
              ))}
            </Space>
          )}

          <Space align="center">
            <UserOutlined />
            <Text strong>责任人</Text>
          </Space>
          {!related?.owners?.length ? (
            <Text type="secondary">未指定</Text>
          ) : (
            <Space wrap>
              {related.owners.map((o) => (
                <Tag key={o} color="geekblue">{o}</Tag>
              ))}
            </Space>
          )}

          <Space align="center">
            <PayCircleOutlined />
            <Text strong>本月费用</Text>
          </Space>
          {amount === null ? (
            <Text type="secondary">无账单数据</Text>
          ) : (
            <Text>¥ {amount.toFixed(2)}</Text>
          )}

          <Space align="center">
            <WarningOutlined />
            <Text strong>活动告警</Text>
            <Text type="secondary">{activeAlerts?.length ?? 0}</Text>
          </Space>
          {!!activeAlerts?.length && (
            <Space wrap>
              {activeAlerts.slice(0, 5).map((a) => (
                <Tag
                  key={a.fingerprint}
                  color={a.severity === 'critical' ? 'red' : 'orange'}
                  style={{ cursor: 'pointer' }}
                  onClick={() => navigate('/monitoring/alerts')}
                >
                  {a.name || a.fingerprint.slice(0, 8)}
                </Tag>
              ))}
            </Space>
          )}

          <Button
            size="small"
            type="link"
            icon={<LinkOutlined />}
            onClick={() => navigate('/assets/cmdb')}
          >
            打开关联图谱
          </Button>
        </Space>
      )}
    </Card>
  )
}
