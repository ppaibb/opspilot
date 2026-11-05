import type { DomainDNSRecordCreate, DomainProviderItem } from '@/types/api'

type DNSCapabilities = Partial<Pick<DomainProviderItem, 'supports_multi_values' | 'supports_proxy' | 'supports_priority'>>

/** 凭证类型决定能否用于厂商，与名称及备注无关，兼容历史 AK/SK 别名。 */
export function credentialMatches(type: string, allowed: string[]): boolean {
  const normalize = (value: string) => value.toLowerCase().replace(/[\s_/-]/g, '').replace(/^aksk$/, 'accesskey')
  return allowed.some((value) => normalize(value) === normalize(type))
}

/** 按能力组装写请求，保留多值、自动TTL及MX优先级0，不把代理误用于MX/TXT。 */
export function buildRecordPayload(values: DomainDNSRecordCreate, capabilities: DNSCapabilities): DomainDNSRecordCreate {
  return {
    ...values,
    record_values: capabilities.supports_multi_values
      ? values.record_value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean) : undefined,
    proxied: capabilities.supports_proxy
      ? ['A', 'AAAA', 'CNAME'].includes(values.record_type ?? 'A') && !!values.proxied : undefined,
    priority: capabilities.supports_priority && values.record_type === 'MX' ? values.priority ?? 10 : undefined,
  }
}
