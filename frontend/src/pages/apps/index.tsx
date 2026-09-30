import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Row, Col, Input, Select, Space, Badge, Tag, Empty, Skeleton, Button, Flex, Statistic, Table, Segmented, Popconfirm, App,
} from 'antd'
import {
  AppstoreOutlined, SearchOutlined, PlusOutlined, RocketOutlined,
  CheckCircleOutlined, WarningOutlined, ExclamationCircleOutlined, UnorderedListOutlined, AppstoreAddOutlined, DeleteOutlined, EditOutlined,
} from '@ant-design/icons'
import { applicationsApi } from '@/api'
import GroupSidebar from '@/components/GroupSidebar'
import type { ApplicationResponse } from '@/types/api'
import type { ColumnsType } from 'antd/es/table'

const ENV_COLOR: Record<string, string> = { prod: 'red', test: 'blue', dev: 'default', mixed: 'purple' }
const ENV_LABEL: Record<string, string> = { prod: '生产', test: '测试', dev: '开发', mixed: '混合' }
const STATUS_COLOR: Record<string, 'success' | 'processing' | 'error' | 'default' | 'warning'> = { active: 'success', maintenance: 'warning', offline: 'default' }
const STATUS_LABEL: Record<string, string> = { active: '运行中', maintenance: '维护中', offline: '已下线' }
const HEALTH_ICON: Record<string, React.ReactNode> = {
  healthy: <CheckCircleOutlined style={{ color: '#52c41a' }} />,
  warning: <WarningOutlined style={{ color: '#faad14' }} />,
  degraded: <ExclamationCircleOutlined style={{ color: '#ff4d4f' }} />,
  offline: <ExclamationCircleOutlined style={{ color: '#8c8c8c' }} />,
  unknown: <ExclamationCircleOutlined style={{ color: '#8c8c8c' }} />,
}

