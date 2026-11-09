import { describe, expect, it } from 'vitest'
import {
  CERT_DEPLOY_POLL_INTERVAL_MS,
  deployStatusText,
  deployStatusTagColor,
  deploySuccessCount,
  deployTargetTypeText,
  hasRunningDeployment,
} from './certDeployHistory'
import type { CertificateDeployment } from '@/types/api'

function deployment(status: string): CertificateDeployment {
  return {
    deploy_id: `d-${status}`,
    cert_id: 'CERT-1',
    target_type: 'host',
    targets: 'HOST-1',
    status,
    results: [],
    success_count: 0,
    total_count: 0,
    triggered_by: 'op',
    started_at: '2026-11-09 10:00:00',
    finished_at: '',
  }
}

describe('部署历史状态映射', () => {
  it('状态文案：running 进行中、success 成功、partial 部分成功、failed 失败、recorded 仅记录未推送', () => {
    expect(deployStatusText('running')).toBe('进行中')
    expect(deployStatusText('success')).toBe('成功')
    expect(deployStatusText('partial')).toBe('部分成功')
    expect(deployStatusText('failed')).toBe('失败')
    // 云路只记录目标、未调用云厂商接口：不是成功，也不是失败
    expect(deployStatusText('recorded')).toBe('仅记录，未推送')
  })

  it('未知/空状态回落原文或 —', () => {
    expect(deployStatusText('queued')).toBe('queued')
    expect(deployStatusText('')).toBe('—')
    expect(deployStatusText(undefined)).toBe('—')
    expect(deployStatusText(null)).toBe('—')
  })

  it('状态徽章颜色与文案配对', () => {
    expect(deployStatusTagColor('running')).toBe('processing')
    expect(deployStatusTagColor('success')).toBe('success')
    expect(deployStatusTagColor('partial')).toBe('warning')
    expect(deployStatusTagColor('failed')).toBe('error')
    expect(deployStatusTagColor('recorded')).toBe('warning')
    expect(deployStatusTagColor('queued')).toBe('default')
    expect(deployStatusTagColor(undefined)).toBe('default')
  })

  it('目标类型文案：host 主机、cloud 云服务、k8s K8s', () => {
    expect(deployTargetTypeText('host')).toBe('主机')
    expect(deployTargetTypeText('cloud')).toBe('云服务')
    expect(deployTargetTypeText('k8s')).toBe('K8s')
    expect(deployTargetTypeText('other')).toBe('other')
    expect(deployTargetTypeText('')).toBe('—')
  })

  it('成功台数按 results 的 success 计数', () => {
    expect(deploySuccessCount([])).toEqual({ success: 0, total: 0 })
    expect(deploySuccessCount(undefined)).toEqual({ success: 0, total: 0 })
    expect(
      deploySuccessCount([
        { target: 'HOST-1', success: true, message: 'ok' },
        { target: 'HOST-2', success: false, message: 'fail' },
        { target: 'HOST-3', success: true, message: 'ok' },
      ]),
    ).toEqual({ success: 2, total: 3 })
  })

  it('仅记录的结果不计入成功台数', () => {
    expect(
      deploySuccessCount([
        { target: 'aliyun-cdn', success: false, recorded: true, message: '仅记录，未推送' },
      ]),
    ).toEqual({ success: 0, total: 1 })
  })

  it('任一记录 running 时需要轮询，否则不轮询', () => {
    expect(hasRunningDeployment([])).toBe(false)
    expect(hasRunningDeployment(undefined)).toBe(false)
    expect(hasRunningDeployment([deployment('success'), deployment('failed')])).toBe(false)
    expect(hasRunningDeployment([deployment('success'), deployment('running')])).toBe(true)
    expect(hasRunningDeployment([deployment('partial'), null, undefined])).toBe(false)
  })

  it('轮询间隔为正数', () => {
    expect(CERT_DEPLOY_POLL_INTERVAL_MS).toBeGreaterThan(0)
  })
})
