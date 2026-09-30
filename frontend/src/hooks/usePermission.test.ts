import { describe, it, expect } from 'vitest'
import { extractAxiosDetail } from './usePermission'

describe('extractAxiosDetail', () => {
  it('returns undefined for non-axios errors', () => {
    expect(extractAxiosDetail(new Error('boom'))).toBeUndefined()
    expect(extractAxiosDetail(null)).toBeUndefined()
  })

  it('reads string detail from axios response', () => {
    const err = {
      isAxiosError: true,
      response: { data: { detail: '权限不足' } },
    }
    expect(extractAxiosDetail(err)).toBe('权限不足')
  })

  it('ignores non-string detail (objects/arrays left to caller)', () => {
    const err = {
      isAxiosError: true,
      response: {
        data: {
          detail: [{ msg: 'field required', loc: ['body', 'name'] }],
        },
      },
    }
    expect(extractAxiosDetail(err)).toBeUndefined()
  })

  it('returns undefined when detail missing', () => {
    const err = {
      isAxiosError: true,
      response: { status: 502, statusText: 'Bad Gateway', data: {} },
    }
    expect(extractAxiosDetail(err)).toBeUndefined()
  })
})
