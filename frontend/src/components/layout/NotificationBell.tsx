import { useEffect, useState } from 'react'
import { Badge, Button, Dropdown, Empty, List, Space, Spin, Tag, Typography, theme } from 'antd'
import {
  BellOutlined, CheckOutlined, CheckCircleFilled, CloseCircleFilled,
  ExclamationCircleFilled, InfoCircleFilled,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useNotificationStore, type NotificationType } from '@/store/notifications'
import { useAuthStore } from '@/store/auth'

const { Text } = Typography

const POLL_INTERVAL = 45_000 // 未读数轮询间隔（毫秒）

const TYPE_ICON: Record<NotificationType, React.ReactNode> = {
  success: <CheckCircleFilled style={{ color: '#52c41a' }} />,
  error:   <CloseCircleFilled style={{ color: '#ff4d4f' }} />,
  warning: <ExclamationCircleFilled style={{ color: '#faad14' }} />,
  info:    <InfoCircleFilled style={{ color: '#1677ff' }} />,
}

const CATEGORY_LABEL: Record<string, string> = {
  certificate: '证书',
  host: '主机',
  vm: '虚拟机',
  agent: 'Agent',
  db: '数据库',
  monitoring: '监控',
  asset: '资产',
  application: '应用',
  system: '系统',
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

/** 顶栏通知铃铛：会话级通知中心（后端接口 ready 后切换数据源） */
export default function NotificationBell() {
  const navigate = useNavigate()
  const { token } = theme.useToken()
  const items = useNotificationStore((s) => s.items)
  const unreadCount = useNotificationStore((s) => s.unreadCount)
  const loading = useNotificationStore((s) => s.loading)
  const fetchList = useNotificationStore((s) => s.fetchList)
  const refreshUnread = useNotificationStore((s) => s.refreshUnread)
  const markRead = useNotificationStore((s) => s.markRead)
  const markAllRead = useNotificationStore((s) => s.markAllRead)
  const connectStream = useNotificationStore((s) => s.connectStream)
  const streamConnected = useNotificationStore((s) => s.streamConnected)
  const authToken = useAuthStore((s) => s.token)

  const [open, setOpen] = useState(false)

  // 挂载后立即拉一次未读数，并尝试建立 SSE 实时连接（连接内部自行取短时效票据）
  useEffect(() => {
    refreshUnread()
    if (!authToken) return
    const disconnect = connectStream()
    return disconnect
  }, [refreshUnread, connectStream, authToken])

  // SSE 未连上时用轮询兜底；连上后停止轮询（实时推送已够）
  useEffect(() => {
    if (streamConnected) return
    const timer = setInterval(refreshUnread, POLL_INTERVAL)
    return () => clearInterval(timer)
  }, [streamConnected, refreshUnread])

  // 打开下拉时拉取完整列表
  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) fetchList()
  }

  const handleClick = (id: string, link?: string) => {
    markRead(id)
    setOpen(false)
    if (link) navigate(link)
  }

  const handleViewAll = () => {
    setOpen(false)
    navigate('/notifications')
  }

  const panel = (
    <div
      style={{
        width: 360,
        background: token.colorBgElevated,
        borderRadius: token.borderRadiusLG,
        boxShadow: token.boxShadowSecondary,
        overflow: 'hidden',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 16px', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Text strong>通知中心</Text>
        {unreadCount > 0 && (
          <Button type="link" size="small" icon={<CheckOutlined />} onClick={markAllRead}>
            全部已读
          </Button>
        )}
      </div>

      {loading && items.length === 0 ? (
        <div style={{ padding: '32px 0', textAlign: 'center' }}><Spin /></div>
      ) : items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无通知" style={{ padding: '32px 0' }} />
      ) : (
        <List
          style={{ maxHeight: 400, overflow: 'auto' }}
          dataSource={items.slice(0, 20)}
          renderItem={(n) => (
            <List.Item
              onClick={() => handleClick(n.id, n.link)}
              style={{
                padding: '10px 16px',
                cursor: 'pointer',
                background: n.isRead ? 'transparent' : token.colorPrimaryBg,
                transition: 'background 0.2s',
              }}
              actions={[
                !n.isRead && (
                  <Button
                    key="read"
                    type="link"
                    size="small"
                    icon={<CheckOutlined />}
                    onClick={(e) => { e.stopPropagation(); markRead(n.id) }}
                  >
                    已读
                  </Button>
                ),
              ].filter(Boolean)}
            >
              <List.Item.Meta
                avatar={TYPE_ICON[n.type]}
                title={
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                    <Space size={4}>
                      <Text style={{ fontSize: 13 }} ellipsis>{n.title}</Text>
                      {n.category && (
                        <Tag style={{ marginInlineEnd: 0, fontSize: 11 }}>
                          {CATEGORY_LABEL[n.category] ?? n.category}
                        </Tag>
                      )}
                    </Space>
                    <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{timeAgo(n.createdAt)}</Text>
                  </div>
                }
                description={n.content && <Text type="secondary" style={{ fontSize: 12 }} ellipsis>{n.content}</Text>}
              />
            </List.Item>
          )}
        />
      )}

      <div style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, textAlign: 'center' }}>
        <Button type="link" block onClick={handleViewAll}>查看全部</Button>
      </div>
    </div>
  )

  return (
    <Dropdown
      popupRender={() => panel}
      trigger={['click']}
      placement="bottomRight"
      open={open}
      onOpenChange={handleOpenChange}
    >
      <Badge count={unreadCount} size="small" offset={[-2, 2]}>
        <Button type="text" icon={<BellOutlined style={{ fontSize: 18 }} />} />
      </Badge>
    </Dropdown>
  )
}
