import { describe, it, expect, beforeEach } from 'vitest'
import {
  applyAlertFilters, countBySeverity, filterByTab, sortAlerts, severityRank,
  type AlertListRow,
} from './alerts/alertList'
import { QUICK_METRICS } from './metrics/quickQueries'
import { filterRules, ruleStats, sortRules } from './alert-rules/ruleList'
import {
  DEFAULT_BOOKMARKS, appendBookmark, loadBookmarks, removeBookmark, saveBookmarks, BOOKMARK_KEY,
} from './logs/bookmarks'
import {
  fleetSummary, isTargetHealthy, sortTargets, metricLight, logLight, logsCollectingCount,
  type FleetTarget,
} from './collectors/fleet'
import type { AlertRuleItem } from '@/types/api'

/** 监控中心五页改版的纯函数契约：Tab 归类 / 严重度置顶 / 书签 / 车队健康。 */

const alerts: AlertListRow[] = [
  { fingerprint: '1', name: 'InfoA', severity: 'info', status: 'firing', started_at: '2026-01-02 00:00:00' },
  { fingerprint: '2', name: 'CritB', severity: 'critical', status: 'firing', started_at: '2026-01-01 00:00:00' },
  { fingerprint: '3', name: 'WarnC', severity: 'warning', status: 'pending', started_at: '2026-01-03 00:00:00' },
  { fingerprint: '4', name: 'SilentD', severity: 'suppressed', status: 'firing', started_at: '2026-01-04 00:00:00' },
  { fingerprint: '5', name: 'DoneE', severity: 'critical', status: 'resolved', started_at: '2026-01-05 00:00:00', fromHistory: true },
]

describe('告警 Tab 归类与严重度置顶', () => {
  it('活动 / 已静默 / 已恢复 分流', () => {
    expect(filterByTab(alerts, 'active').map((a) => a.name).sort()).toEqual(['CritB', 'InfoA', 'WarnC'])
    expect(filterByTab(alerts, 'silenced').map((a) => a.name)).toEqual(['SilentD'])
    expect(filterByTab(alerts, 'resolved').map((a) => a.name)).toEqual(['DoneE'])
  })

  it('critical 永远置顶', () => {
    const sorted = sortAlerts(filterByTab(alerts, 'active'))
    expect(sorted[0].name).toBe('CritB')
    expect(severityRank('critical')).toBeGreaterThan(severityRank('warning'))
  })

  it('applyAlertFilters 支持级别与关键词', () => {
    const onlyCrit = applyAlertFilters(alerts, 'active', { severity: 'critical' })
    expect(onlyCrit.map((a) => a.name)).toEqual(['CritB'])
    const search = applyAlertFilters(alerts, 'active', { search: 'warn' })
    expect(search.map((a) => a.name)).toEqual(['WarnC'])
  })

  it('countBySeverity 不把静默算进活动总数', () => {
    const s = countBySeverity(alerts)
    expect(s.critical).toBe(1) // DoneE 已恢复不计
    expect(s.warning).toBe(1)
    expect(s.total).toBe(3)
    expect(s.silenced).toBe(1)
    expect(s.resolved).toBe(1)
  })
})

describe('指标快捷按钮', () => {
  it('含 CPU/内存/磁盘/网络/Load 且可填 PromQL', () => {
    const labels = QUICK_METRICS.map((m) => m.label)
    expect(labels).toEqual(['CPU', '内存', '磁盘', '网络', 'Load'])
    for (const m of QUICK_METRICS) expect(m.value.length).toBeGreaterThan(3)
  })
})

describe('告警规则排序 / 过滤', () => {
  const rules: AlertRuleItem[] = [
    { name: 'A', expr: 'up==0', duration: '1m', severity: 'info', enabled: true },
    { name: 'B', expr: 'cpu>90', duration: '5m', severity: 'critical', enabled: false },
    { name: 'C', expr: 'mem>90', duration: '5m', severity: 'critical', enabled: true },
    { name: 'D', expr: 'disk>85', duration: '5m', severity: 'warning', enabled: true },
  ]

  it('启用优先，组内严重度降序', () => {
    const sorted = sortRules(rules).map((r) => r.name)
    expect(sorted).toEqual(['C', 'D', 'A', 'B'])
  })

  it('filterRules 按启用/停用与关键词', () => {
    expect(filterRules(rules, { status: 'disabled' }).map((r) => r.name)).toEqual(['B'])
    expect(filterRules(rules, { status: 'enabled', severity: 'critical' }).map((r) => r.name)).toEqual(['C'])
    expect(filterRules(rules, { search: 'disk' }).map((r) => r.name)).toEqual(['D'])
  })

  it('ruleStats 统计启用数', () => {
    const s = ruleStats(rules)
    expect(s.total).toBe(4)
    expect(s.enabled).toBe(3)
    expect(s.disabled).toBe(1)
  })
})

