/**
 * AI 全景看板（/ai/dashboard）。
 *
 * 平台 AI 能力的运行观测页，数据全部来自真实表（无 mock）：
 * - 概览指标：会话数 / 消息量 / 今日活跃 / 参与用户
 * - 近 7 天消息趋势（ECharts 柱状图）
 * - 提问 vs 回复构成
 * - AI 审计日志（操作人 / 意图 / 风险 / 状态）
 * - 运行时状态卡：当前激活的 Provider / 模型与就绪状态
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Row, Col, Statistic, Table, Tag, Typography, Flex, Space, Badge, Button, Empty,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, RobotOutlined, MessageOutlined, UserOutlined } from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { useNavigate } from 'react-router-dom'
import { aiApi, aiPlatformApi } from '@/api'
import type { AIAuditLogItem, AISessionItem, LLMConfigListResponse } from '@/types/api'

const { Text } = Typography

const RISK_COLOR: Record<string, string> = { high: 'red', medium: 'orange', low: 'green' }

export default function AIDashboardPage() {
  const navigate = useNavigate()

  const { data: stats, isLoading, refetch } = useQuery({
    queryKey: ['ai-observability-stats'],
    queryFn: aiPlatformApi.observabilityStats,
  })

  const { data: audit } = useQuery({
    queryKey: ['ai-observability-audit'],
    queryFn: aiPlatformApi.observabilityAudit,
  })

  const { data: sessions } = useQuery({
    queryKey: ['ai-sessions'],
    queryFn: aiApi.listSessions,
  })

  const { data: configList } = useQuery({
    queryKey: ['ai-llm-configs'],
    queryFn: aiPlatformApi.listLLMConfigs,
  })

  const activeConfig = useMemo(
    () => (configList as LLMConfigListResponse | undefined)?.items.find((c) => c.is_active),
    [configList],
  )

  const recentSessions = (sessions as AISessionItem[] || [])
    .slice()
    .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
    .slice(0, 8)

  const trendOption = useMemo(() => {
    // 后端返回近 7 天数组（索引 0 = 6 天前）；横轴用相对日期。
    const days = stats?.chat_trend_7d || []
    const labels = days.map((_, idx) => {
      const d = new Date()
      d.setDate(d.getDate() - (days.length - 1 - idx))
      return `${d.getMonth() + 1}/${d.getDate()}`
    })
    return {
      grid: { left: 56, right: 24, top: 32, bottom: 36 },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: labels, axisLabel: { color: '#8c8c8c' } },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#8c8c8c' }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [{
        type: 'bar',
        name: '消息数',
        barMaxWidth: 32,
        data: days,
        itemStyle: { borderRadius: [4, 4, 0, 0] },
      }],
    }
  }, [stats])

  const auditColumns: ColumnsType<AIAuditLogItem> = [
    { title: '时间', dataIndex: 'time', width: 165 },
    { title: '用户', dataIndex: 'user', width: 110, render: (v: string) => <Text strong>{v || '-'}</Text> },
    { title: 'Agent', dataIndex: 'agent', width: 110 },
    { title: '意图', dataIndex: 'intent', ellipsis: true },
    {
      title: '风险', dataIndex: 'risk', width: 85,
      render: (v: string) => <Tag color={RISK_COLOR[v] || 'default'}>{v || 'low'}</Tag>,
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v: string) => (
        <Badge status={v === 'success' || v === 'ok' ? 'success' : v === 'deny' ? 'error' : 'default'} text={v || '-'} />
      ),
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>AI 全景看板</Typography.Title>
          <Text type="secondary">AI 会话、消息与审计的运行观测（数据来自真实表统计）</Text>
        </div>
        <Space>
          <Button type="primary" icon={<RobotOutlined />} onClick={() => navigate('/ai/workbench')}>
            打开 Agent 工作台
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Flex>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={6}>
          <Card loading={isLoading}>
            <Statistic title="累计会话" value={stats?.total_sessions ?? 0} prefix={<RobotOutlined style={{ marginRight: 6 }} />} />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card loading={isLoading}>
            <Statistic title="累计消息" value={stats?.total_messages ?? 0} prefix={<MessageOutlined style={{ marginRight: 6 }} />} />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card loading={isLoading}>
            <Statistic title="今日消息" value={stats?.today_messages ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card loading={isLoading}>
            <Statistic title="参与用户" value={stats?.active_users ?? 0} prefix={<UserOutlined style={{ marginRight: 6 }} />} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <Card title="近 7 天消息趋势" size="small">
            <ReactECharts option={trendOption} style={{ height: 260 }} notMerge />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="运行时模型" size="small" style={{ marginBottom: 16 }}>
            {activeConfig ? (
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Space>
                  <Badge status="success" text={<Text strong>{activeConfig.name}</Text>} />
                  <Tag color="green">已激活</Tag>
                </Space>
                <Space size={6} wrap>
                  <Tag>{activeConfig.provider}</Tag>
                  <Text code>{activeConfig.model}</Text>
                  <Text type="secondary">temp {activeConfig.temperature.toFixed(2)}</Text>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  更新于 {activeConfig.updated_at || '-'}
                </Text>
                <Button size="small" style={{ alignSelf: 'flex-start', marginTop: 4 }} onClick={() => navigate('/ai/llm-config')}>
                  管理模型配置
                </Button>
              </Space>
            ) : (
              <Space direction="vertical">
                <Text type="secondary">未设置激活配置，AI 问答使用环境变量兜底。</Text>
                <Button size="small" onClick={() => navigate('/ai/llm-config')}>去配置</Button>
              </Space>
            )}
          </Card>
          <Card
            title={`消息构成`}
            size="small"
          >
            <Row gutter={16}>
              <Col span={12}>
                <Statistic title="用户提问" value={stats?.user_messages ?? 0} valueStyle={{ fontSize: 22 }} />
              </Col>
              <Col span={12}>
                <Statistic title="助手回复" value={stats?.assistant_messages ?? 0} valueStyle={{ fontSize: 22 }} />
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={14}>
          <Card title="AI 审计日志" size="small">
            <Table<AIAuditLogItem>
              rowKey="id"
              size="small"
              columns={auditColumns}
              dataSource={(audit?.logs || []).slice(0, 10)}
              pagination={false}
              locale={{ emptyText: <Empty description="暂无审计记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="最近会话" size="small">
            {recentSessions.length === 0 ? (
              <Empty description="暂无会话" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ padding: '32px 0' }} />
            ) : (
              recentSessions.map((session) => (
                <Flex
                  key={session.session_id}
                  justify="space-between"
                  align="center"
                  style={{ padding: '6px 0', borderBottom: '1px solid #f5f5f5', cursor: 'pointer' }}
                  onClick={() => navigate('/ai/workbench', { state: { sessionId: session.session_id } })}
                >
                  <Text ellipsis style={{ maxWidth: 220 }}>{session.title || '未命名会话'}</Text>
                  <Space size={8}>
                    <Tag>{session.message_count} 条</Tag>
                    <Text type="secondary" style={{ fontSize: 12 }}>{session.updated_at?.slice(5, 16)}</Text>
                  </Space>
                </Flex>
              ))
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
