/**
 * 数据库「今日工作台」：按待办而不是按工具排布。
 *
 * 首屏回答三个问题：
 * 1. 我现在要处理什么？（待审批 / 待执行 / 定时中）
 * 2. 最快开工入口在哪？（提单 / 查询）
 * 3. 最近发生了什么？（最近工单）
 */
import { useMemo } from 'react'
import {
  Card, List, Tag, Button, Space, Typography, Flex, Empty, Progress, Tooltip,
} from 'antd'
import {
  PlayCircleOutlined, SafetyCertificateOutlined, ClockCircleOutlined,
  FileTextOutlined, SearchOutlined, UserOutlined, RightOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '@/store/auth'
import { usePermission } from '@/hooks/usePermission'
import type { DbTicket } from '@/types/api'

const { Text, Title } = Typography

const STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '待审批', color: 'gold' },
  approved: { label: '待执行', color: 'blue' },
  executing: { label: '执行中', color: 'processing' },
  success: { label: '成功', color: 'green' },
  failed: { label: '失败', color: 'red' },
  rejected: { label: '已驳回', color: 'default' },
}

function PipelineCard(props: {
  title: string
  count: number
  hint: string
  color: string
  icon: React.ReactNode
  active?: boolean
  onClick: () => void
}) {
  return (
    <Card
      size="small"
      hoverable
      onClick={props.onClick}
      style={{
        borderColor: props.active ? props.color : undefined,
        boxShadow: props.active ? `0 0 0 1px ${props.color}22` : undefined,
        cursor: 'pointer',
      }}
    >
      <Flex align="center" gap={12}>
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: 10,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: `${props.color}14`,
            color: props.color,
            fontSize: 18,
            flexShrink: 0,
          }}
        >
          {props.icon}
        </div>
        <div style={{ minWidth: 0 }}>
          <Flex align="baseline" gap={8}>
            <span style={{ fontSize: 22, fontWeight: 600, lineHeight: 1 }}>{props.count}</span>
            <Text strong style={{ fontSize: 13 }}>{props.title}</Text>
          </Flex>
          <Text type="secondary" style={{ fontSize: 12 }}>{props.hint}</Text>
        </div>
      </Flex>
    </Card>
  )
}

