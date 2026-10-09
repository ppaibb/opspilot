/**
 * Grafana 钻取按钮：走 grafana_sso 换一次性票据，再由后端反代注入身份进入 Grafana。
 * 未配置 grafana_url 时灰显 + Tooltip 说明原因。
 *
 * 票据只有 90 秒有效期，所以刻意在「点击时」才换取，避免页面挂久了票据过期。
 *
 * 数据源模式：
 * - 默认（不传 datasource）：PromQL 钻取，落默认指标数据源 opspilot-vm；
 * - 传 datasource：按 panes 格式带指定数据源（日志页传 opspilot-vlogs + LogsQL，
 *   钻取必须查日志库，不能落回指标库）。
 */
import { useState } from 'react'
import { Button, Tooltip, App } from 'antd'
import { ExportOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { monitoringApi, systemApi } from '@/api'
import type { GrafanaDatasourceRef } from './grafanaDatasources'

export interface GrafanaExploreParams {
  /** PromQL / LogsQL 表达式 */
  expr?: string
  /** 主机 IP / instance 过滤（仅指标模式生效） */
  host?: string
  /** 相对时间窗，如 now-1h */
  from?: string
  to?: string
  /** 指定 Grafana 数据源（如日志库 opspilot-vlogs）；缺省用指标库 */
  datasource?: GrafanaDatasourceRef
  /** 打开入口文案 */
  label?: string
}

function buildExplorePath(p: GrafanaExploreParams): string {
  const from = p.from || 'now-1h'
  const to = p.to || 'now'
  const range = { from, to }

  // 日志数据源：LogsQL 原样作为表达式，panes 格式带指定数据源
  if (p.datasource) {
    const pane = {
      datasource: p.datasource.uid,
      queries: [
        {
          refId: 'A',
          expr: (p.expr || '').trim(),
          queryType: 'range',
          datasource: { type: p.datasource.type, uid: p.datasource.uid },
        },
      ],
      range,
    }
    return `/explore?schemaVersion=1&panes=${encodeURIComponent(JSON.stringify({ A: pane }))}`
  }

  // 指标数据源：兼容旧 left JSON，PromQL 组装 + 可选 instance 过滤
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
    range,
  }
  return `/explore?left=${encodeURIComponent(JSON.stringify(left))}`
}

export default function GrafanaLink({
  expr,
  host,
  from,
  to,
  datasource,
  size = 'small',
  type = 'link',
  label = 'Grafana 更多数据',
}: GrafanaExploreParams & { size?: 'small' | 'middle'; type?: 'link' | 'default' | 'primary'; label?: string }) {
  const { message } = App.useApp()
  const [opening, setOpening] = useState(false)
  const merged: GrafanaExploreParams = { expr, host, from, to, datasource }

  const { data: config } = useQuery({
    queryKey: ['monitoring-config'],
    queryFn: systemApi.getMonitoring,
    staleTime: 60_000,
    retry: false,
  })
  const grafanaConfigured = !!config?.grafana_url

  const handleClick = async () => {
    setOpening(true)
    try {
      // 点击时取票：SSO 票据 90 秒过期，预取的链接大概率已经失效。
      const sso = await monitoringApi.grafanaSsoLink()
      const entry = new URL(sso.url, window.location.origin)
      const token = entry.searchParams.get('opspilot_sso')
      // 反代前缀从入口地址推导（如 /grafana），目标页拼在它后面，保证落在同一入口下。
      const prefix = entry.pathname.replace(/\/+$/, '')
      const target = new URL(`${prefix}${buildExplorePath(merged)}`, window.location.origin)
      if (token) target.searchParams.set('opspilot_sso', token)
      window.open(target.toString(), '_blank', 'noopener,noreferrer')
    } catch {
      message.error('获取 Grafana 免密链接失败，请稍后重试')
    } finally {
      setOpening(false)
    }
  }

  const btn = (
    <Button
      size={size}
      type={type as 'link'}
      icon={<ExportOutlined />}
      loading={opening}
      disabled={!grafanaConfigured}
      onClick={handleClick}
    >
      {label}
    </Button>
  )

  if (!grafanaConfigured) {
    return (
      <Tooltip title="未配置 Grafana 地址，无法钻取。到「系统设置 → 系统管理」填写后可用。">
        <span>{btn}</span>
      </Tooltip>
    )
  }
  return btn
}
