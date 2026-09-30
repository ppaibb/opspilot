/**
 * 告警规则列表纯函数：启用/停用 + 严重度分组排序。
 * 单独成文件，便于契约测试。
 */
import type { AlertRuleItem } from '@/types/api'

const SEVERITY_RANK: Record<string, number> = { critical: 3, warning: 2, info: 1 }

export type RuleStatusFilter = 'all' | 'enabled' | 'disabled'

/** 启用优先，同组内严重度降序（critical 置顶），再按名称。 */
export function sortRules(rules: AlertRuleItem[]): AlertRuleItem[] {
  return [...rules].sort((a, b) => {
    const en = Number(!!b.enabled) - Number(!!a.enabled)
    if (en !== 0) return en
    const sev = (SEVERITY_RANK[(b.severity || '').toLowerCase()] || 0)
      - (SEVERITY_RANK[(a.severity || '').toLowerCase()] || 0)
    if (sev !== 0) return sev
    return String(a.name || '').localeCompare(String(b.name || ''))
  })
}

export function filterRules(
  rules: AlertRuleItem[],
  filters: { status?: RuleStatusFilter; severity?: string; search?: string },
): AlertRuleItem[] {
  const search = (filters.search || '').trim().toLowerCase()
  const list = rules.filter((r) => {
    if (filters.status === 'enabled' && !r.enabled) return false
    if (filters.status === 'disabled' && r.enabled) return false
    if (filters.severity && filters.severity !== 'all') {
      if ((r.severity || '').toLowerCase() !== filters.severity.toLowerCase()) return false
    }
    if (search) {
      const hay = `${r.name || ''} ${r.expr || ''} ${r.group || ''}`.toLowerCase()
      if (!hay.includes(search)) return false
    }
    return true
  })
  return sortRules(list)
}

export function ruleStats(rules: AlertRuleItem[]): { total: number; enabled: number; disabled: number; critical: number } {
  return {
    total: rules.length,
    enabled: rules.filter((r) => r.enabled).length,
    disabled: rules.filter((r) => !r.enabled).length,
    critical: rules.filter((r) => (r.severity || '').toLowerCase() === 'critical' && r.enabled).length,
  }
}
