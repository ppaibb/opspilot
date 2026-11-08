import { describe, expect, it } from 'vitest'
import { queryKeys } from './queryKeys'

describe('query key factories', () => {
  it('nests list and detail keys below a stable all key', () => {
    expect(queryKeys.assets.list({ group: 1 }).slice(0, 1)).toEqual(queryKeys.assets.all)
    expect(queryKeys.applications.detail('a1').slice(0, 1)).toEqual(queryKeys.applications.all)
    expect(queryKeys.logs.query({ q: '*' }).slice(0, 1)).toEqual(queryKeys.logs.all)
  })
})
