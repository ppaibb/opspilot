import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AxiosHeaders } from 'axios'
import { fetchRiskTicket, riskTicketConfig } from './client'
import { useAuthStore } from '@/store/auth'

vi.mock('axios', async () => {
  const actual = await vi.importActual<typeof import('axios')>('axios')
  const post = vi.fn()
  const request = vi.fn()
  const create = vi.fn(() => ({
    post,
    request,
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  }))
  return { ...actual, default: { create } }
})

describe('api/client explicit risk ticket', () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null } as never)
  })

  it('builds a header config that is local to one request', () => {
    expect(riskTicketConfig('t-1')).toEqual({ headers: { 'X-Risk-Ticket': 't-1' } })
  })

  it('never attaches a ticket to an unrelated GET request', async () => {
    const axios = (await import('axios')).default as unknown as { create: ReturnType<typeof vi.fn> }
    const instance = axios.create.mock.results.at(-1)?.value
    const interceptor = instance.interceptors.request.use.mock.calls[0][0]
    const config = interceptor({ method: 'get', url: '/assets', headers: new AxiosHeaders() })
    expect(config.headers.get('X-Risk-Ticket')).toBeFalsy()
  })

  it('fetchRiskTicket rejects when response has no ticket', async () => {
    const axios = (await import('axios')).default as unknown as {
      create: ReturnType<typeof vi.fn>
    }
    const instance = axios.create.mock.results.at(-1)?.value
    instance.post.mockResolvedValueOnce({ data: {} })
    await expect(fetchRiskTicket({ actionKey: 'assets:delete' })).rejects.toThrow(/ticket/)
  })

  it('fetchRiskTicket resolves ticket string', async () => {
    const axios = (await import('axios')).default as unknown as {
      create: ReturnType<typeof vi.fn>
    }
    const instance = axios.create.mock.results.at(-1)?.value
    instance.post.mockResolvedValueOnce({ data: { ticket: 'tk-123' } })
    await expect(fetchRiskTicket({
      permissionKey: 'action.assets.hosts.manage',
      actionId: 'delete',
      resourceId: 'h-1',
      httpMethod: 'delete',
      routeTemplate: '/api/assets/{asset_id}',
      requestDigest: 'sha256:abc',
    })).resolves.toBe('tk-123')
    expect(instance.post).toHaveBeenLastCalledWith(
      '/auth/risk-challenge',
      {
        permission_key: 'action.assets.hosts.manage',
        action_id: 'delete',
        resource_id: 'h-1',
        http_method: 'DELETE',
        route_template: '/api/assets/{asset_id}',
        request_digest: 'sha256:abc',
      },
      { skipRiskTicketAttach: true },
    )
  })
})

describe('api/client refresh rotation', () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: 'expired-access',
      refreshToken: 'refresh-1',
      userInfo: { id: 1, username: 'alice', role: 'user' },
    })
  })

  it('refreshes once and retries the original request with the new access token', async () => {
    const axios = (await import('axios')).default as unknown as {
      create: ReturnType<typeof vi.fn>
    }
    const instance = axios.create.mock.results.at(-1)?.value
    const rejected = instance.interceptors.response.use.mock.calls[0][1]
    instance.post.mockResolvedValueOnce({
      data: { access_token: 'access-2', refresh_token: 'refresh-2' },
    })
    instance.request.mockResolvedValueOnce({ data: { ok: true } })

    const original = { url: '/assets', headers: new AxiosHeaders() }
    const result = await rejected({ response: { status: 401 }, config: original })

    expect(instance.post).toHaveBeenCalledWith('/auth/refresh', {
      refresh_token: 'refresh-1',
    })
    expect(instance.request).toHaveBeenCalledOnce()
    expect(instance.request.mock.calls[0][0].headers.get('Authorization')).toBe('Bearer access-2')
    expect(useAuthStore.getState().token).toBe('access-2')
    expect(useAuthStore.getState().refreshToken).toBe('refresh-2')
    expect(result).toEqual({ data: { ok: true } })
  })
})
