/**
 * 日志标签筛选 → LogsQL 组装。
 * 与自由文本查询独立：标签片段优先 AND 拼接，避免用户手写语法。
 */

export type LabelFilters = Record<string, string[]>

function escapeLogsQLValue(value: string): string {
  // 简单值直接裸写；含空格/括号/AND 等元字符时用双引号
  if (/^[A-Za-z0-9_./:@+-]+$/.test(value)) return value
  return `"${value.replace(/"/g, '\\"')}"`
}

/** 单个标签子句。host 展开成 host OR instance 双匹配：深链常带 IP（落 instance 标签），
 * 标签下拉是主机名（落 host 标签），只匹配一边会查空。 */
function fieldClause(field: string, value: string): string {
  const v = escapeLogsQLValue(value)
  if (field === 'host') return `(host:${v} OR instance:${v})`
  return `${field}:${v}`
}

/** 把标签选择器拼进自由查询。空标签/空查询时保持 *。 */
export function composeLogsQuery(base: string, labels: LabelFilters): string {
  const parts: string[] = []
  const baseText = (base || '').trim()
  if (baseText && baseText !== '*') parts.push(`(${baseText})`)
  for (const [field, values] of Object.entries(labels)) {
    if (!values?.length) continue
    const clauses = values.map((v) => fieldClause(field, v))
    parts.push(clauses.length === 1 ? clauses[0] : `(${clauses.join(' OR ')})`)
  }
  if (!parts.length) return '*'
  return parts.join(' AND ')
}

/** 从自由查询里剥离已由标签承担的 field:value 片段（避免双重 AND 冲突）。 */
export function stripLabeledTerms(query: string, fields: string[]): string {
  let text = (query || '').trim()
  for (const field of fields) {
    const re = new RegExp(`\\s*\\b${field}:("[^"]*"|\\S+)`, 'gi')
    text = text.replace(re, ' ')
  }
  text = text
    .replace(/\s+AND\s+AND\s+/gi, ' AND ')
    .replace(/^\s*AND\s+/i, '')
    .replace(/\s+AND\s*$/i, '')
    .trim()
  return text || '*'
}
