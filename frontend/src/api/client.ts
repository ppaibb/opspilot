import axios from 'axios'
import type { AxiosError, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/store/auth'

const client = axios.create({
  baseURL: '/api',
  timeout: 30_000,
})

// ── 高危操作一次性票据 ──
//
// 时序背景（复审确认的 P0）：useRiskConfirm 的确认回调里同步调用 mutate()，而
// axios 请求拦截器运行在微任务中——旧实现「写槽位 → 同步执行回调 → finally 清空」
// 全在同一同步栈内完成，拦截器运行时槽位必然已被清成 null，高危请求永远缺票 403。
//
// 现在的槽位语义：
// - 取票方（useRiskConfirm / fetchRiskTicket）只负责写入，不负责清空；
// - 请求拦截器发现槽位非空、未超 TTL、且该请求未显式携带 X-Risk-Ticket 头时，
//   把票据附加到该请求并「用后即清」——一张票只随一个请求发出（后端一次性消费）；
// - 超过 TTL 仍未被消费的残留票据直接丢弃（后端票据 TTL 120 秒，这里取更短的
//   15 秒，只覆盖「确认后立刻发请求」的正常窗口），避免陈票随无关请求外发，
//   造成后续真正的业务请求匹配失败。
const RISK_TICKET_TTL_MS = 15_000

interface PendingRiskTicket {
  ticket: string
  /** 写入时刻（毫秒），用于拦截器判断是否过期 */
  createdAt: number
}

let pendingRiskTicket: PendingRiskTicket | null = null

/** 写入待发送的风险票据（传 null 表示主动清空/作废槽位） */
export function setPendingRiskTicket(ticket: string | null) {
  pendingRiskTicket = ticket ? { ticket, createdAt: Date.now() } : null
}

/** 内部标记：挂在「取票请求」配置上，让拦截器跳过槽位附加（防止偷走别人的票） */
interface RiskTicketSkipConfig extends AxiosRequestConfig {
  skipRiskTicketAttach?: boolean
}

/**
 * 手动取一张一次性风险票据，供「一次确认要发多个请求」的场景使用：
 * 每个请求各取各的票，通过请求配置显式携带：
 *   apiFn(args, { headers: { 'X-Risk-Ticket': ticket } })
 * 注意：票据一次性消费，显式携带后槽位不参与，也不可复用同一张票。
 */
export async function fetchRiskTicket(actionKey: string, resourceId?: string): Promise<string> {
  const config: RiskTicketSkipConfig = { skipRiskTicketAttach: true }
  const res = await client.post(
    '/auth/risk-challenge',
    { action_key: actionKey, resource_id: resourceId },
    config,
  )
  const ticket = (res.data as { ticket?: string } | undefined)?.ticket
  if (!ticket) throw new Error('风险票据获取失败：响应缺少 ticket')
  return ticket
}

// ── 请求拦截：自动注入 Bearer Token + 一次性风险票据 ──
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = useAuthStore.getState().token
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }

  // 请求已显式带票据（fetchRiskTicket 手动通道）：槽位不参与、不消费
  const explicitTicket =
    typeof config.headers.get === 'function'
      ? config.headers.get('X-Risk-Ticket')
      : config.headers['X-Risk-Ticket']
  if (explicitTicket) return config

  // 取票请求本身：跳过槽位附加，避免把待发送的票据偷走
  if ((config as RiskTicketSkipConfig).skipRiskTicketAttach) return config

  if (pendingRiskTicket) {
    // 无论是否过期都只读一次：过期残留票据就地丢弃（见上方 TTL 说明），不再顺延
    if (Date.now() - pendingRiskTicket.createdAt <= RISK_TICKET_TTL_MS) {
      config.headers['X-Risk-Ticket'] = pendingRiskTicket.ticket
    }
    pendingRiskTicket = null
  }
  return config
})

// ── 响应拦截：统一错误处理 ──
// 登录接口本身返回 401 是"密码错误"，不应跳转，排除掉
const AUTH_SKIP_PATHS = ['/auth/login']

client.interceptors.response.use(
  (res) => res,
  (error: AxiosError) => {
    const url = error.config?.url ?? ''
    const isSkipped = AUTH_SKIP_PATHS.some((p) => url.includes(p))
    const status = error.response?.status

    if (status === 401 && !isSkipped) {
      useAuthStore.getState().logout()
      window.location.href = '/login'
    }

    // 网络超时和网络不可达时，给错误加上可识别标记，方便全局拦截器区分
    if (error.code === 'ECONNABORTED' || !error.response) {
      ;(error as AxiosError & { isNetworkError?: boolean }).isNetworkError = true
    }

    return Promise.reject(error)
  },
)

export default client
