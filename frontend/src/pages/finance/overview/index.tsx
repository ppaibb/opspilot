import { useMemo, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import {
  Card, Row, Col, Select, DatePicker, Statistic, Table, Typography, Flex, Button, Space, Empty, Tooltip,
  Progress, Drawer, Form, InputNumber, Switch, App,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowUpOutlined, ArrowDownOutlined, ReloadOutlined, ArrowRightOutlined, SettingOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import dayjs, { type Dayjs } from 'dayjs'
import { useNavigate } from 'react-router-dom'
import { financeApi, financeExtraApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { CloudAccountItem, FinanceDimensionItem } from '@/types/api'

const { Text } = Typography

function formatAmount(value: number): string {
  if (!Number.isFinite(value)) return '¥0.00'
  if (Math.abs(value) >= 100000) return `¥${(value / 10000).toFixed(2)} 万`
  return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function FinanceOverviewPage() {
  const navigate = useNavigate()
  const [month, setMonth] = useState<Dayjs>(dayjs())
  const [accountId, setAccountId] = useState<number | undefined>(undefined)
  const monthKey = month.format('YYYY-MM')

  const { data: accounts = [] } = useQuery({
    queryKey: ['finance-accounts'],
    queryFn: financeApi.listAccounts,
  })

  const { has } = usePermission()
  const { message } = App.useApp()
  const canManageBudget = has('action.finance.accounts.manage')
  const [budgetDrawerOpen, setBudgetDrawerOpen] = useState(false)
  const [budgetForm] = Form.useForm()

  const { data: budget, refetch: refetchBudget } = useQuery({
    queryKey: ['finance-budget'],
    queryFn: financeExtraApi.getBudget,
    staleTime: 60 * 1000,
  })

  const saveBudget = useMutation({
    mutationFn: (values: { monthly_budget: number; warn_ratio: number; critical_ratio: number; enabled: boolean }) =>
      financeExtraApi.updateBudget(values),
    onSuccess: () => {
      message.success('预算配置已保存，告警每小时评估一次')
      setBudgetDrawerOpen(false)
      refetchBudget()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const { data: overview, isLoading, refetch } = useQuery({
    queryKey: ['finance-overview', monthKey, accountId],
    queryFn: () => financeApi.overview({ month: monthKey, account_id: accountId }),
  })

  const accountOptions = useMemo(
    () => [
      { value: 0, label: '全部账号' },
      ...accounts.map((a: CloudAccountItem) => ({ value: a.id, label: a.name })),
    ],
    [accounts],
  )

  const momUp = (overview?.mom_delta ?? 0) > 0
  const momRatio = overview && overview.prev_total > 0
    ? ((overview.mom_delta / overview.prev_total) * 100).toFixed(1)
    : null

  const subscriptionSummary = useMemo(() => {
    const items = overview?.by_subscription || []
    if (!items.length) return '暂无付费方式数据'
    return items
      .map((s) => `${s.name === 'Subscription' ? '预付费' : s.name === 'PayAsYouGo' ? '后付费' : s.name} ${formatAmount(s.amount)}`)
      .join(' / ')
  }, [overview])

  const dailyOption = useMemo(() => ({
    grid: { left: 72, right: 24, top: 32, bottom: 40 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: number) => (v == null ? '-' : `¥${Number(v).toFixed(2)}`),
    },
    xAxis: {
      type: 'category',
      data: (overview?.daily_trend || []).map((p) => p.period.slice(5)),
      axisLabel: { color: '#8c8c8c' },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#8c8c8c', formatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)) },
      splitLine: { lineStyle: { color: '#f0f0f0' } },
    },
    series: [{
      type: 'line',
      name: '当日费用',
      smooth: true,
      symbol: 'circle',
      symbolSize: 6,
      data: (overview?.daily_trend || []).map((p) => p.amount),
      areaStyle: { opacity: 0.12 },
      lineStyle: { width: 2 },
    }],
  }), [overview])

  const monthlyOption = useMemo(() => ({
    grid: { left: 72, right: 24, top: 32, bottom: 40 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: number) => (v == null ? '-' : `¥${Number(v).toFixed(2)}`),
    },
    xAxis: {
      type: 'category',
      data: (overview?.monthly_trend || []).map((p) => p.period),
      axisLabel: { color: '#8c8c8c' },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#8c8c8c', formatter: (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)) },
      splitLine: { lineStyle: { color: '#f0f0f0' } },
    },
    series: [{
      type: 'bar',
      name: '月度费用',
      barMaxWidth: 36,
      data: (overview?.monthly_trend || []).map((p) => p.amount),
    }],
  }), [overview])

  const pieOption = useMemo(() => ({
    tooltip: {
      trigger: 'item',
      formatter: (p: { name: string; value: number; percent: number }) =>
        `${p.name}<br/>${formatAmount(p.value)}（${p.percent}%）`,
    },
    legend: { type: 'scroll', orient: 'vertical', right: 8, top: 16, bottom: 16, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['36%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 4, borderColor: '#fff', borderWidth: 2 },
      label: { show: false },
      data: (overview?.by_product || []).map((p) => ({ name: p.name || '未分类', value: p.amount })),
    }],
  }), [overview])

  const productColumns: ColumnsType<FinanceDimensionItem> = [
    {
      title: '产品', dataIndex: 'name', ellipsis: true,
      render: (v: string) => <Text strong>{v || '未分类'}</Text>,
    },
    {
      title: '占比', dataIndex: 'ratio', width: 90, align: 'right',
      render: (v: number) => `${(v * 100).toFixed(1)}%`,
    },
    {
      title: '费用', dataIndex: 'amount', width: 130, align: 'right',
      render: (v: number) => <Text>{formatAmount(v)}</Text>,
    },
  ]

  const hasData = (overview?.total ?? 0) > 0 || (overview?.daily_trend?.length ?? 0) > 0

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }} wrap gap="small">
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>费用总览</Typography.Title>
          <Text type="secondary">
            数据来自阿里云费用中心账单同步
            {overview?.last_synced_at ? `，最近同步 ${overview.last_synced_at}` : ''}
          </Text>
        </div>
        <Space>
          <DatePicker
            picker="month"
            value={month}
            allowClear={false}
            disabledDate={(d) => d.isAfter(dayjs(), 'month')}
            onChange={(v) => v && setMonth(v)}
          />
          <Select
            style={{ width: 180 }}
            value={accountId ?? 0}
            options={accountOptions}
            onChange={(v) => setAccountId(v || undefined)}
            showSearch optionFilterProp="label"
          />
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Flex>

      {accounts.length === 0 && (
        <Card style={{ marginBottom: 16 }}>
          <Empty
            description={
              <span>
                还没有纳管云账号，账单数据为空。
                <Button type="link" size="small" icon={<ArrowRightOutlined />} onClick={() => navigate('/finance/accounts')}>
                  去添加云账号
                </Button>
              </span>
            }
          />
        </Card>
      )}

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title={`${monthKey} 总消费（优惠后）`}
              value={overview?.total ?? 0}
              precision={2}
              prefix="¥"
            />
            <div style={{ marginTop: 8 }}>
              {momRatio ? (
                <Text type={momUp ? 'danger' : 'success'}>
                  {momUp ? <ArrowUpOutlined /> : <ArrowDownOutlined />} 较上月 {momUp ? '+' : ''}{overview?.mom_delta.toFixed(2)}（{momRatio}%）
                </Text>
              ) : (
                <Text type="secondary">上月无数据，暂无环比</Text>
              )}
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic title="上月总消费" value={overview?.prev_total ?? 0} precision={2} prefix="¥" />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">{subscriptionSummary}</Text>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title="本月日均消费"
              value={
                overview && overview.daily_trend.length
                  ? overview.total / overview.daily_trend.length
                  : 0
              }
              precision={2}
              prefix="¥"
            />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">
                已出账 {overview?.daily_trend.length ?? 0} 天（账单约延迟 1 天）
              </Text>
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card
            loading={isLoading}
            extra={
              <Button
                size="small" type="text" icon={<SettingOutlined />}
                disabled={!canManageBudget}
                onClick={() => {
                  budgetForm.setFieldsValue({
                    monthly_budget: budget?.monthly_budget || 1000,
                    warn_ratio: budget?.warn_ratio ?? 0.8,
                    critical_ratio: budget?.critical_ratio ?? 1.0,
                    enabled: budget?.enabled ?? false,
                  })
                  setBudgetDrawerOpen(true)
                }}
              />
            }
          >
            {budget && budget.enabled && budget.monthly_budget > 0 ? (
              <>
                <Statistic
                  title={`预算使用（${budget.month}）`}
                  value={budget.spent}
                  suffix={`/ ¥${budget.monthly_budget.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`}
                  precision={2}
                  prefix="¥"
                />
                <Progress
                  percent={Math.round(budget.usage_ratio * 100)}
                  size="small"
                  status={budget.usage_ratio >= budget.critical_ratio ? 'exception' : budget.usage_ratio >= budget.warn_ratio ? 'active' : 'normal'}
                  style={{ marginTop: 10 }}
                />
              </>
            ) : (
              <>
                <Statistic title="预算告警" value="未启用" valueStyle={{ fontSize: 20 }} />
                <div style={{ marginTop: 8 }}>
                  <Text type="secondary">设置月度预算后自动告警 {canManageBudget ? '' : '（需管理权限）'}</Text>
                </div>
              </>
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <Card title={`${monthKey} 日消费趋势`} size="small">
            {hasData ? (
              <ReactECharts option={dailyOption} style={{ height: 300 }} notMerge />
            ) : (
              <Empty description="暂无账单数据" style={{ padding: '60px 0' }} />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="产品费用占比" size="small" extra={<Tooltip title="查看完整账单明细"><Button type="link" size="small" onClick={() => navigate('/finance/bills')}>明细</Button></Tooltip>}>
            {hasData ? (
              <ReactECharts option={pieOption} style={{ height: 300 }} notMerge />
            ) : (
              <Empty description="暂无账单数据" style={{ padding: '60px 0' }} />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={10}>
          <Card title="近 6 个月消费趋势" size="small">
            <ReactECharts option={monthlyOption} style={{ height: 260 }} notMerge />
          </Card>
        </Col>
        <Col xs={24} lg={7}>
          <Card title={`产品费用 Top（${monthKey}）`} size="small">
            <Table<FinanceDimensionItem>
              rowKey="name"
              size="small"
              columns={productColumns}
              dataSource={overview?.by_product || []}
              pagination={false}
              loading={isLoading}
              locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
        <Col xs={24} lg={7}>
          <Card title={`账号消费排名（${monthKey}）`} size="small">
            <Table<FinanceDimensionItem>
              rowKey="name"
              size="small"
              columns={[
                { title: '账号', dataIndex: 'name', ellipsis: true },
                {
                  title: '费用', dataIndex: 'amount', width: 120, align: 'right',
                  render: (v: number) => formatAmount(v),
                },
              ]}
              dataSource={overview?.by_account || []}
              pagination={false}
              loading={isLoading}
              locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
      </Row>

      {/* ── 预算设置抽屉 ── */}
      <Drawer
        title="预算告警设置"
        open={budgetDrawerOpen}
        onClose={() => setBudgetDrawerOpen(false)}
        width={480}
        destroyOnClose
        extra={
          <Space>
            <Button onClick={() => setBudgetDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saveBudget.isPending} onClick={() => budgetForm.submit()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form
          form={budgetForm}
          layout="vertical"
          onFinish={(values) => saveBudget.mutate(values)}
        >
          <Form.Item
            name="monthly_budget" label="月度预算（¥）"
            rules={[{ required: true, message: '请输入月度预算' }]}
            extra="所有云账号当月账单合计达到阈值时触发告警"
          >
            <InputNumber min={1} step={100} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="warn_ratio" label="警告阈值（占比）"
            rules={[{ required: true }]}
            extra="如 0.8 表示消费达预算 80% 时发警告告警"
          >
            <InputNumber min={0.1} max={2} step={0.05} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="critical_ratio" label="严重阈值（占比）"
            rules={[{ required: true }]}
            extra="需大于等于警告阈值，达到时发严重告警"
          >
            <InputNumber min={0.1} max={2} step={0.05} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="enabled" label="启用预算告警" valuePropName="checked" extra="每小时评估一次，经告警中心渠道推送（飞书/企微/Webhook）">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Drawer>
    </div>
  )
}