export default function AppsListPage() {
  const navigate = useNavigate()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [groupFilter, setGroupFilter] = useState<number | null>(null)
  const [categoryFilter, setCategoryFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [envFilter, setEnvFilter] = useState('')
  const [viewMode, setViewMode] = useState<'card' | 'table'>('card')
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])

  const { data: apps = [], isLoading } = useQuery({
    queryKey: ['applications', search, groupFilter, categoryFilter, statusFilter, envFilter],
    queryFn: () => applicationsApi.list({
      q: search || undefined,
      group_id: groupFilter || undefined,
      category: categoryFilter || undefined,
      status: statusFilter || undefined,
      environment: envFilter || undefined,
    }),
  })

  const deleteMut = useMutation({
    mutationFn: (appId: string) => applicationsApi.delete(appId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['applications'] })
      message.success('应用已删除')
    },
    onError: () => message.error('删除失败'),
  })

  const categories = Array.from(new Set(apps.map(app => app.category).filter(Boolean)))
  const healthStats = {
    healthy: apps.filter(app => app.health_status === 'healthy').length,
    warning: apps.filter(app => app.health_status === 'warning').length,
    degraded: apps.filter(app => app.health_status === 'degraded').length,
    total: apps.length,
  }

  const columns: ColumnsType<ApplicationResponse> = [
    {
      title: '应用名称',
      dataIndex: 'name',
      width: 200,
      render: (name, record) => (
        <Flex vertical gap={4}>
          <span style={{ fontWeight: 500 }}>{name}</span>
          <code style={{ fontSize: 11, color: '#8c8c8c' }}>{record.app_id}</code>
        </Flex>
      ),
    },
    {
      title: '环境',
      dataIndex: 'environment',
      width: 100,
      render: (env) => <Tag color={ENV_COLOR[env] || 'default'}>{ENV_LABEL[env] || env}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status) => <Badge status={STATUS_COLOR[status]} text={STATUS_LABEL[status] || status} />,
    },
    {
      title: '健康状态',
      dataIndex: 'health_status',
      width: 100,
      render: (health) => (
        <Space size={4}>
          {HEALTH_ICON[health] || HEALTH_ICON.unknown}
          <span style={{ fontSize: 13 }}>{health}</span>
        </Space>
      ),
    },
    {
      title: '分类',
      dataIndex: 'category',
      width: 120,
      render: (cat) => cat || '-',
    },
    {
      title: '描述',
      dataIndex: 'description',
      ellipsis: true,
      render: (desc) => desc || '-',
    },
    {
      title: '负责人',
      dataIndex: 'owner',
      width: 100,
      render: (owner) => owner || '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={(e) => {
              e.stopPropagation()
              navigate(`/apps/${record.app_id}`)
            }}
          >
            详情
          </Button>
          <Popconfirm
            title="确认删除该应用？"
            description="删除后数据无法恢复"
            onConfirm={(e) => {
              e?.stopPropagation()
              deleteMut.mutate(record.app_id)
            }}
            okText="删除"
            cancelText="取消"
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={(e) => e.stopPropagation()}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      <Flex gap={16} style={{ flex: 1, minWidth: 0 }}>
        <GroupSidebar
          resourceType="application"
          selectedGroupId={groupFilter}
          onSelect={setGroupFilter}
          selectedIds={selectedKeys}
        />

        <Flex vertical gap={12} style={{ flex: 1, minWidth: 0 }}>
          {/* 顶部概览统计卡片 */}
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>应用总数</span>}
                  value={healthStats.total}
                  prefix={<RocketOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20 }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>健康</span>}
                  value={healthStats.healthy}
                  prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>告警</span>}
                  value={healthStats.warning}
                  prefix={<WarningOutlined style={{ color: '#faad14', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20, color: healthStats.warning > 0 ? '#faad14' : 'inherit' }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
                <Statistic
                  title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>异常</span>}
                  value={healthStats.degraded}
                  prefix={<ExclamationCircleOutlined style={{ color: '#ff4d4f', marginRight: 6 }} />}
                  valueStyle={{ fontWeight: 600, fontSize: 20, color: healthStats.degraded > 0 ? '#ff4d4f' : 'inherit' }}
                />
              </Card>
            </Col>
          </Row>

          <Flex justify="space-between" wrap gap={8}>
            <Space wrap>
              <Input
                placeholder="搜索应用名称、ID、负责人"
                prefix={<SearchOutlined />}
                allowClear
                value={search}
                onChange={e => setSearch(e.target.value)}
                style={{ width: 220 }}
              />
              <Select
                placeholder="环境"
                allowClear
                value={envFilter || undefined}
                onChange={val => setEnvFilter(val || '')}
                style={{ width: 120 }}
                options={[
                  { value: 'prod', label: '生产' },
                  { value: 'test', label: '测试' },
                  { value: 'dev', label: '开发' },
                ]}
              />
              <Select
                placeholder="状态"
                allowClear
                value={statusFilter || undefined}
                onChange={val => setStatusFilter(val || '')}
                style={{ width: 120 }}
                options={[
                  { value: 'active', label: '运行中' },
                  { value: 'maintenance', label: '维护中' },
                  { value: 'offline', label: '已下线' },
                ]}
              />
              {categories.length > 0 && (
                <Select
                  placeholder="分类"
                  allowClear
                  value={categoryFilter || undefined}
                  onChange={val => setCategoryFilter(val || '')}
                  style={{ width: 140 }}
                  options={categories.map(cat => ({ value: cat, label: cat }))}
                />
              )}
            </Space>
            <Space>
              <Segmented
                value={viewMode}
                onChange={setViewMode}
                options={[
                  { label: '卡片', value: 'card', icon: <AppstoreAddOutlined /> },
                  { label: '表格', value: 'table', icon: <UnorderedListOutlined /> },
                ]}
              />
              <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/apps/new')}>新建应用</Button>
            </Space>
          </Flex>

      {isLoading ? (
        viewMode === 'card' ? (
          <Row gutter={[16, 16]}>
            {[1, 2, 3, 4, 5, 6].map(i => (
              <Col key={i} xs={24} sm={12} lg={8} xl={6}>
                <Card size="small"><Skeleton active paragraph={{ rows: 3 }} /></Card>
              </Col>
            ))}
          </Row>
        ) : (
          <Card size="small"><Skeleton active paragraph={{ rows: 8 }} /></Card>
        )
      ) : apps.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={search || categoryFilter || statusFilter || envFilter ? '未找到匹配的应用' : '暂无应用'}
        >
          {!search && !categoryFilter && !statusFilter && !envFilter && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/apps/new')}>新建应用</Button>
          )}
        </Empty>
      ) : viewMode === 'table' ? (
        <Card size="small" styles={{ body: { padding: 0 } }}>
          <Table
            dataSource={apps}
            columns={columns}
            rowKey="app_id"
            size="small"
            rowSelection={{
              selectedRowKeys: selectedKeys,
              onChange: (keys) => setSelectedKeys(keys as string[]),
            }}
            pagination={{
              pageSize: 20,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 个应用`,
            }}
            onRow={(record) => ({
              onClick: () => navigate(`/apps/${record.app_id}`),
              style: { cursor: 'pointer' },
            })}
          />
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {apps.map((app: ApplicationResponse) => (
            <Col key={app.app_id} xs={24} sm={12} lg={8} xl={6}>
              <Card
                size="small"
                hoverable
                onClick={() => navigate(`/apps/${app.app_id}`)}
                styles={{ body: { padding: 16 } }}
              >
                <Flex vertical gap={12}>
                  <Flex justify="space-between" align="start">
                    <Flex vertical gap={4} style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ fontSize: 15, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={app.name}>
                        {app.name}
                      </span>
                      <code style={{ fontSize: 11, color: '#8c8c8c' }}>{app.app_id}</code>
                    </Flex>
                    <div style={{ fontSize: 18, marginLeft: 8 }}>
                      {HEALTH_ICON[app.health_status] || HEALTH_ICON.unknown}
                    </div>
                  </Flex>

                  <Flex gap={4} wrap>
                    <Tag color={ENV_COLOR[app.environment] || 'default'} style={{ margin: 0 }}>
                      {ENV_LABEL[app.environment] || app.environment}
                    </Tag>
                    <Badge status={STATUS_COLOR[app.status]} text={STATUS_LABEL[app.status] || app.status} />
                  </Flex>

                  {app.category && (
                    <div style={{ fontSize: 12, color: '#8c8c8c' }}>
                      <AppstoreOutlined style={{ marginRight: 4 }} />
                      {app.category}
                    </div>
                  )}

                  {app.description && (
                    <div
                      style={{
                        fontSize: 12,
                        color: '#64748b',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        lineHeight: '1.5',
                        minHeight: 36,
                      }}
                      title={app.description}
                    >
                      {app.description}
                    </div>
                  )}

                  {app.owner && (
                    <div style={{ fontSize: 12, color: '#8c8c8c', marginTop: 'auto' }}>
                      负责人：{app.owner}
                    </div>
                  )}
                </Flex>
              </Card>
            </Col>
          ))}
        </Row>
      )}
        </Flex>
      </Flex>
    </Flex>
  )
}
