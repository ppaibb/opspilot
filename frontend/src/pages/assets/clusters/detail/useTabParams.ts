import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/** 跨 Tab 跳转定位参数：kind|namespace|name。 */
export const focusValue = (kind: string, namespace: string, name: string) => `${kind}|${namespace}|${name}`

/**
 * 把 Tab 内的筛选状态托管到 URL query —— 刷新、分享链接、前进后退都不丢。
 * 写回走 replace：敲关键字不该往浏览器历史里堆记录。
 */
export function useUrlFilter(key: string, fallback = '') {
  const [searchParams, setSearchParams] = useSearchParams()
  const value = searchParams.get(key) || fallback
  const setValue = useCallback((next: string) => {
    // 从 window.location.search 读最新串：同一事件里连续改多个筛选项时
    // （如切换 Kind 同时重置 Namespace），闭包里的 searchParams 还是旧快照，
    // 后一次写会把前一次覆盖掉。
    const params = new URLSearchParams(window.location.search)
    if (!next || next === fallback) params.delete(key)
    else params.set(key, next)
    setSearchParams(params, { replace: true })
  }, [fallback, key, setSearchParams])
  return [value, setValue] as const
}

/** 读 URL 上的定位目标（搜索跳转 / 概览下钻写入），空串表示无定位。 */
export function useUrlFocus(): string {
  const [searchParams] = useSearchParams()
  return searchParams.get('focus') || ''
}

/** 命中定位目标的行加高亮 class（配合 index.css 的 .cluster-row-focused）。 */
export const focusRowClass = (
  focus: string, kind: string, namespace: string, name: string,
): string => (focus && focus === focusValue(kind, namespace, name) ? 'cluster-row-focused' : '')
