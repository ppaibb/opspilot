import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AxiosHeaders } from 'axios'
import { fetchRiskTicket, riskTicketConfig } from './client'
import { useAuthStore } from '@/store/auth'

vi.mock('axios', async () => {
  const actual = await vi.importActual<typeof import('axios')>('axios')
  const post = vi.fn()
  const create = vi.fn(() => ({
    post,
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
