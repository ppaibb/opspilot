import axios from 'axios'
import { App } from 'antd'
import { useAuthStore } from '@/store/auth'
import { authApi } from '@/api'
import { setPendingRiskTicket, fetchRiskTicket } from '@/api/client'

// 多请求场景（一次确认要发多个请求）需要手动逐请求取票，从 client 转出口径
export { fetchRiskTicket }

/**
 * 检查当前用户是否拥有指定权限
 */
export function usePermission() {
  const permissions = useAuthStore((s) => s.permissions)
  return {
    has: (key: string) => !!permissions?.[key],
    hasAll: (...keys: string[]) => keys.every((k) => permissions?.[k]),
    hasAny: (...keys: string[]) => keys.some((k) => permissions?.[k]),
  }
}

/**
 * 从 AxiosError 中提取后端 detail 文案；非 Axios 错误或无 detail 时返回 undefined
 * （供各页面 catch 分支复用）。
 *
 * 只取字符串形态：后端在部分 422/403 场景会返回对象型 detail
 * （如 {message, findings}），那类结构由调用方自行解构处理。
 */
export function extractAxiosDetail(err: unknown): string | undefined {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as { detail?: unknown } | undefined
    if (typeof data?.detail === 'string' && data.detail) return data.detail
  }
  return undefined
}

/** 从错误中提取可读文案：优先后端 detail，取不到时给出兜底提示 */
function extractErrorDetail(err: unknown): string {
  return extractAxiosDetail(err) ?? '未知错误，请稍后重试'
}

/**
 * 高危/中危操作二次确认 + 一次性风险票据链路：
 * 1. 从 auth store 的 riskLevels 读取动作风险等级，high 等级先弹确认框，
 *    用户明确同意后才继续；其余等级跳过确认直接取票；
 * 2. 统一先 POST /auth/risk-challenge 取一次性票据（后端校验动作与等级）；
 * 3. 取票成功 → 把票据写入 client 的待发送槽位 → 执行业务回调；
 *    回调触发的第一个请求由拦截器自动附加 X-Risk-Ticket 并用后即清。
 *    槽位由拦截器消费，这里不再清空——同步清空会让微任务里运行的
 *    请求拦截器永远读到 null（复审确认的 403 根因）；
 * 4. 取票失败不执行回调，并提示后端返回的 detail。
 *
 * 一次确认要发多个请求的场景：不要依赖槽位，改用 fetchRiskTicket
 * 逐请求取票并显式携带 X-Risk-Ticket 头。
 *
 * 调用签名保持同步兼容：confirm(key, () => {...}) 照常可用；
 * 新代码可 await confirm(...) 感知完成时机。
 */
export function useRiskConfirm() {
  const { modal, message } = App.useApp()
  const riskLevels = useAuthStore((s) => s.riskLevels)

  const confirm = async (actionKey: string, onOk: () => void, description?: string): Promise<void> => {
    const level = riskLevels?.[actionKey]

    // 高危等级：必须先在确认框中明确同意
    if (level === 'high') {
      const confirmed = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: '高危操作确认',
          content: description ?? `该操作风险等级为「高危」（${actionKey}），确认继续执行？`,
          okText: '确认执行',
          okButtonProps: { danger: true },
          cancelText: '取消',
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        })
      })
      if (!confirmed) return
    }

    // 统一取一次性票据；失败则中断，不执行业务操作
    try {
      const challenge = await authApi.riskChallenge({ action_key: actionKey })
      setPendingRiskTicket(challenge.ticket)
      // 票据由请求拦截器「用后即清」，此处刻意不在 finally 里清空：
      // onOk() 内的 mutate() 只是发起请求，拦截器在微任务里才真正读取槽位
      onOk()
    } catch (err) {
      message.error(`风险确认失败：${extractErrorDetail(err)}`)
    }
  }

  return { confirm }
}
