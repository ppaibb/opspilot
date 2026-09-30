/**
 * 资产状态词表契约：前端 STATUS_MAP 必须覆盖后端实际写入的枚举。
 * 回归 2026-09-28：集群 running 未映射显示「未知」、证书 issuing/failed 误落已吊销、虚机 error 显示未知。
 */
import { describe, it, expect } from 'vitest'

// 与各页面 STATUS_MAP 保持同步的后端写入值（断言页内 map 时动态 import 页面源太重，这里钉「应有键」）
// 页面模块在测试里可被 import（纯常量 + React 组件），直接读取导出前用字符串键做契约检查。

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function mapKeys(rel: string, mapName = 'STATUS_MAP'): string[] {
  const src = readFileSync(path.join(root, rel), 'utf8')
  const start = src.indexOf(`const ${mapName}`)
  if (start < 0) throw new Error(`${rel} 未找到 ${mapName}`)
  const brace = src.indexOf('{', start)
  const end = src.indexOf('\n}', brace)
  const body = src.slice(brace, end + 2)
  return [...body.matchAll(/^\s{2}([A-Za-z0-9_]+):/gm)].map((m) => m[1])
}

describe('集群状态词表', () => {
  it('覆盖后端 create/refresh 写入的 running 与 disconnected', () => {
    const keys = mapKeys('pages/assets/clusters/index.tsx')
    expect(keys).toContain('running')
    expect(keys).toContain('disconnected')
    expect(keys).toContain('pending')
    expect(keys).toContain('unknown')
  })
})

describe('证书状态词表', () => {
  it('覆盖签发/续期/失败等过程态', () => {
    const keys = mapKeys('pages/assets/certificates/index.tsx')
    for (const k of ['pending', 'issuing', 'renewing', 'issued', 'failed', 'expiring', 'expired', 'revoked']) {
      expect(keys, `证书 STATUS_MAP 缺少 ${k}`).toContain(k)
    }
  })
})

describe('虚拟机状态词表', () => {
  it('覆盖后端校验允许的 pending/error', () => {
    const keys = mapKeys('pages/assets/vms/index.tsx')
    for (const k of ['running', 'stopped', 'paused', 'pending', 'error', 'unknown']) {
      expect(keys, `虚机 STATUS_MAP 缺少 ${k}`).toContain(k)
    }
  })
})
