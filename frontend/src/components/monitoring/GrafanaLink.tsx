/**
 * Grafana 钻取按钮：走现有 grafana_sso 换免密链接，可带条件（主机/时间窗/指标）。
 * 未配置 grafana_url 时灰显 + Tooltip 说明原因。
 */
import { useState } from 'react'
import { Button, Tooltip, App } from 'antd'
import { ExportOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { monitoringApi, systemApi } from '@/api'

export interface GrafanaExploreParams {
  /** PromQL 表达式 */
  expr?: string
  /** 主机 IP / instance 过滤 */
  host?: string
  /** 相对时间窗，如 now-1h */
  from?: string
  to?: string
  /** 打开入口文案 */
  label?: string
}

function buildExplorePath(p: GrafanaExploreParams): string {
  const from = p.from || 'now-1h'
  const to = p.to || 'now'
  let expr = (p.expr || '').trim()
  if (p.host && expr) {
    if (expr.includes('{')) {
      expr = expr.replace(/\{/, `{instance=~"${p.host}(:.*)?",`)
    } else {
      expr = `${expr}{instance=~"${p.host}(:.*)?"}`
    }
  } else if (p.host && !expr) {
    expr = `up{instance=~"${p.host}(:.*)?"}`
  }
  const left = {
    queries: expr ? [{ refId: 'A', expr, range: true, instant: false }] : [],
    range: { from, to },
  }
  return `/explore?left=${encodeURIComponent(JSON.stringify(left))}`
}

export default function GrafanaLink({
  expr,
  host,
  from,
  to,
  size = 'small',
  type = 'link',
  label = 'Grafana 更多数据',
}: GrafanaExploreParams & { size?: 'small' | 'middle'; type?: 'link' | 'default' | 'primary'; label?: string }) {
  const { message } = App.useApp()
  const [opening, setOpening] = useState(false)
  const merged: GrafanaExploreParams = { expr, host, from, to }

  const { data: config } = useQuery({
    queryKey: ['monitoring-config'],
    queryFn: systemApi.getMonitoring,
    staleTime: 60_000,
    retry: false,
  })
  const grafanaConfigured = !!config?.grafana_url

  const { data: sso, isLoading } = useQuery({
    queryKey: ['grafana-sso-link'],
    queryFn: () => monitoringApi.grafanaSsoLink(),
    retry: false,
    staleTime: 60_000,
    enabled: grafanaConfigured,
  })

  const disabled = !grafanaConfigured

  const handleClick = async () => {
    if (!sso?.url) {
      message.warning('Grafana 未配置或 SSO 链接不可用，请先在「系统设置 → 系统管理」填写 Grafana 地址')
      return
    }
    setOpening(true)
    try {
      const explorePath = buildExplorePath(merged)
      const url = new URL(sso.url, window.location.origin)
      url.searchParams.set('redirect_to', explorePath)
      window.open(url.toString(), '_blank', 'noopener,noreferrer')
    } finally {
      setOpening(false)
    }
  }

  const btn = (
    <Button
      size={size}
      type={type as 'link'}
      icon={<ExportOutlined />}
      loading={isLoading || opening}
      disabled={disabled}
      onClick={handleClick}
    >
      {label}
    </Button>
  )

  if (disabled) {
    return (
      <Tooltip title="未配置 Grafana 地址，无法钻取。到「系统设置 → 系统管理」填写后可用。">
        <span>{btn}</span>
      </Tooltip>
    )
  }
  return btn
}
