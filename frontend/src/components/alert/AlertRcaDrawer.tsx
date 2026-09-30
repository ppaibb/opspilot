/**
 * 告警根因分析抽屉 —— 塔斯汀式闭环的入口组件。
 *
 * 三步闭环：
 * 1. 「AI 分析」：调后端真 LLM 分析（自动注入同类告警历史人工根因），结果落 pending 记录；
 * 2. 人工补充：填写真实根因与处理方式（"真正发生了什么"）；
 * 3. 评价 AI：准确 / 部分准确 / 不准确 —— 反哺 AI 准确率统计与后续分析的 few-shot。
 *
 * 同屏展示该类告警的历史根因列表（经验沉淀检索）。
 */
import { useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import {
  Drawer, Button, Space, Typography, Tag, App, Empty, Input, Descriptions,
  Card, Radio,
} from 'antd'
import { RobotOutlined, HistoryOutlined, CheckCircleOutlined } from '@ant-design/icons'
import client from '@/api/client'
import { extractAxiosDetail } from '@/hooks/usePermission'

const { Text } = Typography
const { TextArea } = Input

interface AnalysisResult {
  root_cause: string
  impact: string
  suggestions: Array<{ title: string; content: string }>
  mode: string
  root_cause_id: number
  history_count: number
}

/** GET /monitoring/alerts/root-causes 返回的历史根因条目（根因三元组沉淀记录）。 */
interface RootCauseHistoryItem {
  id: number
  alert_name?: string
  instance?: string
  ai_rating?: string
  human_root_cause?: string
  human_resolution?: string
  username?: string
  created_at?: string
}

interface Props {
  open: boolean
  alert: { name: string; instance?: string; severity?: string; summary?: string; description?: string } | null
  onClose: () => void
}

export default function AlertRcaDrawer({ open, alert, onClose }: Props) {
  const { message } = App.useApp()
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null)
  const [humanRootCause, setHumanRootCause] = useState('')
  const [resolution, setResolution] = useState('')
  const [rating, setRating] = useState<'accurate' | 'partial' | 'inaccurate'>('accurate')

  const analyzeMutation = useMutation({
    mutationFn: () =>
      client.post('/monitoring/alerts/analyze', {
        alert_name: alert!.name,
        instance: alert!.instance || '',
        severity: alert!.severity || '',
        description: alert!.description || alert!.summary || '',
      }).then((r) => r.data as AnalysisResult),
    onSuccess: (result) => {
      setAnalysis(result)
      if (result.mode === 'fallback') message.warning('AI 不可用，已返回降级提示（含历史根因参考）')
      else message.success(`AI 分析完成${result.history_count ? `（参考了 ${result.history_count} 条历史根因）` : ''}`)
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? 'AI 分析失败'),
  })

  const feedbackMutation = useMutation({
    mutationFn: () =>
      client.post(`/monitoring/alerts/root-causes/${analysis!.root_cause_id}/feedback`, {
        human_root_cause: humanRootCause,
        human_resolution: resolution,
        ai_rating: rating,
      }).then((r) => r.data),
    onSuccess: () => {
      message.success('根因已沉淀：后续同类告警的 AI 分析将参考这条经验')
      onClose()
      setAnalysis(null); setHumanRootCause(''); setResolution('')
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  // 同类告警历史根因（含人工反馈的记录）。
  const { data: history = [] } = useQuery({
    queryKey: ['alert-rca-history', alert?.name],
    queryFn: () => client.get<RootCauseHistoryItem[]>('/monitoring/alerts/root-causes', { params: { alert_name: alert!.name, limit: 10 } })
      .then((r) => r.data.filter((item) => item.human_root_cause)),
    enabled: open && !!alert?.name,
  })

  const RATING_LABEL = { accurate: '准确', partial: '部分准确', inaccurate: '不准确' } as const

  return (
    <Drawer
      title={
        <Space size={8}>
          <RobotOutlined style={{ color: '#1677ff' }} />
          根因分析 · {alert?.name || ''}
          {analysis?.mode === 'fallback' && <Tag color="orange">降级</Tag>}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={720}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Descriptions size="small" column={2} bordered>
          <Descriptions.Item label="实例">{alert?.instance || '-'}</Descriptions.Item>
          <Descriptions.Item label="级别">{alert?.severity || '-'}</Descriptions.Item>
          <Descriptions.Item label="摘要" span={2}>{alert?.summary || '-'}</Descriptions.Item>
        </Descriptions>

        {!analysis ? (
          <Card size="small">
            <Space direction="vertical" style={{ width: '100%' }} size="small">
              <Text type="secondary">
                AI 将结合平台上下文与同类告警的历史人工根因给出诊断；分析后请补充真实根因并评价 AI——这是经验沉淀的关键一步。
              </Text>
              <Button type="primary" icon={<RobotOutlined />} loading={analyzeMutation.isPending}
                onClick={() => analyzeMutation.mutate()} disabled={!alert?.name}>
                开始 AI 分析
              </Button>
            </Space>
          </Card>
        ) : (
          <>
            <Card size="small" title="AI 诊断">
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <div><Text strong>根因：</Text><Text>{analysis.root_cause}</Text></div>
                <div><Text strong>影响：</Text><Text>{analysis.impact}</Text></div>
                {analysis.suggestions?.length > 0 && (
                  <div>
                    <Text strong>建议步骤：</Text>
                    {analysis.suggestions.map((s, i) => (
                      <div key={i} style={{ padding: '2px 0 2px 12px' }}>
                        <Text strong style={{ fontSize: 12 }}>{s.title}</Text>
                        <Text style={{ fontSize: 12 }}>：{s.content}</Text>
                      </div>
                    ))}
                  </div>
                )}
              </Space>
            </Card>

            <Card size="small" title={<Space><CheckCircleOutlined />人工反馈（闭环关键）</Space>}>
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <Text strong style={{ fontSize: 12 }}>真正发生了什么（人工根因）</Text>
                <TextArea rows={3} value={humanRootCause} onChange={(e) => setHumanRootCause(e.target.value)}
                  placeholder="如：上游发布引入慢查询，导致连接池耗尽" />
                <Text strong style={{ fontSize: 12 }}>怎么解决的（可选）</Text>
                <TextArea rows={2} value={resolution} onChange={(e) => setResolution(e.target.value)}
                  placeholder="如：回滚版本 + 加慢查询告警" />
                <Text strong style={{ fontSize: 12 }}>AI 诊断评价</Text>
                <Radio.Group value={rating} onChange={(e) => setRating(e.target.value)}>
                  {(Object.keys(RATING_LABEL) as Array<keyof typeof RATING_LABEL>).map((key) => (
                    <Radio.Button key={key} value={key}>{RATING_LABEL[key]}</Radio.Button>
                  ))}
                </Radio.Group>
                <Button type="primary" loading={feedbackMutation.isPending}
                  disabled={!humanRootCause.trim()} onClick={() => feedbackMutation.mutate()}>
                  沉淀根因与评价
                </Button>
              </Space>
            </Card>
          </>
        )}

        <Card size="small" title={<Space><HistoryOutlined />该类告警历史根因（{history.length}）</Space>}>
          {history.length === 0 ? (
            <Empty description="暂无人工根因——你的每次沉淀都会让 AI 更准" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            history.map((item) => (
              <div key={item.id} style={{ padding: '6px 0', borderBottom: '1px solid #f5f5f5' }}>
                <Space size={6} wrap>
                  <Tag color={item.ai_rating === 'accurate' ? 'green' : item.ai_rating === 'inaccurate' ? 'red' : 'orange'}>
                    {item.ai_rating === 'accurate' ? 'AI 准确' : item.ai_rating === 'inaccurate' ? 'AI 不准' : item.ai_rating === 'partial' ? 'AI 部分准确' : '未评价'}
                  </Tag>
                  <Text strong style={{ fontSize: 12 }}>{item.instance || '-'}</Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>{item.created_at} · {item.username}</Text>
                </Space>
                <div style={{ fontSize: 12, marginTop: 2 }}>{item.human_root_cause}</div>
                {item.human_resolution && (
                  <Text type="secondary" style={{ fontSize: 11 }}>处理：{item.human_resolution}</Text>
                )}
              </div>
            ))
          )}
        </Card>
      </Space>
    </Drawer>
  )
}
