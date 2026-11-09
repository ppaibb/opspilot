import type { CertificateAccountItem, DomainResponse } from '@/types/api'

/** 主域名归一化：去空格/小写/去掉 `*.` 前缀 */
export function normalizeDomainInput(raw: string): string {
  return (raw || '').trim().toLowerCase().replace(/^\*\./, '')
}

/** 在域名资产中找到主域名所属厂商（按 root_domain/domain 后缀匹配子域名），找不到返回 null */
export function findDomainProvider(primaryDomain: string, domains: DomainResponse[]): string | null {
  const input = normalizeDomainInput(primaryDomain)
  if (!input) return null
  for (const d of domains) {
    const candidates = [d.root_domain, d.domain]
      .map((v) => normalizeDomainInput(v || ''))
      .filter(Boolean)
    for (const cand of candidates) {
      if (input === cand || input.endsWith(`.${cand}`)) {
        const provider = (d.provider || '').trim().toLowerCase()
        return provider || null
      }
    }
  }
  return null
}

export interface FilteredCertAccounts {
  /** 过滤后的凭证列表 */
  accounts: CertificateAccountItem[]
  /** 主域名所属厂商，未找到/未输入时为 null */
  provider: string | null
  /** 主域名是否在域名资产中找到 */
  domainKnown: boolean
}

/**
 * 按主域名所属厂商过滤 DNS 校验凭证：
 * - 主域名为空 → 不过滤，返回全部
 * - 主域名未在域名资产中登记 → 不过滤，返回全部（避免把路堵死）
 * - 找到厂商 → 只保留该厂商的凭证
 */
export function filterCertAccountsByDomain(
  primaryDomain: string,
  domains: DomainResponse[],
  accounts: CertificateAccountItem[],
): FilteredCertAccounts {
  const input = normalizeDomainInput(primaryDomain)
  if (!input) return { accounts, provider: null, domainKnown: false }
  const provider = findDomainProvider(input, domains)
  if (!provider) return { accounts, provider: null, domainKnown: false }
  return {
    accounts: accounts.filter((a) => (a.provider_key || '').trim().toLowerCase() === provider),
    provider,
    domainKnown: true,
  }
}
