/** 部署历史状态与展示的纯函数（与 certIssueLog.ts / certTableMeta.ts 同模式） */
import type { CertificateDeployment, CertificateDeploymentResult } from '@/types/api'

/** 部署历史列表在存在进行中记录时的轮询间隔（毫秒） */
export const CERT_DEPLOY_POLL_INTERVAL_MS = 3000

/** 部署状态 → 展示文案：running 进行中、success 成功、partial 部分成功、failed 失败 */
export function deployStatusText(status?: string | null): string {
  switch ((status ?? '').trim().toLowerCase()) {
    case 'running':
      return '进行中'
    case 'success':
      return '成功'
    case 'partial':
      return '部分成功'
    case 'failed':
      return '失败'
    default:
      return status?.trim() ? status.trim() : '—'
  }
}

/** 部署状态 → antd Tag 颜色（沿用证书状态徽章的配色） */
export function deployStatusTagColor(status?: string | null): string {
  switch ((status ?? '').trim().toLowerCase()) {
    case 'running':
      return 'processing'
    case 'success':
      return 'success'
    case 'partial':
      return 'warning'
    case 'failed':
      return 'error'
    default:
      return 'default'
  }
}

/** 目标类型 → 展示文案：host 主机、cloud 云服务、k8s K8s */
export function deployTargetTypeText(targetType?: string | null): string {
  switch ((targetType ?? '').trim().toLowerCase()) {
    case 'host':
      return '主机'
    case 'cloud':
      return '云服务'
    case 'k8s':
      return 'K8s'
    default:
      return targetType?.trim() ? targetType.trim() : '—'
  }
}

/** 逐目标结果 → 成功台数：{ success, total } */
export function deploySuccessCount(
  results?: CertificateDeploymentResult[] | null,
): { success: number; total: number } {
  const list = results ?? []
  return { success: list.filter((r) => r.success).length, total: list.length }
}

/** 列表里是否有进行中的部署记录（历史列表与日志抽屉轮询用） */
export function hasRunningDeployment(
  deployments?: Array<CertificateDeployment | null | undefined> | null,
): boolean {
  return (deployments ?? []).some((d) => (d?.status ?? '').trim().toLowerCase() === 'running')
}