describe('日志书签', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('内置 5 个常用示例，缺失时播种', () => {
    expect(DEFAULT_BOOKMARKS.length).toBe(5)
    const list = loadBookmarks()
    expect(list.length).toBe(5)
    expect(list.map((b) => b.name)).toContain('错误')
    expect(list.map((b) => b.name)).toContain('5xx')
    expect(list.map((b) => b.name)).toContain('慢请求')
    expect(localStorage.getItem(BOOKMARK_KEY)).toBeTruthy()
  })

  it('自定义保存去重，删除后不复活', () => {
    const seeded = loadBookmarks()
    const { list, added } = appendBookmark(seeded, '我的', 'level:ERROR AND service:api')
    expect(added).toBe(true)
    expect(list.length).toBe(6)
    const dup = appendBookmark(list, '重复', 'level:ERROR AND service:api')
    expect(dup.added).toBe(false)
    const removed = removeBookmark(list, 'level:ERROR')
    expect(removed.some((b) => b.query === 'level:ERROR')).toBe(false)
    saveBookmarks(removed)
    expect(loadBookmarks().some((b) => b.query === 'level:ERROR')).toBe(false)
  })
})

describe('采集车队健康与置顶', () => {
  const targets: FleetTarget[] = [
    { asset_id: 'a', name: 'ok-1', ip: '10.0.0.1', enabled: true, status: 'up' },
    { asset_id: 'b', name: 'down-1', ip: '10.0.0.2', enabled: true, status: 'unreachable' },
    { asset_id: 'c', name: 'off-1', ip: '10.0.0.3', enabled: false, status: 'disabled' },
  ]

  it('异常置顶', () => {
    const sorted = sortTargets(targets).map((t) => t.asset_id)
    expect(sorted[0]).toBe('b')
    expect(sorted).toEqual(['b', 'c', 'a'])
  })

  it('健康总览 = 在线/总数', () => {
    expect(isTargetHealthy(targets[0])).toBe(true)
    expect(isTargetHealthy(targets[1])).toBe(false)
    const s = fleetSummary(targets)
    expect(s.online).toBe(1)
    expect(s.total).toBe(3)
    expect(s.healthRate).toBeCloseTo(33.3, 1)
  })
})

describe('指标/日志双灯与日志在采', () => {
  const rows: FleetTarget[] = [
    {
      asset_id: 'a', name: 'ok', ip: '10.0.0.1', enabled: true, status: 'up',
      metrics_enabled: true, last_metric_at: '2026-01-01 00:00:00',
      logs_enabled: true, last_log_at: '2026-01-01 00:00:00',
    },
    {
      asset_id: 'b', name: 'metrics-only', ip: '10.0.0.2', enabled: true, status: 'up',
      metrics_enabled: true, last_metric_at: '2026-01-01 00:00:00',
      logs_enabled: false, last_log_at: '',
    },
    {
      asset_id: 'c', name: 'fail', ip: '10.0.0.3', enabled: true, status: 'unreachable',
      metrics_enabled: true, last_error: 'timeout',
    },
    {
      asset_id: 'd', name: 'off', ip: '10.0.0.4', enabled: false, status: 'disabled',
    },
  ]

  it('有心跳=绿，无=灰，失败=红', () => {
    expect(metricLight(rows[0])).toBe('green')
    expect(logLight(rows[0])).toBe('green')
    expect(metricLight(rows[1])).toBe('green')
    expect(logLight(rows[1])).toBe('gray')
    expect(metricLight(rows[2])).toBe('red')
    expect(logLight(rows[3])).toBe('gray')
  })

  it('日志在采 = logs_enabled 且有心跳', () => {
    const s = logsCollectingCount(rows)
    expect(s.collecting).toBe(1)
    expect(s.total).toBe(4)
  })
})
