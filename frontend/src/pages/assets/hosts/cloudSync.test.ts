import { describe, expect, it } from 'vitest'
import { isLightCloudInstance, mergeCloudSelection } from './cloudSync'

describe('华为云主机导入', () => {
  it('将 Flexus L 放入轻量服务器分类', () => {
    expect(isLightCloudInstance('FlexusL')).toBe(true)
    expect(isLightCloudInstance('ECS')).toBe(false)
    expect(isLightCloudInstance('Lightsail')).toBe(true)
  })

  it('在轻量 Tab 勾选时保留 ECS 勾选', () => {
    expect(mergeCloudSelection(['ecs-1'], ['light-1'], ['light-1'])).toEqual(['ecs-1', 'light-1'])
  })

  it('取消当前 Tab 勾选时保留其他 Tab 勾选', () => {
    expect(mergeCloudSelection(['ecs-1', 'light-1'], ['light-1'], [])).toEqual(['ecs-1'])
  })
})
