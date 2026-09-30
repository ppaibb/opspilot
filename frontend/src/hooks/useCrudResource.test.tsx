import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import type { ReactNode } from 'react'
import { useCrudResource } from './useCrudResource'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <App>{children}</App>
    </QueryClientProvider>
  )
  return { Wrapper, queryClient }
}

describe('useCrudResource', () => {
  it('saveMut create path calls createFn and invalidates', async () => {
    const createFn = vi.fn().mockResolvedValue({ id: '1' })
    const updateFn = vi.fn()
    const deleteFn = vi.fn()
    const onSaved = vi.fn()
    const { Wrapper, queryClient } = createWrapper()
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string }, string>({
          queryKey: ['demo'],
          entityName: '示例',
          createFn,
          updateFn,
          deleteFn,
          onSaved,
        }),
      { wrapper: Wrapper },
    )

    act(() => {
      result.current.saveMut.mutate({ data: { name: 'a' } })
    })
    await waitFor(() => expect(result.current.saveMut.isSuccess).toBe(true))
    expect(createFn).toHaveBeenCalledWith({ name: 'a' })
    expect(updateFn).not.toHaveBeenCalled()
    expect(onSaved).toHaveBeenCalled()
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['demo'] })
  })

  it('saveMut update path calls updateFn with id', async () => {
    const createFn = vi.fn()
    const updateFn = vi.fn().mockResolvedValue({})
    const deleteFn = vi.fn()
    const { Wrapper } = createWrapper()

    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string }, string>({
          queryKey: ['demo'],
          entityName: '示例',
          createFn,
          updateFn,
          deleteFn,
        }),
      { wrapper: Wrapper },
    )

    act(() => {
      result.current.saveMut.mutate({ id: '9', data: { name: 'b' } })
    })
    await waitFor(() => expect(result.current.saveMut.isSuccess).toBe(true))
    expect(updateFn).toHaveBeenCalledWith('9', { name: 'b' })
    expect(createFn).not.toHaveBeenCalled()
  })

  it('deleteMut calls deleteFn and surfaces error toast on failure', async () => {
    const createFn = vi.fn()
    const updateFn = vi.fn()
    const deleteFn = vi.fn().mockRejectedValue(new Error('nope'))
    const { Wrapper } = createWrapper()

    const { result } = renderHook(
      () =>
        useCrudResource<{ name: string }, string>({
          queryKey: ['demo'],
          entityName: '示例',
          createFn,
          updateFn,
          deleteFn,
        }),
      { wrapper: Wrapper },
    )

    act(() => {
      result.current.deleteMut.mutate('3')
    })
    await waitFor(() => expect(deleteFn).toHaveBeenCalledWith('3'))
    await waitFor(() => {
      expect(document.body.textContent).toContain('删除失败，请重试')
    })
  })
})
