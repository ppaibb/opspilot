import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ default: { get } }))

import { dbApi, financeApi, terminalApi, wireguardApi } from './index'

describe('protected download APIs', () => {
  beforeEach(() => get.mockReset())

  it('downloads a terminal file as an authenticated blob request', async () => {
    get.mockResolvedValueOnce({ data: new Blob(), headers: {} })
    await terminalApi.downloadFile('host/1', '/var/log/a b.log')
    expect(get).toHaveBeenCalledWith(
      '/assets/terminal/files/host%2F1/download',
      { params: { path: '/var/log/a b.log' }, responseType: 'blob' },
    )
  })

  it('downloads database rollback SQL as blob', async () => {
    get.mockResolvedValueOnce({ data: new Blob(), headers: {} })
    await dbApi.downloadRollback(42)
    expect(get).toHaveBeenCalledWith('/db/tickets/42/rollback', { responseType: 'blob' })
  })

  it('downloads WireGuard server config as blob', async () => {
    get.mockResolvedValueOnce({ data: new Blob(), headers: {} })
    await wireguardApi.downloadServerConfig(7)
    expect(get).toHaveBeenCalledWith(
      '/wireguard/networks/7/server-config',
      { params: { download: 1 }, responseType: 'blob' },
    )
  })

  it('downloads filtered bills as blob instead of returning a naked URL', async () => {
    get.mockResolvedValueOnce({ data: new Blob(), headers: {} })
    await financeApi.exportBillsFile({ month: '2026-11', account_id: 3 })
    expect(get).toHaveBeenCalledWith('/finance/bills/export', {
      params: { month: '2026-11', account_id: 3 },
      responseType: 'blob',
    })
  })
})
