/**
 * 采集车队纯函数：健康总览 + 异常置顶排序。
 * 单独成文件，便于契约测试。
 */

export interface FleetTarget {
  asset_id: string
  name: string
  ip: string
  enabled: boolean
  status: string
  last_checked_at?: string
  last_error?: string
  provider?: string
  metrics_enabled?: boolean
  last_metric_at?: string
  logs_enabled?: boolean
  last_log_at?: string
}

/** 灯色：有心跳=绿，无=灰，失败=红 */
export type LightState = 'green' | 'gray' | 'red'

const FAIL_STATUSES = new Set(['unreachable', 'error', 'failed', 'offline', 'down'])

export function lightState(opts: {
  enabled: boolean
  status?: string
  hasHeartbeat: boolean
  failed?: boolean
}): LightState {
  const status = (opts.status || '').toLowerCase()
  if (opts.failed || FAIL_STATUSES.has(status)) return 'red'
  if (!opts.enabled) return 'gray'
  if (opts.hasHeartbeat) return 'green'
  return 'gray'
}

export function metricLight(t: FleetTarget): LightState {
  const enabled = t.metrics_enabled ?? t.enabled
  const status = (t.status || '').toLowerCase()
  if (!enabled) return 'gray'
  if (t.last_error || FAIL_STATUSES.has(status)) return 'red'
  if (t.last_metric_at || isTargetHealthy(t)) return 'green'
  return 'gray'
}

export function logLight(t: FleetTarget): LightState {
  const enabled = t.logs_enabled ?? t.enabled
  const status = (t.status || '').toLowerCase()
  if (!enabled) return 'gray'
  if (t.last_error || FAIL_STATUSES.has(status)) return 'red'
  if (t.last_log_at) return 'green'
  return 'gray'
}

/** 顶部「日志在采 x/总数」：logs_enabled 且有心跳 */
export function logsCollectingCount(targets: FleetTarget[]): { collecting: number; total: number } {
  const total = targets.length
  const collecting = targets.filter((t) => logLight(t) === 'green').length
  return { collecting, total }
}

const UP_STATUSES = new Set(['up', 'online', 'healthy', 'running', 'ok'])

export function isTargetHealthy(t: FleetTarget): boolean {
  return !!t.enabled && UP_STATUSES.has((t.status || '').toLowerCase())
}

/** 异常（离线 / 未启用 / 心跳失败）置顶，其余按名称。 */
export function sortTargets(targets: FleetTarget[]): FleetTarget[] {
  return [...targets].sort((a, b) => {
    const ab = isTargetHealthy(a) ? 1 : 0
    const bb = isTargetHealthy(b) ? 1 : 0
    if (ab !== bb) return ab - bb
    return String(a.name || a.ip || '').localeCompare(String(b.name || b.ip || ''))
  })
}

export function fleetSummary(targets: FleetTarget[]): {
  total: number
  online: number
  offline: number
  disabled: number
  healthRate: number
} {
  const total = targets.length
  const online = targets.filter(isTargetHealthy).length
  const disabled = targets.filter((t) => !t.enabled).length
  const offline = total - online - disabled
  const healthRate = total > 0 ? Math.round((online / total) * 1000) / 10 : 0
  return { total, online, offline, disabled, healthRate }
}
