import { describe, it, expect } from 'vitest'
import type { HostMetricPoint } from '@/types/api'

/** 实时看板对比摘要 / 最近告警字段契约（与后端只增不减对齐）。 */

/** 取序列最后一个非空点（对比摘要用「当前值」）。 */
function lastNumber(points: HostMetricPoint[], field: string): number | null {
  for (let i = points.length - 1; i >= 0; i -= 1) {
    const raw = points[i]?.[field]
    if (raw != null && !Number.isNaN(Number(raw))) return Number(raw)
  }
  return null
}

function formatNetPair(i: number | null, o: number | null): string {
  const fmt = (n: number | null) => (n == null ? '-' : n.toFixed(2))
  return `${fmt(i)} · ${fmt(o)}`
}

const points: HostMetricPoint[] = [
  { time: '10:00', cpu: 10, memory: 20, load1: 0.5, networkIn: 1, networkOut: 2, diskRead: 0.1, diskWrite: 0.2 },
  { time: '10:01', cpu: 55, memory: 60, load1: 1.2, networkIn: 3, networkOut: 4, diskRead: 0.3, diskWrite: 0.4 },
  { time: '10:02', cpu: null, memory: null, load1: null, networkIn: null, networkOut: null, diskRead: null, diskWrite: null },
]

describe('实时看板对比摘要', () => {
  it('当前值取最后一个非空点', () => {
    expect(lastNumber(points, 'cpu')).toBe(55)
    expect(lastNumber(points, 'memory')).toBe(60)
    expect(lastNumber(points, 'load1')).toBe(1.2)
    expect(lastNumber(points, 'networkIn')).toBe(3)
    expect(lastNumber(points, 'networkOut')).toBe(4)
    expect(lastNumber(points, 'diskRead')).toBe(0.3)
    expect(lastNumber(points, 'diskWrite')).toBe(0.4)
  })

  it('全空序列返回 null（优雅降级）', () => {
    expect(lastNumber([], 'cpu')).toBeNull()
    expect(lastNumber([{ time: 'x' }], 'load1')).toBeNull()
  })

  it('网络摘要格式为 in · out', () => {
    expect(formatNetPair(1.25, 3.5)).toBe('1.25 · 3.50')
    expect(formatNetPair(null, null)).toBe('- · -')
  })
})

describe('对比模式指标字段', () => {
  it('HostMetricPoint 支持 load / disk IO 序列', () => {
    const p: HostMetricPoint = {
      time: 'now',
      load1: 0.8,
      diskRead: 1.5,
      diskWrite: 2.5,
    }
    expect(p.load1).toBe(0.8)
    expect(p.diskRead).toBe(1.5)
    expect(p.diskWrite).toBe(2.5)
  })
})
