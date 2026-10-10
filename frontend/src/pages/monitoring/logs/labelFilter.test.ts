import { describe, it, expect } from 'vitest'
import { composeLogsQuery, stripLabeledTerms } from './labelFilter'

describe('日志标签筛选 LogsQL 组装', () => {
  it('空标签时保持自由查询', () => {
    expect(composeLogsQuery('*', {})).toBe('*')
    expect(composeLogsQuery('level:ERROR', {})).toBe('(level:ERROR)')
  })

  it('单标签 AND 拼接', () => {
    expect(composeLogsQuery('*', { host: ['ubuntu'] })).toBe('(host:ubuntu OR instance:ubuntu)')
    expect(composeLogsQuery('*', { service: ['docker', 'mongo'] })).toBe('(service:docker OR service:mongo)')
    expect(composeLogsQuery('error', { host: ['ubuntu'] })).toBe('(error) AND (host:ubuntu OR instance:ubuntu)')
  })

  it('host 标签展开 host OR instance（深链带 IP 也能命中）', () => {
    expect(composeLogsQuery('*', { host: ['192.168.3.235'] })).toBe('(host:192.168.3.235 OR instance:192.168.3.235)')
    expect(composeLogsQuery('*', { host: ['a', 'b'] })).toBe('((host:a OR instance:a) OR (host:b OR instance:b))')
  })

  it('多字段标签 AND', () => {
    expect(composeLogsQuery('*', { host: ['ubuntu'], job: ['container'] })).toBe(
      '(host:ubuntu OR instance:ubuntu) AND job:container',
    )
  })

  it('含空格的值自动加引号', () => {
    expect(composeLogsQuery('*', { host: ['my host'] })).toBe('(host:"my host" OR instance:"my host")')
  })

  it('剥离已选标签片段', () => {
    expect(stripLabeledTerms('host:ubuntu AND level:ERROR', ['host'])).toBe('level:ERROR')
    expect(stripLabeledTerms('*', ['host'])).toBe('*')
  })
})
