/**
 * 仪表盘首页（/）——平台驾驶舱。
 *
 * 在原资产统计骨架上升级为运维驾驶舱：
 * - 顶排态势卡：活跃告警 / 本月云消费 / AI 活跃 / 资产总览（按权限动态显隐）
 * - 资产明细卡：主机（健康度）、虚拟机（运行比）、集群 / 域名 / 证书 / 应用
 * - 告警 24h 趋势迷你柱状图（ECharts）
 * - 快捷入口：高频操作直达
 *
 * 数据源：dashboard/overview + alerts/stats + finance/overview + ai/observability/stats，
 * 每个卡片独立 useQuery，单源失败不影响整页。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Row, Col, Card, Statistic, Space, Typography, Tag, Progress, Empty, Button, Flex,
} from 'antd'
import {
  DesktopOutlined, ClusterOutlined, GlobalOutlined, AppstoreOutlined,
  AlertOutlined, AccountBookOutlined, RobotOutlined, CloudServerOutlined,
  ArrowRightOutlined, CodeOutlined, NodeIndexOutlined, SafetyCertificateOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { useNavigate } from 'react-router-dom'
import { aiPlatformApi, cockpitApi, dashboardApi, financeApi } from '@/api'
import { usePermission } from '@/hooks/usePermission'

const { Text } = Typography

/** 快捷入口（按权限过滤）。 */
const QUICK_ACTIONS = [
  { label: 'AI 工作台', path: '/ai/workbench', icon: <RobotOutlined />, permission: 'module.ai.ops' },
  { label: '脚本执行', path: '/tools/script', icon: <CodeOutlined />, permission: 'module.tools.scripts' },
  { label: '日志查询', path: '/monitoring/logs', icon: <NodeIndexOutlined />, permission: 'module.monitoring.logs' },
  { label: '费用总览', path: '/finance/overview', icon: <AccountBookOutlined />, permission: 'module.finance.overview' },
  { label: '证书管理', path: '/assets/certificates', icon: <SafetyCertificateOutlined />, permission: 'module.assets.certificates' },
]

