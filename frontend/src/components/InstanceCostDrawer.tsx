/**
 * 实例费用抽屉 —— 费用管理与 CMDB 资产的联动组件。
 *
 * 在主机列表行打开，按云实例 ID（阿里云导入的资产 instance_id 即 ECS ID，
 * 与 cloud_bills.instance_id 天然对齐）展示：
 * - 本月 / 上月费用与环比
 * - 近 6 个月月度费用柱状图（ECharts）
 * - 本月计费项构成 Top 表
 *
 * 账单未匹配时（自建主机 / 未同步）给出明确空态，而不是空白。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Drawer, Typography, Table, Tag, Statistic, Row, Col, Empty, Spin, Space } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowUpOutlined, ArrowDownOutlined, PayCircleOutlined,
} from '@ant-design/icons'
import ReactECharts from 'echarts-for-react'
import { financeExtraApi } from '@/api'
import type { FinanceDimensionItem } from '@/types/api'

const { Text } = Typography

interface Props {
  open: boolean
  /** 云实例 ID（资产 instance_id） */
  instanceId: string
  /** 展示名（主机名），仅用于标题 */
  hostName?: string
  onClose: () => void
}

export default function InstanceCostDrawer({ open, instanceId, hostName, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ['instance-cost', instanceId],
    queryFn: () => financeExtraApi.getInstanceCost(instanceId, 6),
    enabled: open && !!instanceId,
    staleTime: 5 * 60 * 1000,
  })

  const chartOption = useMemo(() => {
    const monthly = data?.monthly || []
    if (!monthly.length) return null
    return {
      grid: { left: 56, right: 16, top: 24, bottom: 32 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => (v == null ? '-' : `¥${Number(v).toFixed(2)}`),
      },
      xAxis: {
        type: 'category',
        data: monthly.map((p) => p.month.slice(2)),
        axisLabel: { color: '#8c8c8c' },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: '#8c8c8c' },
        splitLine: { lineStyle: { color: '#f0f0f0' } },
      },
      series: [{
        type: 'bar',
        name: '月度费用',
        barMaxWidth: 28,
        data: monthly.map((p) => p.amount),
        itemStyle: { borderRadius: [4, 4, 0, 0] },
      }],
    }
  }, [data])

  const itemColumns: ColumnsType<FinanceDimensionItem> = [
    { title: '计费项', dataIndex: 'name', ellipsis: true },
    { title: '占比', dataIndex: 'ratio', width: 80, align: 'right', render: (v: number) => `${(v * 100).toFixed(0)}%` },
    {
      title: '费用', dataIndex: 'amount', width: 110, align: 'right',
      render: (v: number) => `¥${(v || 0).toFixed(2)}`,
    },
  ]

  const momDelta = data ? data.current_month_amount - data.previous_month_amount : 0
  const momUp = momDelta > 0

  return (
    <Drawer
      title={
        <Space>
          <PayCircleOutlined style={{ color: '#1677ff' }} />
          实例费用 · {hostName || instanceId}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={620}
      destroyOnClose
    >
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '80px 0' }}><Spin size="large" tip="查询账单..." /></div>
      ) : !data || !data.matched ? (
        <Empty
          description={
            <span>
              账单中未找到该实例的消费记录
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>
                可能原因：自建主机（无云账单）、云账号尚未纳管、或当月账单未同步
              </Text>
            </span>
          }
          style={{ padding: '60px 0' }}
        />
      ) : (
        <>
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={8}>
              <Statistic
                title={`本月费用（${data.current_month}）`}
                value={data.current_month_amount}
                precision={2}
                prefix="¥"
              />
            </Col>
            <Col span={8}>
              <Statistic title="上月费用" value={data.previous_month_amount} precision={2} prefix="¥" />
            </Col>
            <Col span={8}>
              {data.previous_month_amount > 0 ? (
                <Statistic
                  title="环比变化"
                  value={Math.abs(momDelta)}
                  precision={2}
                  prefix={momUp ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
                  suffix={momUp ? '↑' : '↓'}
                  valueStyle={{ color: momUp ? '#cf1322' : '#3f8600', fontSize: 22 }}
                />
              ) : (
                <Statistic title="环比变化" value="无上月数据" valueStyle={{ fontSize: 16 }} />
              )}
            </Col>
          </Row>

          {chartOption && (
            <div style={{ marginBottom: 16 }}>
              <Text strong style={{ display: 'block', marginBottom: 8 }}>近 {data.monthly.length} 个月费用趋势</Text>
              <ReactECharts option={chartOption} style={{ height: 200 }} notMerge />
            </div>
          )}

          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            本月计费项构成 <Tag>{data.current_month}</Tag>
          </Text>
          <Table<FinanceDimensionItem>
            rowKey="name"
            size="small"
            columns={itemColumns}
            dataSource={data.by_billing_item}
            pagination={false}
            locale={{ emptyText: <Empty description="本月暂无明细" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          />
        </>
      )}
    </Drawer>
  )
}
