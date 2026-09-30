/**
 * 数据库管理页（/assets/databases）—— 类 Yearning 的 SQL 工单平台。
 *
 * 工作台 + 五个工具 Tab：
 * 1. 工作台：今日待办流水线 / 我的申请 / 最近工单 / 快捷开工；
 * 2. SQL 工单：提交（后端规则引擎预审）→ 多级审批 → 批准 → 执行 → 逐条结果；
 * 3. 自助查询：只读语句直接执行（≤500 行），需本人名下生效中的查询授权（管理员豁免）；
 * 4. 查询授权 / 5. 数据源 / 6. 审批流 / 7. 审核规则。
 *
 * 分权设计（对齐 Yearning 三权分离）：提交（action.db.submit）/ 执行
 * （action.db.execute）/ 审批（action.db.approve）相互独立。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Tabs, Flex, Space, Alert, Badge, Card, Row, Col, Statistic,
} from 'antd'
import {
  PlayCircleOutlined, DatabaseOutlined, FileTextOutlined, SearchOutlined,
  SafetyCertificateOutlined, ExclamationCircleOutlined, KeyOutlined, NodeIndexOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { dbApi } from '@/api'
import { TICKET_FETCH_LIMIT } from './types'
import { TicketsTab } from './TicketsPanel'
import { QueryTab } from './QueryPanel'
import { GrantsTab } from './GrantsPanel'
import { InstancesTab } from './InstancesPanel'
import { ApprovalFlowsTab } from './ApprovalFlowsPanel'
import { AuditRulesTab } from './AuditRulesPanel'
import { WorkbenchPanel } from './WorkbenchPanel'

// ───────────────────────── 页面主体 ─────────────────────────

export default function DatabasesPage() {
  const { data: instances = [], isLoading: instancesLoading, refetch: refetchInstances } = useQuery({
    queryKey: ['db-instances'],
    queryFn: dbApi.listInstances,
  })
  const { data: allTickets = [], isLoading: ticketsLoading, refetch: refetchTickets } = useQuery({
    queryKey: ['db-tickets', 'all'],
    queryFn: () => dbApi.listTickets({ limit: TICKET_FETCH_LIMIT }),
  })

  const [tab, setTab] = useState('home')
  const [statusFilter, setStatusFilter] = useState('')
  const [keyword, setKeyword] = useState('')

  const pendingCount = useMemo(() => allTickets.filter((t) => t.status === 'pending').length, [allTickets])
  const approvedCount = useMemo(() => allTickets.filter((t) => t.status === 'approved').length, [allTickets])
  const scheduledCount = useMemo(
    () => allTickets.filter((t) => t.status === 'approved' && t.execute_after).length,
    [allTickets],
  )
  const errorInstanceCount = useMemo(() => instances.filter((i) => i.status === 'error').length, [instances])

  const filteredTickets = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return allTickets.filter((t) => {
      if (statusFilter && t.status !== statusFilter) return false
      if (!kw) return true
      return (
        t.title.toLowerCase().includes(kw)
        || t.instance_name.toLowerCase().includes(kw)
        || t.submitter.toLowerCase().includes(kw)
        || (t.database || '').toLowerCase().includes(kw)
      )
    })
  }, [allTickets, statusFilter, keyword])

  const jumpToTickets = (status: string) => {
    setTab('tickets')
    setStatusFilter(status)
  }

  const tabLabel = (icon: React.ReactNode, text: string, count?: number) => (
    <Space size={6}>
      {icon}
      {text}
      {count ? <Badge count={count} size="small" style={{ boxShadow: 'none' }} /> : null}
    </Space>
  )

  return (
    <Flex vertical gap={12} style={{ height: '100%', paddingBottom: tab === 'home' ? 24 : 0 }}>
      {/* 统计只在工作台；其它 Tab 把纵向空间全给工作区 */}
      {tab === 'home' && (
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }} onClick={() => setTab('instances')} style={{ cursor: 'pointer' }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>数据源</span>}
                value={instances.length}
                prefix={<DatabaseOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20 }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }} onClick={() => setTab('instances')} style={{ cursor: 'pointer' }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>连接异常</span>}
                value={errorInstanceCount}
                prefix={<ExclamationCircleOutlined style={{ color: '#ff4d4f', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: errorInstanceCount > 0 ? '#ff4d4f' : 'inherit' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }} onClick={() => jumpToTickets('pending')} style={{ cursor: 'pointer' }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>待审批</span>}
                value={pendingCount}
                suffix={<span style={{ fontSize: 14 }}>张</span>}
                prefix={<SafetyCertificateOutlined style={{ color: '#faad14', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: pendingCount > 0 ? '#faad14' : 'inherit' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }} onClick={() => jumpToTickets('approved')} style={{ cursor: 'pointer' }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>待执行</span>}
                value={approvedCount}
                suffix={<span style={{ fontSize: 14 }}>张</span>}
                prefix={<PlayCircleOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20 }}
              />
            </Card>
          </Col>
        </Row>
      )}

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'home',
            label: tabLabel(<ThunderboltOutlined />, '工作台'),
            children: (
              <WorkbenchPanel
                tickets={allTickets}
                onCreateTicket={() => setTab('tickets')}
                onOpenQuery={() => setTab('query')}
                onJumpTickets={(status) => jumpToTickets(status)}
              />
            ),
          },
          {
            key: 'tickets',
            label: tabLabel(<FileTextOutlined />, 'SQL 工单', pendingCount),
            children: (
              <TicketsTab
                tickets={filteredTickets}
                loading={ticketsLoading}
                refetch={refetchTickets}
                instances={instances}
                statusFilter={statusFilter}
                setStatusFilter={setStatusFilter}
                keyword={keyword}
                setKeyword={setKeyword}
                onGoSelfQuery={() => setTab('query')}
              />
            ),
          },
          { key: 'query', label: tabLabel(<SearchOutlined />, '自助查询'), children: <QueryTab instances={instances} /> },
          { key: 'grants', label: tabLabel(<KeyOutlined />, '查询授权'), children: <GrantsTab instances={instances} /> },
          {
            key: 'instances',
            label: tabLabel(<DatabaseOutlined />, '数据源'),
            children: <InstancesTab instances={instances} loading={instancesLoading} refetch={() => refetchInstances()} />,
          },
          { key: 'flows', label: tabLabel(<NodeIndexOutlined />, '审批流'), children: <ApprovalFlowsTab /> },
          { key: 'rules', label: tabLabel(<SafetyCertificateOutlined />, '审核规则'), children: <AuditRulesTab /> },
        ]}
      />
      {scheduledCount > 0 && (
        <Alert type="info" showIcon style={{ marginTop: 12 }}
          message={`当前有 ${scheduledCount} 张已批准工单等待定时执行（由系统在设定时间自动执行，无需人工干预）`}
        />
      )}
    </Flex>
  )
}
