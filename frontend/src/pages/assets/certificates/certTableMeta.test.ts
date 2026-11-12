import { describe, expect, it } from 'vitest'
import {
  CERT_STATUS_BADGE_BG, CERT_STATUS_BADGE_PULSE, EXPIRY_TONE_COLOR,
  certStatusBadgeBg, expiryDisplay, formatIssuer, formatKeyType,
  isRenewDisabled, matchProviderFilter, matchStatusFilter, renewDisabledReason,
} from './certTableMeta'

describe('formatKeyType', () => {
  it('映射已知密钥类型', () => {
    expect(formatKeyType('ec-256')).toBe('EC(256)')
    expect(formatKeyType('ec-384')).toBe('EC(384)')
    expect(formatKeyType('rsa-2048')).toBe('RSA(2048)')
    expect(formatKeyType('rsa-4096')).toBe('RSA(4096)')
  })
  it('未知取值原样回落，无值显示 —', () => {
    expect(formatKeyType('ed25519')).toBe('ed25519')
    expect(formatKeyType(undefined)).toBe('—')
    expect(formatKeyType('')).toBe('—')
  })
})

describe('formatIssuer', () => {
  it('映射 ACME 服务商', () => {
    expect(formatIssuer('letsencrypt')).toBe("Let's Encrypt")
    expect(formatIssuer('letsencrypt_test')).toBe('Let\'s Encrypt（测试）')
    expect(formatIssuer('custom')).toBe('手动上传')
  })
  it('未知取值原样回落，无值显示 —', () => {
    expect(formatIssuer('zerossl')).toBe('zerossl')
    expect(formatIssuer(undefined)).toBe('—')
  })
})

describe('expiryDisplay', () => {
  it('无值显示 —', () => {
    expect(expiryDisplay(null)).toEqual({ text: '—', tone: 'muted' })
  })
  it('已过期为红', () => {
    expect(expiryDisplay(-1)).toEqual({ text: '已过期', tone: 'red' })
  })
  it('≤30 天为橙', () => {
    expect(expiryDisplay(0)).toEqual({ text: '剩余 0 天', tone: 'orange' })
    expect(expiryDisplay(30)).toEqual({ text: '剩余 30 天', tone: 'orange' })
  })
  it('>30 天为绿', () => {
    expect(expiryDisplay(31)).toEqual({ text: '剩余 31 天', tone: 'green' })
    expect(expiryDisplay(82)).toEqual({ text: '剩余 82 天', tone: 'green' })
  })
  it('每种 tone 都有颜色', () => {
    for (const tone of ['green', 'orange', 'red', 'muted'] as const) {
      expect(EXPIRY_TONE_COLOR[tone]).toMatch(/^#/)
    }
  })
})

describe('certStatusBadgeBg', () => {
  it('已签发/有效为绿，签发中/续期中为蓝', () => {
    expect(certStatusBadgeBg('issued')).toBe('#52c41a')
    expect(certStatusBadgeBg('valid')).toBe('#52c41a')
    expect(certStatusBadgeBg('issuing')).toBe('#1677ff')
    expect(certStatusBadgeBg('renewing')).toBe('#1677ff')
  })
  it('失败/已过期为红，申请中/已吊销为灰', () => {
    expect(certStatusBadgeBg('failed')).toBe('#ff4d4f')
    expect(certStatusBadgeBg('expired')).toBe('#ff4d4f')
    expect(certStatusBadgeBg('pending')).toBe('#8c8c8c')
    expect(certStatusBadgeBg('revoked')).toBe('#8c8c8c')
  })
  it('未知状态回落灰色', () => {
    expect(certStatusBadgeBg('whatever')).toBe('#8c8c8c')
    expect(certStatusBadgeBg(undefined)).toBe('#8c8c8c')
  })
  it('呼吸点只覆盖进行中状态', () => {
    expect(CERT_STATUS_BADGE_PULSE).toEqual(['issuing', 'renewing'])
    expect(Object.keys(CERT_STATUS_BADGE_BG)).toContain('expiring')
  })
})

describe('matchProviderFilter', () => {
  it('all 不过滤', () => {
    expect(matchProviderFilter('aliyun', 'all')).toBe(true)
    expect(matchProviderFilter(undefined, 'all')).toBe(true)
  })
  it('按厂商精确匹配', () => {
    expect(matchProviderFilter('aliyun', 'aliyun')).toBe(true)
    expect(matchProviderFilter('tencent', 'aliyun')).toBe(false)
    expect(matchProviderFilter('tencent', 'tencent')).toBe(true)
  })
  it('other 兜底非阿里云/腾讯云', () => {
    expect(matchProviderFilter('custom', 'other')).toBe(true)
    expect(matchProviderFilter('aws', 'other')).toBe(true)
    expect(matchProviderFilter('aliyun', 'other')).toBe(false)
  })
})

describe('matchStatusFilter', () => {
  it('all 不过滤，其余精确匹配', () => {
    expect(matchStatusFilter('issued', 'all')).toBe(true)
    expect(matchStatusFilter('issued', 'issued')).toBe(true)
    expect(matchStatusFilter('failed', 'issued')).toBe(false)
    expect(matchStatusFilter(undefined, 'issued')).toBe(false)
  })
})

describe('isRenewDisabled / renewDisabledReason', () => {
  it('已吊销禁用续签（与后端 409 校验一致）', () => {
    expect(isRenewDisabled('revoked', 'aliyun', 'letsencrypt')).toBe(true)
    expect(renewDisabledReason('revoked', 'aliyun', 'letsencrypt')).toBe(
      '已吊销证书不可续签，如需重新签发请先删除该记录',
    )
  })
  it('已签发/即将到期/已过期可续签', () => {
    expect(isRenewDisabled('issued', 'aliyun', 'letsencrypt')).toBe(false)
    expect(isRenewDisabled('expiring', 'aliyun', 'letsencrypt')).toBe(false)
    expect(isRenewDisabled('expired', 'tencent', 'letsencrypt')).toBe(false)
    expect(renewDisabledReason('issued', 'aliyun', 'letsencrypt')).toBe('手动触发续期')
  })
  it('进行中与手动上传仍禁用', () => {
    expect(isRenewDisabled('renewing', 'aliyun', 'letsencrypt')).toBe(true)
    expect(isRenewDisabled('issuing', 'aliyun', 'letsencrypt')).toBe(true)
    expect(isRenewDisabled('issued', 'custom', 'custom')).toBe(true)
    expect(renewDisabledReason('issued', 'custom', 'custom')).toBe(
      '手动上传的证书没有 ACME 账号，无法自动续期',
    )
  })
})
