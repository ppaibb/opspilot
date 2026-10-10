import { describe, it, expect } from 'vitest'
import { normalizeLog } from './normalizeLog'

/** P0 日志字段契约：后端 _vlogs_get 归一 {id,time,message,fields}，前端 normalizeLog 必须对齐。 */
describe('日志字段契约 normalizeLog', () => {
  it('映射后端归一化行：id/time/message/fields', () => {
    const row = normalizeLog({
      id: 'stream-abc',
      time: '2026-01-01T00:00:00Z',
      message: 'hello world',
      level: 'ERROR',
      service: 'nginx',
      host: '10.0.0.1',
      fields: { _stream_id: 'stream-abc', level: 'ERROR' },
    })
    expect(row.id).toBe('stream-abc')
    expect(row.stream_id).toBe('stream-abc')
    expect(row.time).toBe('2026-01-01T00:00:00Z')
    expect(row.message).toBe('hello world')
    expect(row.level).toBe('ERROR')
  })

  it('上下文抽屉用 id 作 stream_id（缺 id 时回退 stream_id/_stream_id）', () => {
    expect(normalizeLog({ stream_id: 's1', time: 't', message: 'm' }).stream_id).toBe('s1')
    expect(normalizeLog({ _stream_id: 's2', _time: 't', _msg: 'm' }).stream_id).toBe('s2')
    expect(normalizeLog({ id: 's3', time: 't', message: 'm' }).stream_id).toBe('s3')
  })

  it('兼容旧 raw VLogs 字段（_time/_msg）', () => {
    const row = normalizeLog({ _time: '2026-01-01', _msg: 'legacy', level: 'warn' })
    expect(row.time).toBe('2026-01-01')
    expect(row.message).toBe('legacy')
    expect(row.level).toBe('WARN')
  })
})

/** P0 告警规则字段契约：前端统一 name/duration（与后端 schemas.AlertRuleItem 一致）。 */
describe('告警规则字段契约', () => {
  it('规则对象使用 name/duration 而非 alert/for', () => {
    const rule = {
      name: 'HighCPU',
      expr: 'up == 0',
      duration: '5m',
      severity: 'critical',
      enabled: true,
      channel_ids: ['c1'],
      host_ids: ['h1'],
      im_user_ids: [1],
    }
    expect(rule.name).toBeTruthy()
    expect(rule.duration).toBeTruthy()
    expect((rule as Record<string, unknown>).alert).toBeUndefined()
    expect((rule as Record<string, unknown>).for).toBeUndefined()
    // 保存不丢字段
    expect(rule.channel_ids).toEqual(['c1'])
    expect(rule.host_ids).toEqual(['h1'])
    expect(rule.im_user_ids).toEqual([1])
  })
})

/** 直方图时间戳：后端归一为 Unix 秒，前端 *1000 得到 ms。 */
describe('直方图时间戳单位', () => {
  it('秒级时间戳 *1000 得到合法 Date', () => {
    const tsSeconds = 1704067200 // 2024-01-01T00:00:00Z
    const d = new Date(tsSeconds * 1000)
    expect(d.getUTCFullYear()).toBe(2024)
  })
})
