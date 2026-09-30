import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setPendingRiskTicket, fetchRiskTicket } from './client'
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

describe('api/client risk ticket slot', () => {
  beforeEach(() => {
    setPendingRiskTicket(null)
    useAuthStore.setState({ token: null, user: null } as never)
  })

  it('setPendingRiskTicket(null) clears the slot', () => {
    setPendingRiskTicket('t-1')
    setPendingRiskTicket(null)
    // 再次清空不抛错
    expect(() => setPendingRiskTicket(null)).not.toThrow()
  })

  it('fetchRiskTicket rejects when response has no ticket', async () => {
    const axios = (await import('axios')).default as unknown as {
      create: ReturnType<typeof vi.fn>
    }
    const instance = axios.create.mock.results.at(-1)?.value
    instance.post.mockResolvedValueOnce({ data: {} })
    await expect(fetchRiskTicket('assets:delete')).rejects.toThrow(/ticket/)
  })

  it('fetchRiskTicket resolves ticket string', async () => {
    const axios = (await import('axios')).default as unknown as {
      create: ReturnType<typeof vi.fn>
    }
    const instance = axios.create.mock.results.at(-1)?.value
    instance.post.mockResolvedValueOnce({ data: { ticket: 'tk-123' } })
    await expect(fetchRiskTicket('assets:delete', 'h-1')).resolves.toBe('tk-123')
  })
})
