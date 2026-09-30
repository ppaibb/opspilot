/**
 * 链路自检面板（仅管理员/系统配置权限可见）：采集 / 评估 / 渠道三灯。
 * 默认折叠；整体非绿时自动展开，方便排障。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Space, Tag, Typography, Tooltip, Button, Flex, Collapse } from 'antd'
import {
  CheckCircleFilled, CloseCircleFilled, ExclamationCircleFilled, ReloadOutlined,
} from '@ant-design/icons'
import { monitoringApi } from '@/api'
import { useAuthStore, isAdminRole } from '@/store/auth'
import { usePermission } from '@/hooks/usePermission'

const { Text } = Typography

export type Lamp = 'green' | 'yellow' | 'red'

function LampIcon({ lamp }: { lamp: Lamp }) {
  if (lamp === 'green') return <CheckCircleFilled style={{ color: '#52c41a' }} />
  if (lamp === 'yellow') return <ExclamationCircleFilled style={{ color: '#faad14' }} />
  return <CloseCircleFilled style={{ color: '#ff4d4f' }} />
}

const LAMP_LABEL: Record<Lamp, string> = { green: '正常', yellow: '注意', red: '异常' }
const LAMP_COLOR: Record<Lamp, string> = { green: 'success', yellow: 'warning', red: 'error' }

/** 把底层术语换成用户能懂的说法 */
function friendlyName(name: string) {
  if (name.includes('采集')) return '最近采集'
  if (name.includes('评估')) return '最近评估'
  if (name.includes('渠道') || name.includes('通知')) return '通知渠道'
  return name
}

export default function LinkHealthPanel({ compact = false }: { compact?: boolean }) {
  const role = useAuthStore((s) => s.userInfo?.role || '')
  const { has } = usePermission()
  const canSee = isAdminRole(role) || has('module.settings.system') || has('action.settings.system.manage')

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['monitoring-link-health'],
    queryFn: () => monitoringApi.linkHealth(),
    refetchInterval: 60_000,
    enabled: canSee,
  })

  const checks = data?.checks || []
  const overall = (data?.overall as Lamp) || 'yellow'
  // 正常时收起，异常/注意时自动展开（渲染期调整，避免 effect 里同步 setState）
  const [open, setOpen] = useState(false)
  const [prevOverall, setPrevOverall] = useState(overall)
  if (prevOverall !== overall) {
    setPrevOverall(overall)
    if (overall !== 'green') setOpen(true)
  }

  const body = useMemo(
    () => (
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {checks.map((c: { key: string; name: string; lamp: Lamp; message: string; last_at?: string }) => (
          <Flex key={c.key} align="flex-start" gap={10} justify="space-between">
            <Space align="start">
              <LampIcon lamp={c.lamp} />
              <div>
                <div>
                  <Text strong>{friendlyName(c.name)}</Text>{' '}
                  <Tag color={LAMP_COLOR[c.lamp]} style={{ marginInlineStart: 4 }}>
                    {LAMP_LABEL[c.lamp]}
                  </Tag>
                </div>
                <Tooltip title={c.last_at ? `最近：${c.last_at}` : undefined}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{c.message}</Text>
                </Tooltip>
              </div>
            </Space>
          </Flex>
        ))}
        {checks.length === 0 && <Text type="secondary">加载中…</Text>}
      </Space>
    ),
    [checks],
  )

  if (!canSee) return null

  return (
    <Card
      size="small"
      title={
        <Space>
          <LampIcon lamp={overall} />
          链路自检
          <Tag color={LAMP_COLOR[overall]}>{LAMP_LABEL[overall]}</Tag>
        </Space>
      }
      extra={
        <Button size="small" type="text" icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>
          刷新
        </Button>
      }
      style={compact ? undefined : { marginBottom: 16 }}
    >
      <Collapse
        ghost
        activeKey={open ? ['detail'] : []}
        onChange={(keys) => setOpen(keys.includes('detail'))}
        items={[{
          key: 'detail',
          label: <Text type="secondary" style={{ fontSize: 12 }}>{open ? '收起明细' : '展开明细'}</Text>,
          children: body,
        }]}
      />
    </Card>
  )
}