export default function DashboardPage() {
  const navigate = useNavigate()
  const { has } = usePermission()
  // 告警卡数据来自 /monitoring/alerts/stats（要求 module.monitoring.rules），
  // 卡片显隐与接口权限保持一致——只按 live 开会把无 rules 的用户引到 403 兜底的 0 上
  const canAlertCard = has('module.monitoring.rules')
  const canFinance = has('module.finance.overview')
  const canAI = has('module.ai.ops')

  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: dashboardApi.overview,
  })

  const { data: alertStats } = useQuery({
    queryKey: ['cockpit-alert-stats'],
    queryFn: () => cockpitApi.alertStats(24),
    enabled: canAlertCard,
    refetchInterval: 60 * 1000,
  })

  const { data: financeOverview } = useQuery({
    queryKey: ['cockpit-finance'],
    queryFn: () => financeApi.overview({}),
    enabled: canFinance,
    staleTime: 10 * 60 * 1000,
  })

  const { data: aiStats } = useQuery({
    queryKey: ['cockpit-ai-stats'],
    queryFn: aiPlatformApi.observabilityStats,
    enabled: canAI,
    staleTime: 5 * 60 * 1000,
  })

  const summary = (data?.summary ?? {}) as Record<string, number>
  const totalHosts = Number(summary.hosts_total ?? 0)
  const hostsHealthy = Number(summary.hosts_healthy ?? 0)
  const hostsAbnormal = Number(summary.hosts_abnormal ?? 0)
  const totalVms = Number(summary.vms_total ?? 0)
  const vmsRunning = Number(summary.vms_running ?? 0)
  const hostHealthPct = totalHosts ? Math.round((hostsHealthy / totalHosts) * 100) : 0
  const vmRunPct = totalVms ? Math.round((vmsRunning / totalVms) * 100) : 0

  const alertTrendOption = useMemo(() => {
    const trend = alertStats?.hourly_trend || []
    if (!trend.length) return null
    return {
      grid: { left: 36, right: 8, top: 8, bottom: 20 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: trend.map((_, i) => `${24 - trend.length + i + 1}h`),
        axisLabel: { color: '#bfbfbf', fontSize: 10 },
      },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#bfbfbf', fontSize: 10 }, splitLine: { show: false } },
      series: [{
        type: 'bar', barMaxWidth: 10, data: trend,
        itemStyle: { color: trend.some((v) => v > 0) ? '#fa8c16' : '#d9d9d9', borderRadius: [2, 2, 0, 0] },
      }],
    }
  }, [alertStats])

  const severityTags = (alertStats?.by_severity || [])
    .filter((s) => s.count > 0)
    .map((s) => (
      <Tag key={s.key} color={s.key === 'critical' || s.key === 'error' ? 'red' : s.key === 'warning' ? 'orange' : 'blue'}>
        {s.key} {s.count}
      </Tag>
    ))

  const quickActions = QUICK_ACTIONS.filter((a) => !a.permission || has(a.permission))

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%', padding: '0 0 24px 0' }}>
      {/* ── 态势总览 ── */}
      <Row gutter={[16, 16]}>
        {canAlertCard && (
          <Col xs={24} sm={12} lg={6}>
            <Card hoverable loading={isLoading} onClick={() => navigate('/monitoring/alerts')}>
              <Statistic
                title="活跃告警"
                value={alertStats?.active_count ?? 0}
                prefix={<AlertOutlined style={{ color: (alertStats?.active_count ?? 0) > 0 ? '#fa8c16' : undefined }} />}
                valueStyle={{ color: (alertStats?.active_count ?? 0) > 0 ? '#fa8c16' : undefined }}
              />
              <div style={{ marginTop: 4, minHeight: 22 }}>
                {severityTags.length ? <Space size={4} wrap>{severityTags}</Space> : <Text type="secondary">24h 无新增</Text>}
              </div>
            </Card>
          </Col>
        )}
        {canFinance && (
          <Col xs={24} sm={12} lg={6}>
            <Card hoverable loading={isLoading} onClick={() => navigate('/finance/overview')}>
              <Statistic
                title="本月云消费"
                value={financeOverview?.total ?? 0}
                precision={2}
                prefix="¥"
              />
              <div style={{ marginTop: 4, minHeight: 22 }}>
                {financeOverview && financeOverview.prev_total > 0 ? (
                  (() => {
                    // 与费用总览页同口径：金额变动 + 百分比，避免只显示裸数字看不出量级
                    const pct = ((financeOverview.mom_delta / financeOverview.prev_total) * 100).toFixed(1)
                    return (
                      <Text type={financeOverview.mom_delta > 0 ? 'danger' : 'success'}>
                        环比 {financeOverview.mom_delta > 0 ? '+' : ''}{financeOverview.mom_delta.toFixed(2)}（{financeOverview.mom_delta > 0 ? '+' : ''}{pct}%）
                      </Text>
                    )
                  })()
                ) : <Text type="secondary">暂无环比数据</Text>}
              </div>
            </Card>
          </Col>
        )}
        {canAI && (
          <Col xs={24} sm={12} lg={6}>
            <Card hoverable loading={isLoading} onClick={() => navigate('/ai/dashboard')}>
              <Statistic
                title="今日 AI 对话"
                value={aiStats?.today_messages ?? 0}
                prefix={<RobotOutlined style={{ color: '#1677ff' }} />}
              />
              <div style={{ marginTop: 4, minHeight: 22 }}>
                <Text type="secondary">累计 {aiStats?.total_sessions ?? 0} 会话 · {aiStats?.active_users ?? 0} 用户</Text>
              </div>
            </Card>
          </Col>
        )}
        <Col xs={24} sm={12} lg={6}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/hosts')}>
            <Statistic title="资产总览" value={totalHosts + totalVms} prefix={<CloudServerOutlined />} />
            <div style={{ marginTop: 4, minHeight: 22 }}>
              <Text type="secondary">
                主机 {totalHosts} · 虚机 {totalVms} · 集群 {Number(summary.clusters_total ?? 0)}
              </Text>
            </div>
          </Card>
        </Col>
      </Row>

      {/* ── 告警趋势 + 快捷入口 ── */}
      <Row gutter={[16, 16]}>
        {canAlertCard && (
          <Col xs={24} lg={14}>
            <Card title="告警触发趋势（24h）" size="small">
              {alertTrendOption ? (
                <ReactECharts option={alertTrendOption} style={{ height: 140 }} notMerge />
              ) : (
                <Empty description="暂无告警数据" image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ padding: '30px 0' }} />
              )}
            </Card>
          </Col>
        )}
        <Col xs={24} lg={10}>
          <Card title="快捷入口" size="small">
            <Flex wrap gap="small" >
              {quickActions.map((action) => (
                <Button
                  key={action.path}
                  icon={action.icon}
                  onClick={() => navigate(action.path)}
                  style={{ margin: 4 }}
                >
                  {action.label}
                </Button>
              ))}
              {quickActions.length === 0 && <Text type="secondary">暂无可用入口</Text>}
            </Flex>
          </Card>
        </Col>
      </Row>

      {/* ── 资产明细 ── */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/hosts')}>
            <Statistic title="主机" value={totalHosts} prefix={<DesktopOutlined />} />
            <Progress
              percent={hostHealthPct} size="small" status={hostsAbnormal > 0 ? 'exception' : 'normal'}
              format={() => `健康 ${hostsHealthy}/${totalHosts}`}
              style={{ marginTop: 8 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/vms')}>
            <Statistic title="虚拟机" value={totalVms} prefix={<CloudServerOutlined />} />
            <Progress
              percent={vmRunPct} size="small"
              format={() => `运行 ${vmsRunning}/${totalVms}`}
              style={{ marginTop: 8 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/clusters')}>
            <Statistic title="集群" value={Number(summary.clusters_total ?? 0)} prefix={<ClusterOutlined />} />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">点击查看集群健康摘要 <ArrowRightOutlined /></Text>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/apps')}>
            <Statistic title="应用" value={Number(summary.applications_total ?? 0)} prefix={<AppstoreOutlined />} />
            <div style={{ marginTop: 8 }}>
              <Tag color="green">活跃 {Number(summary.applications_active ?? 0)}</Tag>
              <Tag>维护 {Number(summary.applications_maintenance ?? 0)}</Tag>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/domains')}>
            <Statistic title="域名" value={Number(summary.domains_total ?? 0)} prefix={<GlobalOutlined />} />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">点击查看到期情况 <ArrowRightOutlined /></Text>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card hoverable loading={isLoading} onClick={() => navigate('/assets/certificates')}>
            <Statistic title="证书" value={Number(summary.certificates_total ?? 0)} prefix={<SafetyCertificateOutlined />} />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">点击查看剩余有效期 <ArrowRightOutlined /></Text>
            </div>
          </Card>
        </Col>
      </Row>
    </Space>
  )
}
