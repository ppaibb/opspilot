import { useState } from 'react'
import {
  Card, Statistic, Row, Col, Table, Button, Tag, Space, Skeleton, Alert, Descriptions, Badge, Flex,
} from 'antd'
import {
  DesktopOutlined, HddOutlined, AppstoreOutlined, ExclamationCircleOutlined, LinkOutlined,
} from '@ant-design/icons'
import RelationCard from '@/components/RelationCard'
import type { ApplicationOverviewResponse, ApplicationResponse } from '@/types/api'
import { ENV_COLOR, ENV_LABEL, STATUS_COLOR, STATUS_LABEL, HEALTH_COLOR } from './constants'

export default function OverviewPanel({
  app,
  overview,
  overviewLoading,
  componentCount,
  setActiveTab,
}: {
  app: ApplicationResponse
  overview: ApplicationOverviewResponse | undefined
  overviewLoading: boolean
  componentCount: number
  setActiveTab: (tab: string) => void
}) {
  const [releaseStatusFilter, setReleaseStatusFilter] = useState<'all' | 'success' | 'failed'>('all')

  return (
    <Flex vertical gap={16}>
      <RelationCard type="application" id={app.app_id} title="关联资产 / 费用 / 告警" />
      <Card size="small" title="基本信息">
        <Descriptions size="small" column={3} labelStyle={{ color: '#8c8c8c' }}>
          <Descriptions.Item label="应用 ID"><code style={{ fontSize: 12 }}>{app.app_id}</code></Descriptions.Item>
          <Descriptions.Item label="环境"><Tag color={ENV_COLOR[app.environment] || 'default'}>{ENV_LABEL[app.environment] || app.environment}</Tag></Descriptions.Item>
          <Descriptions.Item label="状态"><Badge status={STATUS_COLOR[app.status]} text={STATUS_LABEL[app.status] || app.status} /></Descriptions.Item>
          <Descriptions.Item label="分类">{app.category || '-'}</Descriptions.Item>
          <Descriptions.Item label="负责人">{app.owner || '-'}</Descriptions.Item>
          <Descriptions.Item label="健康状态"><Badge status={HEALTH_COLOR[app.health_status]} text={app.health_label} /></Descriptions.Item>
          {app.entry_url && <Descriptions.Item label="访问地址" span={3}><a href={app.entry_url} target="_blank" rel="noreferrer">{app.entry_url} <LinkOutlined /></a></Descriptions.Item>}
          {app.description && <Descriptions.Item label="描述" span={3}>{app.description}</Descriptions.Item>}
          <Descriptions.Item label="创建时间">{app.created_at?.slice(0, 16) || '-'}</Descriptions.Item>
          <Descriptions.Item label="更新时间">{app.updated_at?.slice(0, 16) || '-'}</Descriptions.Item>
        </Descriptions>
      </Card>

      {(app.health_reasons?.length ?? 0) > 0 && (
        <Alert
          type={HEALTH_COLOR[app.health_status] === 'error' ? 'error' : HEALTH_COLOR[app.health_status] === 'warning' ? 'warning' : 'success'}
          showIcon message="健康状态详情"
          description={<ul style={{ margin: 0, paddingLeft: 20 }}>{app.health_reasons!.map((r, i) => <li key={i}>{r}</li>)}</ul>}
        />
      )}

      {overviewLoading ? <Skeleton.Input active block /> : overview && (
        <Row gutter={[16, 16]}>
          {[
            { label: '绑定主机', value: overview.host_stats.total, sub: `${overview.host_stats.online} 在线`, icon: <DesktopOutlined style={{ color: '#1677ff', marginRight: 6 }} />, tab: 'resources' },
            { label: '绑定虚机', value: overview.vm_stats.total, sub: `${overview.vm_stats.running} 运行中`, icon: <HddOutlined style={{ color: '#722ed1', marginRight: 6 }} />, tab: 'resources' },
            { label: '部署组件', value: componentCount, sub: '', icon: <AppstoreOutlined style={{ color: '#13c2c2', marginRight: 6 }} />, tab: 'components' },
            { label: '证书/域名到期', value: overview.expiry_stats.certificate_expiring + overview.expiry_stats.domain_expiring, sub: '', icon: <ExclamationCircleOutlined style={{ color: '#faad14', marginRight: 6 }} />, tab: 'resources' },
          ].map((item) => (
            <Col xs={12} sm={6} key={item.label}>
              <Card 
                size="small" 
                styles={{ body: { padding: '12px 16px', cursor: 'pointer' } }}
                onClick={() => setActiveTab(item.tab)}
                hoverable
              >
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>{item.label}</span>}
                  value={item.value} prefix={item.icon}
                  valueStyle={{ fontWeight: 600, fontSize: 20 }}
                  suffix={item.sub ? <span style={{ fontSize: 12, color: '#52c41a', marginLeft: 6 }}>{item.sub}</span> : undefined}
                />
              </Card>
            </Col>
          ))}
        </Row>
      )}

      {overview?.recent_releases?.length ? (
        <Card 
          size="small" 
          title="近期发布"
          extra={
            <Space size="small">
              <Button 
                size="small" 
                type={releaseStatusFilter === 'all' ? 'primary' : 'default'}
                onClick={() => setReleaseStatusFilter('all')}
              >
                全部
              </Button>
              <Button 
                size="small" 
                type={releaseStatusFilter === 'success' ? 'primary' : 'default'}
                onClick={() => setReleaseStatusFilter('success')}
              >
                成功
              </Button>
              <Button 
                size="small" 
                type={releaseStatusFilter === 'failed' ? 'primary' : 'default'}
                onClick={() => setReleaseStatusFilter('failed')}
              >
                失败
              </Button>
            </Space>
          }
        >
          <Table 
            size="small" 
            rowKey="release_id" 
            pagination={false} 
            dataSource={
              releaseStatusFilter === 'all' 
                ? overview.recent_releases 
                : overview.recent_releases.filter(r => r.status === releaseStatusFilter)
            }
            columns={[
              { title: '发布 ID', dataIndex: 'release_id', width: 140, render: (v) => <code style={{ fontSize: 12 }}>{v}</code> },
              { title: '类型', dataIndex: 'release_type', width: 100, render: (v) => <Tag>{v}</Tag> },
              { title: '状态', dataIndex: 'status', width: 90, render: (v) => { const c: 'success' | 'error' | 'default' = v === 'success' ? 'success' : v === 'failed' ? 'error' : 'default'; return <Badge status={c} text={v} /> } },
              { title: '操作人', dataIndex: 'created_by', width: 100 },
              { title: '时间', dataIndex: 'created_at', render: (v) => v?.slice(0, 16) || '-' },
            ]}
          />
        </Card>
      ) : null}
    </Flex>
  )
}
