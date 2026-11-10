/** 证书列表 redesign 的可抽取纯函数（与 certAccountFilter.ts / certIssueLog.ts 同模式） */

/** 密钥类型 → 展示文案：ec-256 → EC(256)，未知取值原样回落，无值显示 — */
export function formatKeyType(keyType?: string | null): string {
  const v = (keyType ?? '').trim().toLowerCase()
  if (v === 'ec-256') return 'EC(256)'
  if (v === 'ec-384') return 'EC(384)'
  if (v === 'rsa-2048') return 'RSA(2048)'
  if (v === 'rsa-4096') return 'RSA(4096)'
  return v ? (keyType ?? '').trim() : '—'
}

/** ACME 服务商 → 颁发机构展示文案 */
export function formatIssuer(acmeServer?: string | null): string {
  const v = (acmeServer ?? '').trim().toLowerCase()
  if (v === 'letsencrypt') return "Let's Encrypt"
  if (v === 'letsencrypt_test') return "Let's Encrypt（测试）"
  if (v === 'custom') return '手动上传'
  return v ? (acmeServer ?? '').trim() : '—'
}

export type ExpiryTone = 'green' | 'orange' | 'red' | 'muted'

/** 剩余天数 → 到期时间展示：>30 天绿、≤30 天橙、已过期红、无值灰 */
export function expiryDisplay(days: number | null): { text: string; tone: ExpiryTone } {
  if (days === null) return { text: '—', tone: 'muted' }
  if (days < 0) return { text: '已过期', tone: 'red' }
  if (days <= 30) return { text: `剩余 ${days} 天`, tone: 'orange' }
  return { text: `剩余 ${days} 天`, tone: 'green' }
}

export const EXPIRY_TONE_COLOR: Record<ExpiryTone, string> = {
  green: '#52c41a',
  orange: '#faad14',
  red: '#ff4d4f',
  muted: '#8c8c8c',
}

/** 实心状态徽章底色（与 STATUS_MAP 的文案配对使用） */
export const CERT_STATUS_BADGE_BG: Record<string, string> = {
  valid: '#52c41a',
  issued: '#52c41a',
  issuing: '#1677ff',
  renewing: '#1677ff',
  pending: '#8c8c8c',
  expiring: '#faad14',
  expired: '#ff4d4f',
  failed: '#ff4d4f',
  revoked: '#8c8c8c',
}

/** 需要带呼吸点的进行中状态 */
export const CERT_STATUS_BADGE_PULSE: readonly string[] = ['issuing', 'renewing']

/** 状态 → 实心徽章底色，未知状态回落灰色 */
export function certStatusBadgeBg(status?: string | null): string {
  if (!status) return '#8c8c8c'
  return CERT_STATUS_BADGE_BG[status] ?? '#8c8c8c'
}

export type CertProviderFilter = 'all' | 'aliyun' | 'tencent' | 'other'

export const CERT_PROVIDER_FILTER_OPTIONS: Array<{ value: CertProviderFilter; label: string }> = [
  { value: 'all', label: '全部平台' },
  { value: 'aliyun', label: '阿里云' },
  { value: 'tencent', label: '腾讯云' },
  { value: 'other', label: '其他' },
]

/** 平台筛选：按 provider 前端过滤；other 兜底非阿里云/腾讯云 */
export function matchProviderFilter(provider: string | undefined | null, filter: CertProviderFilter): boolean {
  if (filter === 'all') return true
  const p = (provider ?? '').trim().toLowerCase()
  if (filter === 'aliyun') return p === 'aliyun'
  if (filter === 'tencent') return p === 'tencent'
  return p !== 'aliyun' && p !== 'tencent'
}

export const CERT_STATUS_FILTER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'all', label: '全部状态' },
  { value: 'issued', label: '已签发' },
  { value: 'valid', label: '有效' },
  { value: 'issuing', label: '签发中' },
  { value: 'renewing', label: '续期中' },
  { value: 'pending', label: '申请中' },
  { value: 'expiring', label: '即将到期' },
  { value: 'expired', label: '已过期' },
  { value: 'failed', label: '失败' },
  { value: 'revoked', label: '已吊销' },
]

/** 状态筛选：all 不过滤，其余按 status 精确匹配 */
export function matchStatusFilter(status: string | undefined | null, filter: string): boolean {
  if (filter === 'all') return true
  return (status ?? '') === filter
}

/** 续签按钮禁用判定（与后端续签入口的 revoked 校验一致）：已吊销不可续签，
 * 进行中（pending/issuing/renewing）不可重复续签，手动上传证书无 ACME 账号
 * 不可续签。只禁用续签，不动其他按钮。 */
export function isRenewDisabled(
  status: string | undefined | null,
  provider: string | undefined | null,
  acmeServer: string | undefined | null,
): boolean {
  const s = (status ?? '').trim().toLowerCase()
  if (s === 'revoked') return true
  if (['pending', 'issuing', 'renewing'].includes(s)) return true
  if ((provider ?? '').trim().toLowerCase() === 'custom') return true
  if ((acmeServer ?? '').trim().toLowerCase() === 'custom') return true
  return false
}

/** 续签按钮禁用原因（Tooltip 文案）：与 isRenewDisabled 同口径 */
export function renewDisabledReason(
  status: string | undefined | null,
  provider: string | undefined | null,
  acmeServer: string | undefined | null,
): string {
  const s = (status ?? '').trim().toLowerCase()
  if (s === 'revoked') return '已吊销证书不可续签，如需重新签发请先删除该记录'
  if ((provider ?? '').trim().toLowerCase() === 'custom') return '手动上传的证书没有 ACME 账号，无法自动续期'
  if ((acmeServer ?? '').trim().toLowerCase() === 'custom') return '手动上传的证书没有 ACME 账号，无法自动续期'
  return '手动触发续期'
}
