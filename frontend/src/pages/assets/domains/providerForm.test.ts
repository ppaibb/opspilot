import { describe, expect, it } from 'vitest'
import { buildRecordPayload, credentialMatches } from './providerForm'

describe('DNS 厂商表单语义', () => {
  it('华为云多值逐行提交，保留全部值', () => {
    expect(buildRecordPayload({ record_type: 'A', record_value: '203.0.113.1\n203.0.113.2' },
      { supports_multi_values: true }).record_values).toEqual(['203.0.113.1', '203.0.113.2'])
  })
  it('Cloudflare保留自动TTL和代理开关', () => {
    const payload = buildRecordPayload({ record_type: 'A', record_value: '203.0.113.1', ttl: 1, proxied: true },
      { supports_proxy: true })
    expect(payload.proxied).toBe(true)
    expect(payload.ttl).toBe(1)
  })
  it('Cloudflare MX不能误带代理状态，保留优先级0', () => {
    const payload = buildRecordPayload({ record_type: 'MX', record_value: 'mail.example.com', proxied: true, priority: 0 },
      { supports_proxy: true, supports_priority: true })
    expect(payload.proxied).toBe(false)
    expect(payload.priority).toBe(0)
  })
  it('凭证按类型匹配，不需要通过名称识别厂商', () => {
    expect(credentialMatches('AccessKey', ['Token'])).toBe(false)
    expect(credentialMatches('Token', ['Token'])).toBe(true)
    expect(credentialMatches('ak/sk', ['AccessKey'])).toBe(true)
  })
})
