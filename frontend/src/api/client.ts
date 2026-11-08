import axios, { AxiosHeaders } from 'axios'
import type { AxiosError, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/store/auth'

const client = axios.create({
  baseURL: '/api',
  timeout: 30_000,
})

// ── 高危操作一次性票据 ──
// 票据必须由业务调用方显式放进目标请求，禁止使用“下一个 axios 请求”全局槽位：
// 页面轮询、SSE 换票或并发 mutation 都可能抢先消费槽位，导致票据发往错误接口。
export interface RiskRequestTarget {
  actionKey?: string
  permissionKey?: string
  actionId?: string
  resourceId?: string
  httpMethod?: string
  routeTemplate?: string
  requestDigest?: string
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
export async function fetchRiskTicket(target: RiskRequestTarget): Promise<string> {
  const config: RiskTicketSkipConfig = { skipRiskTicketAttach: true }
  const res = await client.post(
    '/auth/risk-challenge',
    {
      action_key: target.actionKey,
      permission_key: target.permissionKey,
      action_id: target.actionId,
      resource_id: target.resourceId,
      http_method: target.httpMethod?.trim().toUpperCase(),
      route_template: target.routeTemplate,
      request_digest: target.requestDigest,
    },
    config,
  )
  const ticket = (res.data as { ticket?: string } | undefined)?.ticket
  if (!ticket) throw new Error('风险票据获取失败：响应缺少 ticket')
  return ticket
}

/** 构造仅属于一个业务请求的票据配置，不修改 axios 全局状态。 */
export function riskTicketConfig(ticket: string): AxiosRequestConfig {
  return { headers: { 'X-Risk-Ticket': ticket } }
}

/** 与后端 `_risk_path_resource` 相同的稳定路径资源编码。 */
export function riskPathResource(entries: Array<[string, string | number]>): string {
  return entries.map(([name, value]) => `${name}=${String(value)}`).join('&')
}

// ── 请求拦截：自动注入 Bearer Token + 一次性风险票据 ──
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = useAuthStore.getState().token
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }

  return config
})

// ── 响应拦截：统一错误处理 ──
// 登录接口本身返回 401 是"密码错误"，不应跳转，排除掉
const AUTH_SKIP_PATHS = ['/auth/login', '/auth/refresh']

interface RetryableRequestConfig extends AxiosRequestConfig {
  _authRefreshRetried?: boolean
}

interface RefreshResponse {
  access_token: string
  refresh_token: string
}

// 并发 401 共用一次轮换请求。Refresh token 是单次消费品，多次并发轮换会被
// 后端判为重放并撤销整个 family，因此这里必须做 single-flight。
let refreshPromise: Promise<RefreshResponse> | null = null

async function refreshAccessToken(): Promise<RefreshResponse> {
  const refreshToken = useAuthStore.getState().refreshToken
  if (!refreshToken) throw new Error('缺少 refresh token')
  if (!refreshPromise) {
    refreshPromise = client.post<RefreshResponse>('/auth/refresh', {
      refresh_token: refreshToken,
    }).then((response) => {
      const tokens = response.data
      useAuthStore.getState().setTokens(tokens.access_token, tokens.refresh_token)
      return tokens
    }).finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

client.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    const url = error.config?.url ?? ''
    const isSkipped = AUTH_SKIP_PATHS.some((p) => url.includes(p))
    const status = error.response?.status

    const original = error.config as RetryableRequestConfig | undefined
    if (
      status === 401
      && !isSkipped
      && original
      && !original._authRefreshRetried
      && useAuthStore.getState().refreshToken
    ) {
      original._authRefreshRetried = true
      try {
        const tokens = await refreshAccessToken()
        const headers = AxiosHeaders.from(original.headers as AxiosHeaders | undefined)
        headers.set('Authorization', `Bearer ${tokens.access_token}`)
        original.headers = headers
        return client.request(original)
      } catch {
        useAuthStore.getState().logout()
        window.location.href = '/login'
      }
    } else if (status === 401 && !isSkipped) {
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