export function WorkbenchPanel({
  tickets,
  onCreateTicket,
  onOpenQuery,
  onJumpTickets,
}: {
  tickets: DbTicket[]
  onCreateTicket: () => void
  onOpenQuery: () => void
  onJumpTickets: (status: string) => void
}) {
  const userInfo = useAuthStore((s) => s.userInfo)
  const myName = userInfo?.name || userInfo?.username || ''
  // 开工卡按提交权限显隐：无 action.db.submit 的用户看不到提单/查询入口（后端本就 403）
  const { has } = usePermission()
  const canSubmit = has('action.db.submit')

  const pending = useMemo(() => tickets.filter((t) => t.status === 'pending'), [tickets])
  const ready = useMemo(() => tickets.filter((t) => t.status === 'approved'), [tickets])
  const scheduled = useMemo(
    () => tickets.filter((t) => t.status === 'approved' && t.execute_after),
    [tickets],
  )
  const mine = useMemo(
    () => tickets.filter((t) => t.submitter === myName).slice(0, 8),
    [tickets, myName],
  )
  const recent = useMemo(() => tickets.slice(0, 6), [tickets])

  const doneRate = useMemo(() => {
    const finished = tickets.filter((t) => t.status === 'success' || t.status === 'rejected').length
    return tickets.length ? Math.round((finished / tickets.length) * 100) : 0
  }, [tickets])

  return (
    <Flex vertical gap={16}>
      {/* 快捷开工（需要提交权限；普通只读角色整卡隐藏） */}
      {canSubmit && (
        <Card size="small" styles={{ body: { padding: 16 } }}>
          <Flex justify="space-between" align="center" wrap gap={12}>
            <div>
              <Title level={5} style={{ margin: 0 }}>
                <ThunderboltOutlined style={{ color: '#faad14', marginRight: 8 }} />
                开工
              </Title>
              <Text type="secondary" style={{ fontSize: 12 }}>
                提单会先过规则预审；查询需有效授权
              </Text>
            </div>
            <Space>
              <Button type="primary" icon={<FileTextOutlined />} onClick={onCreateTicket}>
                提交 SQL 工单
              </Button>
              <Button icon={<SearchOutlined />} onClick={onOpenQuery}>
                自助查询
              </Button>
            </Space>
          </Flex>
        </Card>
      )}

      {/* 今日流水线 */}
      <div>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
          今日流水线 · 点卡片直达列表
        </Text>
        <Flex gap={12} wrap>
          <div style={{ flex: '1 1 180px', minWidth: 180 }}>
            <PipelineCard
              title="待审批"
              count={pending.length}
              hint="等审批人放行"
              color="#faad14"
              icon={<SafetyCertificateOutlined />}
              onClick={() => onJumpTickets('pending')}
            />
          </div>
          <div style={{ flex: '1 1 180px', minWidth: 180 }}>
            <PipelineCard
              title="待执行"
              count={ready.length - scheduled.length}
              hint="已批准，可立刻执行"
              color="#1677ff"
              icon={<PlayCircleOutlined />}
              onClick={() => onJumpTickets('approved')}
            />
          </div>
          <div style={{ flex: '1 1 180px', minWidth: 180 }}>
            <PipelineCard
              title="定时中"
              count={scheduled.length}
              hint="到了窗口自动提醒"
              color="#722ed1"
              icon={<ClockCircleOutlined />}
              onClick={() => onJumpTickets('approved')}
            />
          </div>
          <div style={{ flex: '1 1 180px', minWidth: 180 }}>
            <Card size="small">
              <Flex align="center" gap={12}>
                <div style={{ width: 40, height: 40, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <Progress type="circle" size={40} percent={doneRate} strokeColor="#52c41a" />
                </div>
                <div>
                  <Text strong style={{ fontSize: 13 }}>闭环率</Text>
                  <div>
                    <Text type="secondary" style={{ fontSize: 12 }}>成功 + 驳回 占比</Text>
                  </div>
                </div>
              </Flex>
            </Card>
          </div>
        </Flex>
      </div>

      <Flex gap={16} wrap align="flex-start">
        {/* 我的申请 */}
        <Card
          size="small"
          style={{ flex: '1 1 320px', minWidth: 280 }}
          title={
            <Space>
              <UserOutlined />
              <span>我的申请</span>
            </Space>
          }
          extra={
            <Button type="link" size="small" onClick={() => onJumpTickets('')}>
              全部 <RightOutlined />
            </Button>
          }
        >
          {mine.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={myName ? `${myName} 还没有工单` : '暂无'}
            >
              <Button size="small" type="primary" onClick={onCreateTicket}>
                提交第一张
              </Button>
            </Empty>
          ) : (
            <List
              size="small"
              dataSource={mine}
              renderItem={(t) => {
                const meta = STATUS_META[t.status] || { label: t.status, color: 'default' }
                return (
                  <List.Item
                    style={{ padding: '10px 0', cursor: 'pointer' }}
                    onClick={() => onJumpTickets(t.status)}
                  >
                    <List.Item.Meta
                      title={
                        <Flex justify="space-between" gap={8}>
                          <Text ellipsis style={{ maxWidth: 220 }}>{t.title || t.sql_text?.slice(0, 24)}</Text>
                          <Tag color={meta.color} style={{ marginRight: 0 }}>{meta.label}</Tag>
                        </Flex>
                      }
                      description={
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t.instance_name || t.database || '-'}
                          {t.created_at ? ` · ${String(t.created_at).slice(5, 16)}` : ''}
                        </Text>
                      }
                    />
                  </List.Item>
                )
              }}
            />
          )}
        </Card>

        {/* 最近动态 */}
        <Card
          size="small"
          style={{ flex: '1 1 320px', minWidth: 280 }}
          title="最近工单"
          extra={
            <Tooltip title="按提交时间">
              <Text type="secondary" style={{ fontSize: 12 }}>滚动窗口</Text>
            </Tooltip>
          }
        >
          {recent.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有工单" />
          ) : (
            <List
              size="small"
              dataSource={recent}
              renderItem={(t) => {
                const meta = STATUS_META[t.status] || { label: t.status, color: 'default' }
                return (
                  <List.Item style={{ padding: '10px 0', cursor: 'pointer' }} onClick={() => onJumpTickets('')}>
                    <List.Item.Meta
                      title={
                        <Flex justify="space-between" gap={8}>
                          <Text ellipsis style={{ maxWidth: 200 }}>{t.title || t.sql_text?.slice(0, 24)}</Text>
                          <Tag color={meta.color} style={{ marginRight: 0 }}>{meta.label}</Tag>
                        </Flex>
                      }
                      description={
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t.submitter || '-'}
                          {t.instance_name ? ` → ${t.instance_name}` : ''}
                        </Text>
                      }
                    />
                  </List.Item>
                )
              }}
            />
          )}
        </Card>
      </Flex>
    </Flex>
  )
}
