/** 证书签发日志轮询间隔（毫秒） */
export const CERT_LOG_POLL_INTERVAL_MS = 2500
/** 证书列表在存在进行中任务时的轮询间隔（毫秒） */
export const CERT_LIST_POLL_INTERVAL_MS = 3000

/**
 * 进行中的证书状态：日志与列表都需要轮询。
 * pending 是新建记录的初始状态（后台任务随后才置为 issuing），必须包含，
 * 否则提交后抽屉与列表都会在任务启动前卡住不动。
 */
export const CERT_TRANSIENT_STATUSES = ['pending', 'issuing', 'renewing'] as const

/** 该状态是否处于进行中（日志抽屉与列表需要轮询） */
export function isTransientCertStatus(status?: string | null): boolean {
  if (!status) return false
  return (CERT_TRANSIENT_STATUSES as readonly string[]).includes(status)
}

/** 证书列表是否应轮询：任一证书处于进行中状态 */
export function shouldPollCertList(statuses: Array<string | undefined | null>): boolean {
  return statuses.some((s) => isTransientCertStatus(s))
}

/** 日志行是否为 ERROR 行（后端格式：[时间] [LEVEL] 内容） */
export function isCertLogErrorLine(line: string): boolean {
  return line.includes('[ERROR]')
}
