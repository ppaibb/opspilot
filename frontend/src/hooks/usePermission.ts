import axios from 'axios'
import { App } from 'antd'
import { useAuthStore } from '@/store/auth'
import { fetchRiskTicket, type RiskRequestTarget } from '@/api/client'

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
 * 3. 取票成功后把票据参数交给业务回调；业务请求必须显式携带该票据，
 *    因此轮询请求、SSE 换票和并发 mutation 不可能“抢走”票据；
 * 4. 取票失败不执行回调，并提示后端返回的 detail。
 *
 * 一次确认要发多个请求的场景，每个请求必须重新 fetchRiskTicket；票据不可复用。
 */
export function useRiskConfirm() {
  const { modal, message } = App.useApp()
  const riskLevels = useAuthStore((s) => s.riskLevels)

  const confirm = async (
    target: string | RiskRequestTarget,
    onOk: (ticket: string) => void | Promise<void>,
    description?: string,
  ): Promise<void> => {
    const normalizedTarget = typeof target === 'string' ? { actionKey: target } : target
    const actionKey = normalizedTarget.actionKey ?? `${normalizedTarget.permissionKey}:${normalizedTarget.actionId}`
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
      const ticket = await fetchRiskTicket(normalizedTarget)
      await onOk(ticket)
    } catch (err) {
      message.error(`风险确认失败：${extractErrorDetail(err)}`)
    }
  }

  return { confirm }
}
