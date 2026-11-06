import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authenticatedFetch } from './fetch'
import { useAuthStore } from '@/store/auth'

describe('authenticatedFetch', () => {
  beforeEach(() => {
    useAuthStore.setState({ token: 'access', refreshToken: null, userInfo: { id: 1, username: 'u', role: 'user' } })
    vi.restoreAllMocks()
  })

  it('injects auth and clears the session on 401', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }))
    await authenticatedFetch('/api/private')
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer access')
    expect(useAuthStore.getState().token).toBeNull()
  })

  it('forwards caller abort signals', async () => {
    const controller = new AbortController()
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
    }))
    const request = authenticatedFetch('/api/stream', { signal: controller.signal })
    controller.abort(new DOMException('cancelled', 'AbortError'))
    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('aborts requests after the explicit timeout', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
    }))
    await expect(authenticatedFetch('/api/slow', { timeoutMs: 5 })).rejects.toMatchObject({ name: 'TimeoutError' })
  })
})
