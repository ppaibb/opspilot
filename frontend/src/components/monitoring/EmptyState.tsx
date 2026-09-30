/**
 * 空态三分：未接入 / 无结果 / 权限不足。
 * 文案可操作——告诉用户下一步点哪里。
 */
import { Button, Empty, Flex, Result } from 'antd'
import { useNavigate } from 'react-router-dom'

export type EmptyKind = 'not-connected' | 'no-result' | 'forbidden'

const PRESET: Record<EmptyKind, { title: string; desc: string; action?: string; to?: string }> = {
  'not-connected': {
    title: '尚未接入监控',
    desc: '先在「系统设置 → 系统管理 → 监控连接」填写 VictoriaMetrics / VictoriaLogs 地址（支持分体部署），再回来查询。',
    action: '去配置',
    to: '/settings/system',
  },
  'no-result': {
    title: '没有匹配结果',
    desc: '换个时间范围、放宽过滤条件，或用 * 再试一次。',
  },
  forbidden: {
    title: '权限不足',
    desc: '当前角色没有查看该监控数据的权限，请联系管理员在「权限管理」中开通。',
  },
}

export default function EmptyState({
  kind,
  title,
  description,
  actionLabel,
  onAction,
}: {
  kind: EmptyKind
  title?: string
  description?: string
  actionLabel?: string
  onAction?: () => void
}) {
  const navigate = useNavigate()
  const preset = PRESET[kind]
  const actionText = actionLabel || preset.action

  if (kind === 'forbidden') {
    return (
      <Result
        status="403"
        title={title || preset.title}
        subTitle={description || preset.desc}
        extra={
          actionText ? (
            <Button type="primary" onClick={onAction || (() => navigate(preset.to || '/'))}>
              {actionText}
            </Button>
          ) : undefined
        }
      />
    )
  }

  return (
    <Flex align="center" justify="center" style={{ padding: '48px 16px' }}>
      <Empty
        description={
          <div>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{title || preset.title}</div>
            <div style={{ color: '#8c8c8c', fontSize: 13 }}>{description || preset.desc}</div>
          </div>
        }
      >
        {actionText ? (
          <Button type="primary" onClick={onAction || (preset.to ? () => navigate(preset.to!) : undefined)}>
            {actionText}
          </Button>
        ) : null}
      </Empty>
    </Flex>
  )
}
