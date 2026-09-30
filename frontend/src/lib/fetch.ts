/**
 * 带认证的 fetch 包装器，自动注入 Bearer token
 */
import { useAuthStore } from '@/store/auth'

export async function authenticatedFetch(url: string, options?: RequestInit): Promise<Response> {
  const token = useAuthStore.getState().token
  const headers = new Headers(options?.headers)
  
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  
  if (!headers.has('Content-Type') && options?.body && typeof options.body === 'string') {
    headers.set('Content-Type', 'application/json')
  }
  
  return fetch(url, {
    ...options,
    headers,
  })
}
