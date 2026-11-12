import { describe, expect, it } from 'vitest'
import {
  CERT_LIST_POLL_INTERVAL_MS,
  CERT_LOG_POLL_INTERVAL_MS,
  isCertLogErrorLine,
  isTransientCertStatus,
  shouldPollCertList,
} from './certIssueLog'

describe('签发日志轮询条件', () => {
  it('进行中状态需要轮询：pending/issuing/renewing', () => {
    expect(isTransientCertStatus('pending')).toBe(true)
    expect(isTransientCertStatus('issuing')).toBe(true)
    expect(isTransientCertStatus('renewing')).toBe(true)
  })

  it('终态不需要轮询：issued/valid/failed/revoked/expired/expiring', () => {
    for (const s of ['issued', 'valid', 'failed', 'revoked', 'expired', 'expiring']) {
      expect(isTransientCertStatus(s)).toBe(false)
    }
    expect(isTransientCertStatus('')).toBe(false)
    expect(isTransientCertStatus(undefined)).toBe(false)
    expect(isTransientCertStatus(null)).toBe(false)
  })

  it('列表任一证书进行中时轮询，否则不轮询', () => {
    expect(shouldPollCertList([])).toBe(false)
    expect(shouldPollCertList(['issued', 'valid', 'failed'])).toBe(false)
    expect(shouldPollCertList(['issued', 'issuing'])).toBe(true)
    expect(shouldPollCertList(['pending'])).toBe(true)
    expect(shouldPollCertList(['renewing', 'revoked'])).toBe(true)
    expect(shouldPollCertList([undefined, null, ''])).toBe(false)
  })

  it('ERROR 行按 [ERROR] 标记识别', () => {
    expect(isCertLogErrorLine('[2026-11-09 10:00:00] [ERROR] 签发账户不存在')).toBe(true)
    expect(isCertLogErrorLine('[2026-11-09 10:00:00] [INFO] ACME 订单创建成功')).toBe(false)
    expect(isCertLogErrorLine('[2026-11-09 10:00:00] [WARNING] TXT 记录传播等待结束')).toBe(false)
    // 正文里提到 error 但级别不是 ERROR 时不标红
    expect(isCertLogErrorLine('[2026-11-09 10:00:00] [INFO] error count: 0')).toBe(false)
  })

  it('轮询间隔为正数，日志间隔不大于列表间隔', () => {
    expect(CERT_LOG_POLL_INTERVAL_MS).toBeGreaterThan(0)
    expect(CERT_LIST_POLL_INTERVAL_MS).toBeGreaterThan(0)
    expect(CERT_LOG_POLL_INTERVAL_MS).toBeLessThanOrEqual(CERT_LIST_POLL_INTERVAL_MS)
  })
})
