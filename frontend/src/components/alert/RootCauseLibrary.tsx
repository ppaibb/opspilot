/**
 * 根因库与 AI 准确率统计抽屉 —— 闭环的"沉淀可視化"。
 *
 * 上半：整体准确率 + 按告警名的准确率排行（AI 越用越准的可量化证据）；
 * 下半：根因记录流（AI 诊断摘要 + 人工根因 + 评价），支持按告警名检索。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Drawer, Input, Table, Tag, Typography, Space, Statistic, Card, Row, Col, Empty,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { HistoryOutlined, AimOutlined } from '@ant-design/icons'
import client from '@/api/client'

const { Text } = Typography

const RATING_TAG: Record<string, { color: string; label: string }> = {
  accurate: { color: 'green', label: '准确' },
  partial: { color: 'orange', label: '部分准确' },
  inaccurate: { color: 'red', label: '不准确' },
  pending: { color: 'default', label: '未评价' },
}

/** 取评价标签元信息（未评价/未知等级返回 undefined，由调用方兜底）。 */
function ratingMeta(rating?: string) {
  return rating ? RATING_TAG[rating] : undefined
}

/** GET /monitoring/alerts/root-causes/stats 响应（AI 准确率统计）。 */
interface RcaStats {
  overall_accuracy?: number
  total_rated?: number
  total_accurate?: number
  by_alert?: RcaAlertStat[]
}

/** 按告警名聚合的准确率统计行。 */
interface RcaAlertStat {
  alert_name: string
  total: number
  accuracy: number
  accurate: number
  partial: number
  inaccurate: number
}

/** GET /monitoring/alerts/root-causes 返回的根因记录（含 AI 诊断摘要）。 */
interface RcaRecord {
  id: number
  alert_name?: string
  instance?: string
  ai_rating?: string
  ai_diagnosis?: { root_cause?: string } | null
  human_root_cause?: string
  human_resolution?: string
  username?: string
  created_at?: string
}

export default function RootCauseLibrary({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [search, setSearch] = useState('')

  const { data: stats } = useQuery({
    queryKey: ['rca-stats'],
    queryFn: () => client.get<RcaStats>('/monitoring/alerts/root-causes/stats').then((r) => r.data),
    enabled: open,
  })

  const { data: records = [] } = useQuery({
    queryKey: ['rca-records', search],
    queryFn: () => client.get<RcaRecord[]>('/monitoring/alerts/root-causes', { params: { alert_name: search || undefined, limit: 100 } })
      .then((r) => r.data),
    enabled: open,
  })

  const statColumns: ColumnsType<RcaAlertStat> = [
    { title: '告警名', dataIndex: 'alert_name', ellipsis: true },
    { title: '样本', dataIndex: 'total', width: 70, align: 'right' },
    {
      title: '准确率', dataIndex: 'accuracy', width: 100, align: 'right',
      render: (v: number) => (
        <Text strong style={{ color: v >= 0.7 ? '#3f8600' : v >= 0.4 ? '#fa8c16' : '#cf1322' }}>
          {(v * 100).toFixed(0)}%
        </Text>
      ),
    },
    {
      title: '分布', key: 'dist', width: 160,
      render: (_v, r) => `${r.accurate}准 / ${r.partial}部分 / ${r.inaccurate}不准`,
    },
  ]

  return (
    <Drawer
      title={<Space><HistoryOutlined />根因库 · AI 准确率</Space>}
      open={open} onClose={onClose} width={820} destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Card size="small">
          <Row gutter={16}>
            <Col span={8}>
              <Statistic
                title="AI 综合准确率"
                value={stats?.overall_accuracy != null ? (stats.overall_accuracy * 100).toFixed(1) : '-'}
                suffix={stats?.overall_accuracy != null ? '%' : ''}
                prefix={<AimOutlined />}
              />
            </Col>
            <Col span={8}><Statistic title="已评价样本" value={stats?.total_rated ?? 0} /></Col>
            <Col span={8}><Statistic title="判断完全准确" value={stats?.total_accurate ?? 0} /></Col>
          </Row>
        </Card>

        {!!stats?.by_alert?.length && (
          <Card size="small" title="按告警名统计（Top 20）">
            <Table rowKey="alert_name" size="small" columns={statColumns} dataSource={stats.by_alert} pagination={false} />
          </Card>
        )}

        <Input.Search
          placeholder="按告警名检索根因记录" allowClear value={search}
          onChange={(e) => setSearch(e.target.value)}
        />

        {records.length === 0 ? (
          <Empty description="暂无根因记录——在活跃告警里做一次「根因分析」并沉淀人工反馈" style={{ padding: '40px 0' }} />
        ) : (
          records.map((item) => (
            <Card size="small" key={item.id}
              title={
                <Space size={6}>
                  <Text strong style={{ fontSize: 13 }}>{item.alert_name || '(未命名)'}</Text>
                  <Tag color={ratingMeta(item.ai_rating)?.color}>{ratingMeta(item.ai_rating)?.label || item.ai_rating}</Tag>
                  {item.instance && <Tag>{item.instance}</Tag>}
                </Space>
              }
              extra={<Text type="secondary" style={{ fontSize: 11 }}>{item.created_at} · {item.username}</Text>}
            >
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                {item.ai_diagnosis?.root_cause && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    AI：{String(item.ai_diagnosis.root_cause).slice(0, 160)}
                  </Text>
                )}
                {item.human_root_cause && (
                  <Text style={{ fontSize: 12 }}>
                    <Text strong>人工根因：</Text>{item.human_root_cause}
                  </Text>
                )}
                {item.human_resolution && (
                  <Text style={{ fontSize: 12 }} type="secondary">
                    处理：{item.human_resolution}
                  </Text>
                )}
              </Space>
            </Card>
          ))
        )}
      </Space>
    </Drawer>
  )
}
