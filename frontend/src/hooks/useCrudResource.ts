import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { QueryKey } from '@tanstack/react-query'
import { App } from 'antd'
import { extractAxiosDetail } from './usePermission'

/**
 * 通用 CRUD 资源 Hook。
 *
 * 封装列表页最常见的两类变更操作——保存（新增/编辑）和删除，
 * 统一处理：成功后失效查询缓存、弹出 toast、失败兜底提示。
 *
 * 使用 antd 的 `App.useApp()` 实例获取 message，避免静态调用在
 * 部分场景下 context 丢失导致提示不显示。
 *
 * @example
 * const { saveMut, deleteMut } = useCrudResource<VMResponse, VMCreate, string>({
 *   queryKey: ['vms'],
 *   entityName: '虚拟机',
 *   createFn: vmsApi.create,
 *   updateFn: vmsApi.update,
 *   deleteFn: vmsApi.delete,
 * })
 */
export interface UseCrudResourceOptions<TCreate, TUpdate, TId> {
  /** 该资源的查询 key，成功后会失效它触发列表刷新 */
  queryKey: QueryKey
  /** 实体中文名，用于 toast 文案，如「虚拟机」 */
  entityName: string
  createFn: (data: TCreate) => Promise<unknown>
  updateFn: (id: TId, data: TUpdate) => Promise<unknown>
  deleteFn: (id: TId) => Promise<unknown>
  /** 额外需要一并失效的查询 key（可选） */
  extraInvalidateKeys?: QueryKey[]
  /** 保存成功回调（通常用于关闭抽屉） */
  onSaved?: () => void
}

export function useCrudResource<TCreate, TId = string, TUpdate = TCreate>(
  options: UseCrudResourceOptions<TCreate, TUpdate, TId>,
) {
  const { queryKey, entityName, createFn, updateFn, deleteFn, extraInvalidateKeys, onSaved } = options
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey })
    extraInvalidateKeys?.forEach((k) => queryClient.invalidateQueries({ queryKey: k }))
  }

  const saveMut = useMutation({
    mutationFn: ({ id, data }: { id?: TId; data: TCreate | TUpdate }) =>
      id != null ? updateFn(id, data as TUpdate) : createFn(data as TCreate),
    onSuccess: (_res, variables) => {
      invalidate()
      onSaved?.()
      message.success(variables.id != null ? `${entityName}更新成功` : `${entityName}创建成功`)
    },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '操作失败，请重试'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: TId) => deleteFn(id),
    onSuccess: () => {
      invalidate()
      message.success(`${entityName}删除成功`)
    },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '删除失败，请重试'),
  })

  return { saveMut, deleteMut, invalidate }
}
