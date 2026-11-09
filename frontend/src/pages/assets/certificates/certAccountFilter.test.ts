import { describe, expect, it } from 'vitest'
import type { CertificateAccountItem, DomainResponse } from '@/types/api'
import { filterCertAccountsByDomain, findDomainProvider, normalizeDomainInput } from './certAccountFilter'

const dom = (domain: string, provider: string): DomainResponse => ({
  id: 1,
  domain,
  root_domain: domain,
  record_type: 'A',
  record_value: '-',
  provider,
  status: 'active',
})

const acc = (account_id: string, provider_key: string): CertificateAccountItem => ({
  account_id,
  account_name: account_id,
  provider_key,
  provider_label: provider_key,
  credential_type: 'AccessKey',
})

const DOMAINS = [dom('nnyy.ltd', 'tencent'), dom('ppaibb.com', 'aliyun')]
const ACCOUNTS = [acc('CRED-CEA61D', 'tencent'), acc('CRED-EA3B77', 'aliyun'), acc('CRED-B29678', 'aliyun')]

describe('DNS 校验凭证按主域名厂商过滤', () => {
  it('归一化：去空格/小写/去掉 *. 前缀', () => {
    expect(normalizeDomainInput('  *.Nnyy.LTD ')).toBe('nnyy.ltd')
    expect(normalizeDomainInput('')).toBe('')
  })

  it('根域名精确匹配所属厂商', () => {
    expect(findDomainProvider('nnyy.ltd', DOMAINS)).toBe('tencent')
    expect(findDomainProvider('ppaibb.com', DOMAINS)).toBe('aliyun')
  })

  it('子域名按后缀归属根域名，通配符主域名同样匹配', () => {
    expect(findDomainProvider('www.nnyy.ltd', DOMAINS)).toBe('tencent')
    expect(findDomainProvider('*.ppaibb.com', DOMAINS)).toBe('aliyun')
    expect(findDomainProvider('api.www.nnyy.ltd', DOMAINS)).toBe('tencent')
  })

  it('主域名为空或未登记时返回 null', () => {
    expect(findDomainProvider('', DOMAINS)).toBeNull()
    expect(findDomainProvider('example.com', DOMAINS)).toBeNull()
  })

  it('nnyy.ltd 只保留腾讯云凭证', () => {
    const r = filterCertAccountsByDomain('nnyy.ltd', DOMAINS, ACCOUNTS)
    expect(r.provider).toBe('tencent')
    expect(r.domainKnown).toBe(true)
    expect(r.accounts.map((a) => a.account_id)).toEqual(['CRED-CEA61D'])
  })

  it('通配符与子域名主域名同样过滤', () => {
    expect(filterCertAccountsByDomain('*.ppaibb.com', DOMAINS, ACCOUNTS).accounts.map((a) => a.account_id))
      .toEqual(['CRED-EA3B77', 'CRED-B29678'])
    expect(filterCertAccountsByDomain('WWW.NNYY.LTD', DOMAINS, ACCOUNTS).accounts.map((a) => a.account_id))
      .toEqual(['CRED-CEA61D'])
  })

  it('主域名为空或未登记时不过滤，显示全部', () => {
    const empty = filterCertAccountsByDomain('', DOMAINS, ACCOUNTS)
    expect(empty.accounts).toEqual(ACCOUNTS)
    expect(empty.domainKnown).toBe(false)
    const unknown = filterCertAccountsByDomain('example.com', DOMAINS, ACCOUNTS)
    expect(unknown.accounts).toEqual(ACCOUNTS)
    expect(unknown.domainKnown).toBe(false)
  })

  it('厂商键大小写不一致时仍能匹配', () => {
    const accounts = [acc('CRED-X', 'Tencent')]
    expect(filterCertAccountsByDomain('nnyy.ltd', DOMAINS, accounts).accounts).toEqual(accounts)
  })
})
