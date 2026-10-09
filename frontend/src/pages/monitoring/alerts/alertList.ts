/**
 * 告警列表纯函数：状态 Tab 归类 + 严重度置顶排序。
 * 单独成文件，便于契约测试且满足 react-refresh/only-export-components。
 */

/** 列表行（活动 / 历史归一后的公共形状）。 */
export interface AlertListRow {
  fingerprint?: string
  id?: number | null
  name: string
  severity?: string
  instance?: string
  summary?: string
  status?: string
  started_at?: string
  resolved_at?: string
  acknowledged?: boolean
  acknowledged_by?: string
  acknowledged_at?: string
  acknowledged_note?: string
  labels?: Record<string, string>
  /** true = 来自历史归档（已恢复） */
  fromHistory?: boolean
}

export type AlertStatusTab = 'active' | 'resolved' | 'silenced'

const SEVERITY_RANK: Record<string, number> = {
  critical: 3,
  warning: 2,
  info: 1,
  suppressed: 0,
}

export function severityRank(severity?: string): number {
  return SEVERITY_RANK[(severity || '').toLowerCase()] ?? 0
}

/** 已静默：后端把静默中的活动告警 severity 标成 suppressed。 */
export function isSilencedRow(row: AlertListRow): boolean {
  return (row.severity || '').toLowerCase() === 'suppressed'
}

export function isResolvedRow(row: AlertListRow): boolean {
  const s = (row.status || '').toLowerCase()
  return row.fromHistory === true || s === 'resolved'
}

/** 异常严重度（critical/warning）永远置顶，同级再按触发时间倒序。 */
export function sortAlerts(rows: AlertListRow[]): AlertListRow[] {
  return [...rows].sort((a, b) => {
    const rankDiff = severityRank(b.severity) - severityRank(a.severity)
    if (rankDiff !== 0) return rankDiff
    return String(b.started_at || '').localeCompare(String(a.started_at || ''))
  })
}

export function filterByTab(rows: AlertListRow[], tab: AlertStatusTab): AlertListRow[] {
  if (tab === 'resolved') return rows.filter(isResolvedRow)
  if (tab === 'silenced') return rows.filter((r) => !isResolvedRow(r) && isSilencedRow(r))
  return rows.filter((r) => !isResolvedRow(r) && !isSilencedRow(r))
}

export function applyAlertFilters(
  rows: AlertListRow[],
  tab: AlertStatusTab,
  filters: { severity?: string; search?: string },
): AlertListRow[] {
  const search = (filters.search || '').trim().toLowerCase()
  const filtered = filterByTab(rows, tab).filter((alert) => {
    if (filters.severity && filters.severity !== 'all') {
      if ((alert.severity || '').toLowerCase() !== filters.severity.toLowerCase()) return false
    }
    if (search) {
      const hay = `${alert.name || ''} ${alert.instance || ''} ${alert.summary || ''}`.toLowerCase()
      if (!hay.includes(search)) return false
    }
    return true
  })
  return sortAlerts(filtered)
}

export function countBySeverity(rows: AlertListRow[]): { critical: number; warning: number; total: number; silenced: number; resolved: number } {
  const active = rows.filter((r) => !isResolvedRow(r))
  return {
    critical: active.filter((r) => (r.severity || '').toLowerCase() === 'critical').length,
    warning: active.filter((r) => (r.severity || '').toLowerCase() === 'warning').length,
    total: active.filter((r) => !isSilencedRow(r)).length,
    silenced: active.filter(isSilencedRow).length,
    resolved: rows.filter(isResolvedRow).length,
  }
}
