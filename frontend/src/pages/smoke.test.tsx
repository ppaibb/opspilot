import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import type { ReactNode } from 'react'
import { useCrudResource } from '@/hooks/useCrudResource'

/** 主机 CRUD / DB 工单的「变更链路」冒烟：只钉死 create/update/delete 的调用契约，
 *  页面级渲染回归交给手点/后续 e2e。 */

function wrapperFactory() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <App>{children}</App>
    </QueryClientProvider>
  )
  return Wrapper
}

describe('主机 CRUD 变更链路', () => {
  const createFn = vi.fn()
  const updateFn = vi.fn()
  const deleteFn = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    createFn.mockResolvedValue({ id: 'i-1' })
    updateFn.mockResolvedValue({})
    deleteFn.mockResolvedValue({})
  })

  it('创建主机：createFn 收到 payload 且不带 id', async () => {
    const Wrapper = wrapperFactory()
    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string; ip: string }, string>({
          queryKey: ['assets'],
          entityName: '主机',
          createFn,
          updateFn,
          deleteFn,
        }),
      { wrapper: Wrapper },
    )
    act(() => {
      result.current.saveMut.mutate({ data: { name: 'web-1', ip: '10.0.0.1' } })
    })
    await waitFor(() => expect(result.current.saveMut.isSuccess).toBe(true))
    expect(createFn).toHaveBeenCalledWith({ name: 'web-1', ip: '10.0.0.1' })
  })

  it('编辑主机：updateFn 带 id', async () => {
    const Wrapper = wrapperFactory()
    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string }, string>({
          queryKey: ['assets'],
          entityName: '主机',
          createFn,
          updateFn,
          deleteFn,
        }),
      { wrapper: Wrapper },
    )
    act(() => {
      result.current.saveMut.mutate({ id: 'i-1', data: { name: 'web-2' } })
    })
    await waitFor(() => expect(result.current.saveMut.isSuccess).toBe(true))
    expect(updateFn).toHaveBeenCalledWith('i-1', { name: 'web-2' })
  })

  it('删除主机：deleteFn 带业务 id', async () => {
    const Wrapper = wrapperFactory()
    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string }, string>({
          queryKey: ['assets'],
          entityName: '主机',
          createFn,
          updateFn,
          deleteFn,
        }),
      { wrapper: Wrapper },
    )
    act(() => {
      result.current.deleteMut.mutate('i-1')
    })
    await waitFor(() => expect(result.current.deleteMut.isSuccess).toBe(true))
    expect(deleteFn).toHaveBeenCalledWith('i-1')
  })
})

describe('DB 工单变更链路', () => {
  it('提交工单：payload 含 instance/database/sql 三要素', async () => {
    const submitFn = vi.fn().mockResolvedValue({ ticket_id: 'T-1', status: 'pending' })
    const Wrapper = wrapperFactory()
    const { result } = renderHook(
      () =>
        useCrudResource<{
          instance_id: number
          database: string
          sql: string
          title: string
        }, number>({
          queryKey: ['db-tickets'],
          entityName: '工单',
          createFn: submitFn,
          updateFn: vi.fn(),
          deleteFn: vi.fn(),
        }),
      { wrapper: Wrapper },
    )
    const payload = {
      instance_id: 1,
      database: 'orders',
      sql: 'UPDATE t SET a=1 WHERE id=1',
      title: '修数',
    }
    act(() => {
      result.current.saveMut.mutate({ data: payload })
    })
    await waitFor(() => expect(result.current.saveMut.isSuccess).toBe(true))
    expect(submitFn).toHaveBeenCalledWith(payload)
    expect(submitFn.mock.calls[0][0].sql).toContain('UPDATE')
  })

  it('工单提交失败时不调用 updateFn（不会误走编辑分支）', async () => {
    const submitFn = vi.fn().mockRejectedValue(new Error('R8 拒绝'))
    const updateFn = vi.fn()
    const Wrapper = wrapperFactory()
    const { result } = renderHook(
      () =>
        useCrudResource<{ sql: string }, number>({
          queryKey: ['db-tickets'],
          entityName: '工单',
          createFn: submitFn,
          updateFn,
          deleteFn: vi.fn(),
        }),
      { wrapper: Wrapper },
    )
    act(() => {
      result.current.saveMut.mutate({ data: { sql: 'DELETE FROM t' } })
    })
    await waitFor(() => expect(result.current.saveMut.isPending).toBe(false))
    expect(updateFn).not.toHaveBeenCalled()
  })
})
