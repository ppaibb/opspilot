import { create } from 'zustand'
import { notificationsApi } from '@/api'
import type { NotificationApiItem } from '@/types/api'

export type NotificationType = 'success' | 'error' | 'warning' | 'info'

export interface AppNotification {
  id: string
  type: NotificationType
  category?: string
  title: string
  content?: string
  link?: string
  resourceId?: string
  isRead: boolean
  createdAt: string
}

/** 后端结构 → 前端结构 */
function fromApi(n: NotificationApiItem): AppNotification {
  return {
    id: n.id,
    type: n.type,
    category: n.category,
    title: n.title,
    content: n.content,
    link: n.link ?? undefined,
    resourceId: n.resource_id ?? undefined,
    isRead: n.is_read,
    createdAt: n.created_at,
  }
}

interface NotificationState {
  items: AppNotification[]
  unreadCount: number
  loading: boolean
  /** SSE 是否已连接（连上后停止轮询） */
  streamConnected: boolean

  /** 拉取通知列表（点开铃铛时调用） */
  fetchList: () => Promise<void>
  /** 仅刷新未读数（轮询用，省流量） */
  refreshUnread: () => Promise<void>
  /** 标记单条已读（乐观更新 + 调后端） */
  markRead: (id: string) => Promise<void>
  /** 全部已读 */
  markAllRead: () => Promise<void>
  /** 删除单条 */
  remove: (id: string) => Promise<void>
  /** 会话级即时通知（操作后本地立即反馈；后端也会落库，下次 fetch 覆盖） */
  pushLocal: (n: Omit<AppNotification, 'id' | 'isRead' | 'createdAt'>) => void
  /** 接收后端 SSE 推送的一条新通知，实时插入列表 */
  receivePush: (n: NotificationApiItem) => void
  /** 建立 SSE 连接（内部先取短时效票据），返回断开函数 */
  connectStream: () => () => void
}

/** SSE 连续失败多少次后放弃重连（只留轮询兜底） */
const MAX_STREAM_FAILURES = 3
/** SSE 失败后的重连退避间隔（毫秒），重连前需重新取票 */
const STREAM_RETRY_DELAY_MS = 30_000

function countUnread(items: AppNotification[]): number {
  return items.filter((i) => !i.isRead).length
}

export const useNotificationStore = create<NotificationState>((set, get) => ({
  items: [],
  unreadCount: 0,
  loading: false,
  streamConnected: false,

  fetchList: async () => {
    set({ loading: true })
    try {
      const res = await notificationsApi.list({ limit: 20 })
      const serverItems = res.items.map(fromApi)
      // 保留本地即时通知（local- 前缀，后端尚未落库的），与后端结果合并，避免被覆盖
      set((state) => {
        const localOnly = state.items.filter((i) => i.id.startsWith('local-'))
        const merged = [...localOnly, ...serverItems]
        return {
          items: merged,
          unreadCount: res.unread_count + localOnly.filter((i) => !i.isRead).length,
          loading: false,
        }
      })
    } catch {
      set({ loading: false })
    }
  },

  refreshUnread: async () => {
    try {
      const res = await notificationsApi.unreadCount()
      set({ unreadCount: res.unread_count })
    } catch {
      /* 静默失败，下次轮询重试 */
    }
  },

  markRead: async (id) => {
    set((state) => {
      const items = state.items.map((i) => (i.id === id ? { ...i, isRead: true } : i))
      return { items, unreadCount: countUnread(items) }
    })
    // local- 前缀是会话级即时通知，后端还没落库，跳过请求（否则 404）
    if (id.startsWith('local-')) return
    try {
      await notificationsApi.markRead(id)
    } catch {
      get().refreshUnread()
    }
  },

  markAllRead: async () => {
    set((state) => ({ items: state.items.map((i) => ({ ...i, isRead: true })), unreadCount: 0 }))
    // 没有后端落库记录时（全是 local- 即时通知）不必请求
    if (!get().items.some((i) => !i.id.startsWith('local-'))) return
    try {
      await notificationsApi.markAllRead()
    } catch {
      get().refreshUnread()
    }
  },

  remove: async (id) => {
    set((state) => {
      const items = state.items.filter((i) => i.id !== id)
      return { items, unreadCount: countUnread(items) }
    })
    try {
      await notificationsApi.remove(id)
    } catch {
      get().fetchList()
    }
  },

  pushLocal: (n) =>
    set((state) => {
      const item: AppNotification = {
        ...n,
        id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        isRead: false,
        createdAt: new Date().toISOString(),
      }
      const items = [item, ...state.items]
      return { items, unreadCount: countUnread(items) }
    }),

  receivePush: (n) =>
    set((state) => {
      const incoming = fromApi(n)
      // 去重：后端推送的记录可能与本地即时通知重复（同一 resourceId 的 local- 占位）
      const deduped = state.items.filter(
        (i) => i.id !== incoming.id && !(i.id.startsWith('local-') && i.resourceId && i.resourceId === incoming.resourceId),
      )
      const items = [incoming, ...deduped]
      return { items, unreadCount: countUnread(items) }
    }),

  connectStream: () => {
    let es: EventSource | null = null
    let closed = false
    let consecutiveFailures = 0
    let retryTimer: ReturnType<typeof setTimeout> | null = null

    // 连接失败/中断的统一处理：关闭连接、标记未连接（轮询兜底），
    // 退避 30 秒后重新取票重连；连续失败达上限后停止重连。
    const handleFailure = () => {
      if (closed) return
      set({ streamConnected: false })
      es?.close()
      es = null
      consecutiveFailures += 1
      if (consecutiveFailures >= MAX_STREAM_FAILURES) return
      retryTimer = setTimeout(openStream, STREAM_RETRY_DELAY_MS)
    }

    const openStream = async () => {
      if (closed) return
      // 原生 EventSource 不支持自定义 header：先取短时效票据，再按 query param 建连
      let ticket: string
      try {
        const res = await notificationsApi.sseTicket()
        ticket = res.ticket
      } catch {
        handleFailure()
        return
      }
      if (closed) return

      try {
        es = new EventSource(`/api/notifications/stream?ticket=${encodeURIComponent(ticket)}`)
      } catch {
        handleFailure()
        return
      }

      es.onopen = () => {
        if (closed) return
        // 连接成功后清零失败计数，恢复完整的重连预算
        consecutiveFailures = 0
        set({ streamConnected: true })
      }

      es.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data) as NotificationApiItem
          get().receivePush(data)
        } catch {
          /* 忽略心跳等非 JSON 消息 */
        }
      }

      es.onerror = () => handleFailure()
    }

    openStream()

    return () => {
      closed = true
      if (retryTimer) clearTimeout(retryTimer)
      set({ streamConnected: false })
      es?.close()
    }
  },
}))
