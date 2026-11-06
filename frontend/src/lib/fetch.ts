/** 带认证、401 会话清理和可选超时的 fetch 包装器。 */
import { useAuthStore } from '@/store/auth'

export interface AuthenticatedFetchOptions extends RequestInit {
  /** 0 表示不设总超时，适用于 SSE；普通长操作应显式给出上限。 */
  timeoutMs?: number
  skipAuthRedirect?: boolean
}

export async function authenticatedFetch(
  url: string,
  options: AuthenticatedFetchOptions = {},
): Promise<Response> {
  const { timeoutMs = 0, skipAuthRedirect = false, signal, ...requestOptions } = options
  const headers = new Headers(options.headers)
  const token = useAuthStore.getState().token
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (!headers.has('Content-Type') && options.body && typeof options.body === 'string') {
    headers.set('Content-Type', 'application/json')
  }

  const timeoutController = new AbortController()
  const combinedSignal = signal ? AbortSignal.any([signal, timeoutController.signal]) : timeoutController.signal
  const timeout = timeoutMs > 0
    ? window.setTimeout(() => timeoutController.abort(new DOMException('请求超时', 'TimeoutError')), timeoutMs)
    : undefined
  try {
    const response = await fetch(url, { ...requestOptions, headers, signal: combinedSignal })
    if (response.status === 401 && !skipAuthRedirect) {
      useAuthStore.getState().logout()
      window.location.assign('/login')
    }
    return response
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout)
  }
}
