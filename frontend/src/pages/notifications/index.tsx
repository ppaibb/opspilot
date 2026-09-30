/**
 * 通知中心页（/notifications）—— 站内信收件箱。
 *
 * 体验设计：
 * 1. 筛选三连：已读状态 Segmented + 业务分类下拉 + 标题/内容关键词搜索（后端筛选，分页基数正确）；
 * 2. 时间分组：今天/昨天/更早 分组头，长列表不用逐条看日期；
 * 3. 未读强标识：左侧蓝色竖条 + 浅蓝底 + 标题加粗，一眼定位未处理事项；
 * 4. 有跳转链接的通知在描述行给出「查看详情 →」提示，点击整行进业务页。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App, Badge, Button, Card, Empty, Flex, Input, List, Pagination, Segmented, Select, Space, Spin, Tag, Typography,
} from 'antd'
import {
  CheckOutlined, DeleteOutlined, CheckCircleFilled, CloseCircleFilled,
  ExclamationCircleFilled, InfoCircleFilled, ReloadOutlined, ClearOutlined, SearchOutlined,
  RightOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { notificationsApi } from '@/api'
import type { NotificationApiItem } from '@/types/api'
import { useNotificationStore } from '@/store/notifications'

const { Text, Title } = Typography

const PAGE_SIZE = 15

const TYPE_ICON: Record<string, React.ReactNode> = {
  success: <CheckCircleFilled style={{ color: '#52c41a', fontSize: 18 }} />,
  error:   <CloseCircleFilled style={{ color: '#ff4d4f', fontSize: 18 }} />,
  warning: <ExclamationCircleFilled style={{ color: '#faad14', fontSize: 18 }} />,
  info:    <InfoCircleFilled style={{ color: '#1677ff', fontSize: 18 }} />,
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

const CATEGORY_OPTIONS = Object.entries(CATEGORY_LABEL).map(([value, label]) => ({ value, label }))

/** 分类标签着色：与业务模块的语义色对齐，未知分类回落到灰色。 */
const CATEGORY_COLOR: Record<string, string> = {
  certificate: 'green',
  host: 'blue',
  vm: 'geekblue',
  agent: 'cyan',
  db: 'gold',
  monitoring: 'red',
  asset: 'purple',
  application: 'magenta',
  system: 'default',
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 归组键：今天 / 昨天 / 更早（按本地日期比较）。 */
function dateGroup(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '更早'
  const today = new Date()
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  const startOfYesterday = startOfToday - 86400000
  const t = d.getTime()
  if (t >= startOfToday) return '今天'
  if (t >= startOfYesterday) return '昨天'
  return '更早'
}

const GROUP_ORDER = ['今天', '昨天', '更早']

type FilterKey = 'all' | 'unread' | 'read'

export default function NotificationsPage() {
  const navigate = useNavigate()
  const { message, modal } = App.useApp()

  const [items, setItems] = useState<NotificationApiItem[]>([])
  const [total, setTotal] = useState(0)
  const [unreadCount, setUnreadCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<FilterKey>('all')
  const [category, setCategory] = useState('')
  const [keyword, setKeyword] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [page, setPage] = useState(1)

  // 同步顶栏铃铛的未读数
  const setBellUnread = useNotificationStore((s) => s.refreshUnread)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params: { is_read?: boolean; category?: string; q?: string; limit: number; offset: number } = {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }
      if (filter === 'unread') params.is_read = false
      if (filter === 'read') params.is_read = true
      if (category) params.category = category
      if (keyword.trim()) params.q = keyword.trim()
      const res = await notificationsApi.list(params)
      setItems(res.items)
      setTotal(res.total)
      setUnreadCount(res.unread_count)
    } catch {
      message.error('加载通知失败')
    } finally {
      setLoading(false)
    }
  }, [filter, page, category, keyword, message])

  useEffect(() => {
    // 首次加载延后到微任务（load 内同步置 loading），避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => { void load() })
  }, [load])

  // 关键词防抖：输入停顿 300ms 再请求，避免每敲一个字都打接口
  useEffect(() => {
    if (keywordInput === keyword) return
    const timer = setTimeout(() => { setKeyword(keywordInput); setPage(1) }, 300)
    return () => clearTimeout(timer)
  }, [keywordInput, keyword])

  // SSE 推送/铃铛操作后，本页数据可能过期；监听 store 列表版本变化做轻量刷新
  const storeUnread = useNotificationStore((s) => s.unreadCount)
  useEffect(() => {
    // 同步延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    if (storeUnread !== unreadCount) {
      queueMicrotask(() => setUnreadCount(storeUnread))
    }
    // 只在铃铛未读数变化时同步展示，不触发重新请求（避免与 SSE 循环刷新）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeUnread])

  const handleFilterChange = (val: FilterKey) => {
    setFilter(val)
    setPage(1)
  }

  const handleClick = async (n: NotificationApiItem) => {
    if (!n.is_read) {
      try {
        await notificationsApi.markRead(n.id)
        setBellUnread()
      } catch {
        /* 忽略，跳转优先 */
      }
    }
    if (n.link) navigate(n.link)
    else load()
  }

  const handleMarkRead = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    try {
      await notificationsApi.markRead(id)
      await load()
      setBellUnread()
    } catch {
      message.error('操作失败')
    }
  }

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    try {
      await notificationsApi.remove(id)
      await load()
      setBellUnread()
      message.success('已删除')
    } catch {
      message.error('删除失败')
    }
  }

  const handleMarkAllRead = async () => {
    try {
      await notificationsApi.markAllRead()
      await load()
      setBellUnread()
      message.success('已全部标记为已读')
    } catch {
      message.error('操作失败')
    }
  }

  const handleClear = () => {
    modal.confirm({
      title: '清空全部通知',
      content: '此操作将删除你的所有通知，且不可恢复。确定继续吗？',
      okText: '清空',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await notificationsApi.clear()
          setPage(1)
          await load()
          setBellUnread()
          message.success('已清空全部通知')
        } catch {
          message.error('清空失败')
        }
      },
    })
  }

  const filterOptions = useMemo(
    () => [
      { label: '全部', value: 'all' as const },
      {
        label: (
          <Space size={4}>
            未读
            {unreadCount > 0 && <Badge count={unreadCount} size="small" />}
          </Space>
        ),
        value: 'unread' as const,
      },
      { label: '已读', value: 'read' as const },
    ],
    [unreadCount],
  )

  // 按日期分组（保持后端 created_at desc 的顺序）
  const groups = useMemo(() => {
    const map = new Map<string, NotificationApiItem[]>()
    for (const n of items) {
      const key = dateGroup(n.created_at)
      const arr = map.get(key)
      if (arr) arr.push(n)
      else map.set(key, [n])
    }
    return GROUP_ORDER.filter((g) => map.has(g)).map((g) => ({ group: g, list: map.get(g)! }))
  }, [items])

  const renderItem = (n: NotificationApiItem) => (
    <List.Item
      onClick={() => handleClick(n)}
      style={{
        padding: '14px 24px 14px 20px',
        cursor: 'pointer',
        background: n.is_read ? 'transparent' : 'rgba(22,119,255,0.04)',
        borderInlineStart: n.is_read ? '3px solid transparent' : '3px solid #1677ff',
        transition: 'background 0.2s',
      }}
      actions={[
        ...(!n.is_read
          ? [
              <Button
                key="read"
                type="link"
                size="small"
                icon={<CheckOutlined />}
                onClick={(e) => handleMarkRead(e, n.id)}
              >
                标记已读
              </Button>,
            ]
          : []),
        <Button
          key="del"
          type="link"
          size="small"
          danger
          icon={<DeleteOutlined />}
          onClick={(e) => handleDelete(e, n.id)}
        >
          删除
        </Button>,
      ]}
    >
      <List.Item.Meta
        avatar={TYPE_ICON[n.type] ?? TYPE_ICON.info}
        title={
          <Space size={6} wrap>
            <Text strong={!n.is_read}>{n.title}</Text>
            {n.category && (
              <Tag color={CATEGORY_COLOR[n.category] ?? 'default'} style={{ marginInlineEnd: 0 }}>
                {CATEGORY_LABEL[n.category] ?? n.category}
              </Tag>
            )}
            {!n.is_read && <Badge status="processing" />}
          </Space>
        }
        description={
          <div>
            {n.content && <div style={{ marginBottom: 4 }}>{n.content}</div>}
            <Space size={8} split={n.link ? <Text type="secondary">·</Text> : null}>
              <Text type="secondary" style={{ fontSize: 12 }}>{formatTime(n.created_at)}</Text>
              {n.link && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  查看详情 <RightOutlined style={{ fontSize: 10 }} />
                </Text>
              )}
            </Space>
          </div>
        }
      />
    </List.Item>
  )

  const hasFilter = filter !== 'all' || !!category || !!keyword.trim()

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>通知中心</Title>
          <Text type="secondary">告警、工单审批、证书与资产变更都会在这里通知你</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          <Button icon={<CheckOutlined />} onClick={handleMarkAllRead} disabled={unreadCount === 0}>
            全部已读
          </Button>
          <Button danger icon={<ClearOutlined />} onClick={handleClear} disabled={total === 0}>
            清空
          </Button>
        </Space>
      </div>

      <Card size="small" style={{ marginBottom: 12 }}>
        <Flex justify="space-between" wrap gap="small">
          <Space wrap>
            <Segmented options={filterOptions} value={filter} onChange={(v) => handleFilterChange(v as FilterKey)} />
            <Select
              value={category || undefined}
              onChange={(v) => { setCategory(v || ''); setPage(1) }}
              style={{ width: 130 }}
              allowClear
              placeholder="全部类型"
              options={CATEGORY_OPTIONS}
            />
          </Space>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索标题 / 内容"
            style={{ width: 240 }}
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            onClear={() => { setKeywordInput(''); setKeyword(''); setPage(1) }}
          />
        </Flex>
      </Card>

      <Card styles={{ body: { padding: 0 } }}>
        {loading ? (
          <div style={{ padding: '64px 0', textAlign: 'center' }}><Spin /></div>
        ) : items.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={hasFilter ? '没有符合条件的通知' : '暂无通知'}
            style={{ padding: '64px 0' }}
          >
            {hasFilter && (
              <Button type="link" onClick={() => { setFilter('all'); setCategory(''); setKeyword(''); setKeywordInput('') }}>
                清除筛选条件
              </Button>
            )}
          </Empty>
        ) : (
          <List
            dataSource={groups}
            renderItem={({ group, list }) => (
              <div key={group}>
                <div
                  style={{
                    position: 'sticky', top: 0, zIndex: 2,
                    padding: '6px 24px',
                    background: 'rgba(255,255,255,0.92)',
                    backdropFilter: 'blur(4px)',
                    borderBottom: '1px solid rgba(5,5,5,0.06)',
                  }}
                >
                  <Text type="secondary" style={{ fontSize: 12 }}>{group}</Text>
                </div>
                {list.map((n) => (
                  <div key={n.id}>{renderItem(n)}</div>
                ))}
              </div>
            )}
          />
        )}

        {total > PAGE_SIZE && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', padding: 16 }}>
            <Pagination
              current={page}
              pageSize={PAGE_SIZE}
              total={total}
              showSizeChanger={false}
              onChange={setPage}
            />
          </div>
        )}
      </Card>
    </div>
  )
}
