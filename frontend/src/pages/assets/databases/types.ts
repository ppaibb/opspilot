import type dayjs from 'dayjs'

/** 提交工单表单的值结构：execute_after_picker 是日期控件，提交前转成字符串 */
export interface TicketFormValues {
  instance_id: number
  ticket_type: string
  title?: string
  sql_text?: string
  execute_after_picker?: dayjs.Dayjs
  /** 查询授权申请字段（ticket_type=query 时使用）。 */
  grant_database?: string
  grant_tables?: string[]
  grant_hours?: number
  grant_reason?: string
  [key: string]: unknown
}

/** 审核规则行（GET/PUT /db/audit-rules 的 rules 数组元素）。 */
export interface DbAuditRuleRow {
  rule: string
  name: string
  default_level: string
  enabled: boolean
  level: string
}

/** 工单状态机展示。 */
export const TICKET_STATUS: Record<string, { color: string; label: string }> = {
  pending: { color: 'orange', label: '待审批' },
  approved: { color: 'blue', label: '已批准' },
  executing: { color: 'processing', label: '执行中' },
  success: { color: 'green', label: '执行成功' },
  failed: { color: 'red', label: '执行失败' },
  rejected: { color: 'default', label: '已驳回' },
}

/** 工单列表拉取上限（后端 limit 上限 200）：概览统计与表格共用一份数据。 */
export const TICKET_FETCH_LIMIT = 200

export const GRANT_STATUS: Record<string, { color: string; label: string }> = {
  active: { color: 'green', label: '生效中' },
  expired: { color: 'default', label: '已到期' },
  revoked: { color: 'red', label: '已吊销' },
}

/** SQL 摘要：确认弹窗里展示首条语句，避免整段 SQL 刷屏。 */
export function sqlBrief(sql: string, maxLen = 120): string {
  const flat = (sql || '').replace(/\s+/g, ' ').trim()
  return flat.length > maxLen ? `${flat.slice(0, maxLen)}…` : flat
}
