import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Table, Input, Select, DatePicker, Typography, Flex, Button, Space, Tag, Tooltip, Statistic, Card, Row, Col,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { SearchOutlined, ReloadOutlined, DownloadOutlined } from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import { financeApi } from '@/api'
import type { CloudAccountItem, CloudBillItem } from '@/types/api'

const { Text } = Typography

const SUBSCRIPTION_META: Record<string, { color: string; label: string }> = {
  Subscription: { color: 'blue', label: '预付费' },
  PayAsYouGo: { color: 'orange', label: '后付费' },
}

function formatAmount(value: number): string {
  return `¥${(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export default function FinanceBillsPage() {
  const [month, setMonth] = useState<Dayjs>(dayjs())
  const [accountId, setAccountId] = useState<number | undefined>(undefined)
  const [product, setProduct] = useState('')
  const [keyword, setKeyword] = useState('')
  const [subscription, setSubscription] = useState<string | undefined>(undefined)
  const [page, setPage] = useState(1)
  const pageSize = 50

  const monthKey = month.format('YYYY-MM')

  const { data: accounts = [] } = useQuery({
    queryKey: ['finance-accounts'],
    queryFn: financeApi.listAccounts,
  })

  const queryParams = useMemo(
    () => ({
      month: monthKey,
      account_id: accountId,
      product: product || undefined,
      keyword: keyword || undefined,
      subscription_type: subscription,
      page,
      page_size: pageSize,
    }),
    [monthKey, accountId, product, keyword, subscription, page],
  )

  const { data: bills, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['finance-bills', queryParams],
    queryFn: () => financeApi.listBills(queryParams),
    placeholderData: (prev) => prev,
  })

  const { data: productGroups } = useQuery({
    queryKey: ['finance-bills-products', monthKey, accountId],
    queryFn: () => financeApi.groupBills({ dimension: 'product', month: monthKey, account_id: accountId }),
  })

  const accountOptions = [
    { value: 0, label: '全部账号' },
    ...accounts.map((a: CloudAccountItem) => ({ value: a.id, label: a.name })),
  ]
  const productOptions = (productGroups?.items || []).slice(0, 30).map((p) => ({
    value: p.name,
    label: p.name,
  }))

  const columns: ColumnsType<CloudBillItem> = [
    {
      title: '日期', dataIndex: 'billing_date', width: 105, fixed: 'left',
      render: (v: string) => <Text>{v?.slice(5)}</Text>,
    },
    {
      title: '账号', dataIndex: 'account_name', width: 130, ellipsis: true, fixed: 'left',
    },
    {
      title: '产品', dataIndex: 'product_name', width: 170, ellipsis: true,
      render: (v: string, record) => (
        <Tooltip title={record.product_detail || v}>
          <Text strong>{v || record.product_code || '未知产品'}</Text>
        </Tooltip>
      ),
    },
    {
      title: '计费项', dataIndex: 'billing_item_name', width: 180, ellipsis: true,
      render: (v: string, record) => v || record.billing_item_code || '-',
    },
    {
      title: '付费方式', dataIndex: 'subscription_type', width: 90,
      render: (v: string) => {
        const meta = SUBSCRIPTION_META[v]
        return meta ? <Tag color={meta.color}>{meta.label}</Tag> : <Tag>{v || '-'}</Tag>
      },
    },
    {
      title: '实例 / 资源', dataIndex: 'instance_id', width: 190, ellipsis: true,
      render: (v: string, record) => (
        <Tooltip title={record.resource_name ? `${record.resource_name}（${v}）` : v}>
          {record.resource_name || v || <Text type="secondary">-</Text>}
        </Tooltip>
      ),
    },
    { title: '地域', dataIndex: 'region', width: 110, ellipsis: true },
    {
      title: '费用', dataIndex: 'pretax_amount', width: 120, align: 'right', fixed: 'right',
      sorter: (a, b) => a.pretax_amount - b.pretax_amount,
      defaultSortOrder: 'descend',
      render: (v: number) => <Text strong>{formatAmount(v)}</Text>,
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }} wrap gap="small">
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>账单明细</Typography.Title>
          <Text type="secondary">按天粒度的实例账单，来自阿里云费用中心（仅保留近 18 个月）</Text>
        </div>
        <Space wrap>
          <DatePicker
            picker="month"
            value={month}
            allowClear={false}
            disabledDate={(d) => d.isAfter(dayjs(), 'month')}
            onChange={(v) => { if (v) { setMonth(v); setPage(1) } }}
          />
          <Select
            style={{ width: 170 }}
            value={accountId ?? 0}
            options={accountOptions}
            onChange={(v) => { setAccountId(v || undefined); setPage(1) }}
            showSearch optionFilterProp="label"
          />
          <Select
            style={{ width: 190 }}
            placeholder="按产品筛选"
            allowClear
            value={product || undefined}
            options={productOptions}
            onChange={(v) => { setProduct(v || ''); setPage(1) }}
            showSearch optionFilterProp="label"
          />
          <Select
            style={{ width: 120 }}
            placeholder="付费方式"
            allowClear
            value={subscription}
            options={[
              { value: 'Subscription', label: '预付费' },
              { value: 'PayAsYouGo', label: '后付费' },
            ]}
            onChange={(v) => { setSubscription(v); setPage(1) }}
          />
          <Input.Search
            style={{ width: 220 }}
            placeholder="实例 ID / 资源名 / 计费项"
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => setPage(1)}
            prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
          />
          <Button icon={<DownloadOutlined />} onClick={() => { window.open(financeApi.exportBillsUrl({ month: monthKey, account_id: accountId, product, keyword, subscription_type: subscription }), '_blank') }}>导出 CSV</Button>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()} loading={isFetching}>刷新</Button>
        </Space>
      </Flex>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic
              title="筛选结果合计"
              value={bills?.grand_total ?? 0}
              precision={2}
              prefix="¥"
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic title="账单行数（当前筛选）" value={bills?.total ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic
              title="账期"
              value={monthKey}
              valueStyle={{ fontSize: 20 }}
            />
          </Card>
        </Col>
      </Row>

      <Table<CloudBillItem>
        rowKey="id"
        columns={columns}
        dataSource={bills?.items || []}
        loading={isLoading}
        scroll={{ x: 1180 }}
        pagination={{
          current: page,
          pageSize,
          total: bills?.total ?? 0,
          showSizeChanger: false,
          showTotal: (t) => `共 ${t} 行`,
          onChange: (p) => setPage(p),
        }}
        size="middle"
      />
    </div>
  )
}
