/**
 * 日志查询书签：内置 5 个常用示例 + 用户自定义（localStorage）。
 * 单独成文件，便于契约测试。
 */

export const BOOKMARK_KEY = 'opspilot-log-query-bookmarks'

export interface LogBookmark {
  name: string
  query: string
  /** 内置示例（可删，删后不自动复活；仅在 localStorage 完全缺失时播种） */
  builtin?: boolean
}

/** 内置常用查询示例（错误 / 5xx / 慢请求 等）。 */
export const DEFAULT_BOOKMARKS: LogBookmark[] = [
  { name: '错误', query: 'level:ERROR', builtin: true },
  { name: '5xx', query: 'status:>=500', builtin: true },
  { name: '慢请求', query: 'duration:>1s', builtin: true },
  { name: 'Nginx', query: 'service:nginx', builtin: true },
  { name: '警告', query: 'level:WARN', builtin: true },
]

export function loadBookmarks(): LogBookmark[] {
  try {
    const raw = localStorage.getItem(BOOKMARK_KEY)
    if (raw == null) {
      const seeded = DEFAULT_BOOKMARKS.map((b) => ({ ...b }))
      localStorage.setItem(BOOKMARK_KEY, JSON.stringify(seeded))
      return seeded
    }
    const parsed = JSON.parse(raw) as LogBookmark[]
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return DEFAULT_BOOKMARKS.map((b) => ({ ...b }))
  }
}

export function saveBookmarks(list: LogBookmark[]) {
  localStorage.setItem(BOOKMARK_KEY, JSON.stringify(list))
}

/** 追加自定义书签（同 query 去重，最多 20 条）。返回是否新增成功。 */
export function appendBookmark(list: LogBookmark[], name: string, query: string): { list: LogBookmark[]; added: boolean } {
  const q = query.trim()
  if (!q) return { list, added: false }
  if (list.some((b) => b.query === q)) return { list, added: false }
  const next = [...list, { name: (name || q).slice(0, 40), query: q }].slice(-20)
  return { list: next, added: true }
}

export function removeBookmark(list: LogBookmark[], query: string): LogBookmark[] {
  return list.filter((b) => b.query !== query)
}
