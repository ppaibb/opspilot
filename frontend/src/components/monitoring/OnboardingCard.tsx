/**
 * 新手引导卡片：装采集 → 首指标 → 规则 → 测试通知。
 * 可放在监控首页 / 自检面板旁；步骤按当前配置给出下一步动作。
 */
import { Card, Steps, Button, Typography, Space } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { monitoringApi, systemApi } from '@/api'

const { Text } = Typography

export default function OnboardingCard() {
  const navigate = useNavigate()
  const { data: config } = useQuery({ queryKey: ['monitoring-config'], queryFn: systemApi.getMonitoring })
  const { data: rulesData } = useQuery({ queryKey: ['alert-rules'], queryFn: () => monitoringApi.listRules() })
  const { data: channelsData } = useQuery({ queryKey: ['monitoring-channels'], queryFn: systemApi.getChannels })

  const vmReady = !!config?.vm_url
  const logsReady = !!config?.vlogs_url
  const hasRule = (rulesData?.rules || []).some((r) => r.enabled)
  const hasChannel = (channelsData?.channels || []).some((c) => (c as { enabled?: boolean }).enabled !== false)

  // 四步都完成后自动收起，不再占看板位置
  if (vmReady && logsReady && hasRule && hasChannel) {
    return null
  }

  const steps = [
    {
      title: '装采集',
      description: vmReady ? '监控连接已配置' : '配置 VM / Alloy 采集',
      status: vmReady ? ('finish' as const) : ('process' as const),
    },
    {
      title: '首指标',
      description: logsReady ? '日志存储已接入' : '验证指标/日志可查',
      status: logsReady ? ('finish' as const) : vmReady ? ('process' as const) : ('wait' as const),
    },
    {
      title: '规则',
      description: hasRule ? '已有启用规则' : '创建第一条告警规则',
      status: hasRule ? ('finish' as const) : logsReady || vmReady ? ('process' as const) : ('wait' as const),
    },
    {
      title: '测试通知',
      description: hasChannel ? '通知渠道已配置' : '配置渠道并发送测试',
      status: hasChannel ? ('finish' as const) : hasRule ? ('process' as const) : ('wait' as const),
    },
  ]

  const nextAction = !vmReady
    ? { label: '去配置监控连接', to: '/settings/system' }
    : !hasRule
      ? { label: '创建告警规则', to: '/monitoring/alert-rules' }
      : !hasChannel
        ? { label: '配置通知渠道', to: '/settings/system' }
        : { label: '打开告警列表', to: '/monitoring/alerts' }

  return (
    <Card
      size="small"
      title="快速上手"
      style={{ marginBottom: 16 }}
      extra={<Text type="secondary" style={{ fontSize: 12 }}>装采集 → 首指标 → 规则 → 测试通知</Text>}
    >
      <Steps size="small" items={steps} />
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" size="small" onClick={() => navigate(nextAction.to)}>
          {nextAction.label}
        </Button>
        <Button size="small" onClick={() => navigate('/monitoring/logs')}>
          查日志
        </Button>
        <Button size="small" onClick={() => navigate('/monitoring/metrics')}>
          查指标
        </Button>
      </Space>
    </Card>
  )
}
