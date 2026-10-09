/**
 * 集群实时动态（SSE Watch 前端消费，对标 kubectl get -w 的 UI 化）。
 *
 * 协议（后端 k8s_watch.py + SharedInformer）：
 *   1. POST /clusters/{cid}/watch/session 换一次性 token（90 秒、单次有效）；
 *   2. GET /clusters/{cid}/watch?token&kinds&namespace 为 SSE 流：
 *      - event: snapshot  Informer 缓存初始快照（只展示统计，不刷屏）
 *      - event: watch     增量事件 {type: ADDED/MODIFIED/DELETED, kind, namespace, name, ...}
 *      - event: ping      30 秒心跳
 *
 * 连接策略：fetch 流式读取（与日志 Live Tail 同模式），**token 一次性意味着
 * EventSource 的自动重连必然 401**，因此在组件内做手动重连——流断开后换新
 * token 重连，指数上限 5 次防风暴；关闭抽屉即中止。
 *
 * 交互：暂停/继续（暂停只停渲染不停接收）、kind/namespace 过滤（变更即重连）、
 * 关键字过滤、事件计数统计、环形缓冲 500 条；每行带 Informer 摘要出的当前状态
 * （Pod：状态/就绪/重启；负载：状态/就绪副本），自动重连耗尽后可手动「重新连接」。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Drawer, Select, Button, Space, Typography, Tag, App, Badge, Input, Tooltip, Statistic, Flex,
} from 'antd'
import {
  PauseCircleOutlined, PlayCircleOutlined, ClearOutlined, PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { clusterOpsApi } from '@/api'
import { authenticatedFetch } from '@/lib/fetch'

const { Text } = Typography

/** 后端 WATCHED_KINDS 的前端子集（14 种全部支持，UI 分两组便于筛选）。 */
const ALL_KINDS = [
  'Pod', 'Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet', 'Job', 'CronJob',
  'Service', 'Ingress', 'ConfigMap', 'Secret', 'PVC', 'ServiceAccount', 'Event',
]
/** 默认订阅：高频且业务关心的 kinds（Event 量大默认不选，可手动开启）。 */
const DEFAULT_KINDS = ['Pod', 'Deployment', 'StatefulSet', 'DaemonSet', 'Service', 'Ingress']

const MAX_EVENTS = 500

interface WatchEvent {
  type: 'ADDED' | 'MODIFIED' | 'DELETED' | string
  kind: string
  namespace: string
  name: string
  resourceVersion?: string
  /** SSE 帧里的 object_summary（Pod: status/ready/restarts；负载: status/ready）。 */
  object?: { status?: string; ready?: string; restarts?: number }
  ts: number
}

const EVENT_META: Record<string, { color: string; icon: ReactNode; label: string }> = {
  ADDED: { color: 'green', icon: <PlusOutlined />, label: '新增' },
  MODIFIED: { color: 'blue', icon: <EditOutlined />, label: '变更' },
  DELETED: { color: 'red', icon: <DeleteOutlined />, label: '删除' },
}

const STATUS_COLOR: Record<string, string> = {
  Running: '#52c41a', Active: '#52c41a', Complete: '#52c41a', Succeeded: '#52c41a',
  Pending: '#faad14', ContainerCreating: '#faad14', Starting: '#faad14', Terminating: '#faad14',
  Failed: '#f5222d', Error: '#f5222d', CrashLoopBackOff: '#f5222d', Degraded: '#f5222d', Evicted: '#f5222d',
  Suspended: '#8c8c8c',
}

/** 事件行状态摘要：Pod 给「状态 · 就绪 · 重启」，负载给「状态 · 就绪」。 */
function eventSummary(evt: WatchEvent): { text: string; color: string } | null {
  const o = evt.object
  if (!o) return null
  const color = STATUS_COLOR[o.status || ''] || '#8c8c8c'
  if (evt.kind === 'Pod') {
    const parts = [o.status || '未知', o.ready || '?']
    if (o.restarts != null) parts.push(`重启 ${o.restarts}`)
    return { text: parts.join(' · '), color }
  }
  if (evt.kind === 'Event') return null
  const parts = [o.status || '未知']
  if (o.ready) parts.push(o.ready)
  return { text: parts.join(' · '), color }
}

interface ClusterLiveFeedProps {
  open: boolean
  clusterId: string
  /** 复用页面级 namespace 清单，避免重复请求。 */
  namespaces: string[]
  onClose: () => void
}

export default function ClusterLiveFeed({ open, clusterId, namespaces, onClose }: ClusterLiveFeedProps) {
  const { message } = App.useApp()
  const [kinds, setKinds] = useState<string[]>(DEFAULT_KINDS)
  const [namespace, setNamespace] = useState('')
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState('')
  const [events, setEvents] = useState<WatchEvent[]>([])
  const [status, setStatus] = useState<'idle' | 'connecting' | 'live' | 'error'>('idle')
  const [snapshotCount, setSnapshotCount] = useState<number | null>(null)
  // 手动重连：自增即重启主循环（换新 token，指数退避计数归零）。
  const [reconnectKey, setReconnectKey] = useState(0)

  const pausedRef = useRef(false)
  const bufferRef = useRef<WatchEvent[]>([])
  const abortRef = useRef<AbortController | null>(null)
  const enabledRef = useRef(false)
  const restartSeq = useRef(0)

  useEffect(() => { pausedRef.current = paused }, [paused])

  const counts = useMemo(() => {
    const result = { ADDED: 0, MODIFIED: 0, DELETED: 0 }
    for (const e of events) {
      if (e.type in result) result[e.type as keyof typeof result] += 1
    }
    return result
  }, [events])

  /** 主循环：换 token → 建立 SSE → 消费帧；断开后自动重连（组件卸载/条件变更时退出）。 */
  useEffect(() => {
    if (!open || kinds.length === 0) {
      // 状态复位延后到微任务，避免在 effect 内同步 setState 触发级联渲染
      queueMicrotask(() => setStatus('idle'))
      return
    }
    enabledRef.current = true
    let cancelled = false
    const seq = ++restartSeq.current

    const run = async () => {
      let attempts = 0
      while (!cancelled && enabledRef.current && seq === restartSeq.current) {
        setStatus('connecting')
        const controller = new AbortController()
        abortRef.current = controller
        try {
          const { token } = await clusterOpsApi.createWatchSession(clusterId)
          if (cancelled || seq !== restartSeq.current) return
          const params = new URLSearchParams({ token, kinds: kinds.join(',') })
          if (namespace) params.set('namespace', namespace)
          const resp = await authenticatedFetch(`/api/clusters/${clusterId}/watch?${params.toString()}`, {
            signal: controller.signal,
          })
          if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
          setStatus('live')
          attempts = 0

          const reader = resp.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          // SSE 以空行分帧；event:/data: 两行一组。
          for (;;) {
            const { done, value } = await reader.read()
            if (done || cancelled || seq !== restartSeq.current) return
            buf += decoder.decode(value, { stream: true })
            const chunks = buf.split('\n\n')
            buf = chunks.pop() || ''
            for (const chunk of chunks) {
              const eventName = chunk.split('\n').find((l) => l.startsWith('event:'))?.slice(6).trim()
              const dataLine = chunk.split('\n').find((l) => l.startsWith('data:'))?.slice(5).trim()
              if (!dataLine) continue
              try {
                const data = JSON.parse(dataLine)
                if (eventName === 'snapshot') {
                  // 快照只取资源总数作为"已同步"提示，避免初次连接刷屏。
                  const size = Array.isArray(data) ? data.length : Object.keys(data).length
                  setSnapshotCount(size)
                } else if (eventName === 'watch' && data.name) {
                  const evt: WatchEvent = { ...data, ts: Date.now() }
                  bufferRef.current = [...bufferRef.current.slice(-MAX_EVENTS + 1), evt]
                  if (!pausedRef.current) setEvents(bufferRef.current)
                }
                // ping 帧仅保活，无需处理。
              } catch { /* 非 JSON 帧忽略 */ }
            }
          }
        } catch (error: unknown) {
          // fetch 中止抛出 AbortError，属正常退出而非连接故障
          if (cancelled) return
          if (error instanceof Error && error.name === 'AbortError') return
        }
        // 断流重连：token 一次性，必须换新 token；连续失败退避。
        attempts += 1
        if (attempts <= 5 && enabledRef.current) {
          setStatus('connecting')
          await new Promise((r) => setTimeout(r, Math.min(1000 * attempts, 5000)))
        } else {
          setStatus('error')
          message.error('实时连接多次失败，已停止自动重连（可点「重新连接」重试）')
          return
        }
      }
    }

    run()
    return () => {
      cancelled = true
      enabledRef.current = false
      abortRef.current?.abort()
      abortRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, clusterId, kinds, namespace, reconnectKey])

  // 关闭抽屉清空状态，避免下次打开残留旧会话数据。
  // （复位延后到微任务，避免在 effect 内同步 setState 触发级联渲染）
  useEffect(() => {
    if (!open) {
      queueMicrotask(() => {
        setEvents([])
        bufferRef.current = []
        setSnapshotCount(null)
        setPaused(false)
      })
    }
  }, [open])

  const visible = useMemo(
    () => (filter
      ? events.filter((e) => `${e.namespace}/${e.name}`.toLowerCase().includes(filter.toLowerCase()) || e.kind.toLowerCase().includes(filter.toLowerCase()))
      : events).slice().reverse(), // 最新在上
    [events, filter],
  )

  return (
    <Drawer
      title={
        <Space size={8} wrap>
          <span>集群实时动态</span>
          {status === 'live' && <Badge status="success" text="实时接收中" />}
          {status === 'connecting' && <Badge status="processing" text="连接中..." />}
          {status === 'error' && <Badge status="error" text="连接失败" />}
          {status === 'idle' && <Badge status="default" text="未连接" />}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={760}
      destroyOnClose
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Space wrap size="small">
          <Select
            mode="multiple"
            value={kinds}
            onChange={setKinds}
            style={{ minWidth: 300 }}
            maxTagCount={4}
            placeholder="订阅资源类型"
            options={ALL_KINDS.map((k) => ({ value: k, label: k }))}
          />
          <Select
            value={namespace}
            onChange={setNamespace}
            style={{ width: 160 }}
            showSearch
            options={[
              { value: '', label: '全部 Namespace' },
              ...namespaces.map((ns) => ({ value: ns, label: ns })),
            ]}
          />
          {paused ? (
            <Button size="small" type="primary" icon={<PlayCircleOutlined />}
              onClick={() => { setPaused(false); setEvents(bufferRef.current) }}>
              继续
            </Button>
          ) : (
            <Tooltip title="暂停只停渲染，接收不中断">
              <Button size="small" icon={<PauseCircleOutlined />} onClick={() => setPaused(true)}>暂停</Button>
            </Tooltip>
          )}
          {status === 'error' && (
            <Tooltip title="重新尝试连接（自动换取新的一次性 token）">
              <Button size="small" type="primary" icon={<ReloadOutlined />}
                onClick={() => setReconnectKey((k) => k + 1)}>
                重新连接
              </Button>
            </Tooltip>
          )}
          <Tooltip title="清屏（不影响接收）">
            <Button size="small" icon={<ClearOutlined />} onClick={() => { setEvents([]); bufferRef.current = [] }} />
          </Tooltip>
        </Space>

        <Space wrap size="small" style={{ width: '100%', justifyContent: 'space-between' }}>
          <Input.Search
            placeholder="按名称/类型过滤"
            allowClear
            style={{ width: 240 }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <Space size={16}>
            <Statistic title="新增" value={counts.ADDED} valueStyle={{ fontSize: 16, color: '#52c41a' }} />
            <Statistic title="变更" value={counts.MODIFIED} valueStyle={{ fontSize: 16, color: '#1677ff' }} />
            <Statistic title="删除" value={counts.DELETED} valueStyle={{ fontSize: 16, color: '#cf1322' }} />
          </Space>
        </Space>

        <Text type="secondary" style={{ fontSize: 12 }}>
          {snapshotCount != null
            ? `Informer 快照已同步 ${snapshotCount} 个资源，以下为增量事件（最新在上，保留 ${MAX_EVENTS} 条）`
            : '等待 Informer 快照同步...'}
          {paused && ' · 已暂停渲染（接收仍在进行）'}
        </Text>

        <div style={{ maxHeight: 'calc(100vh - 320px)', overflow: 'auto' }}>
          {visible.length === 0 ? (
            status === 'error' ? (
              <Flex vertical align="center" gap={8} style={{ padding: '48px 0', color: '#8c8c8c' }}>
                <span>连接已断开（退避重连 5 次仍未恢复）</span>
                <Button icon={<ReloadOutlined />} onClick={() => setReconnectKey((k) => k + 1)}>重新连接</Button>
              </Flex>
            ) : (
              <div style={{ textAlign: 'center', padding: '48px 0', color: '#8c8c8c' }}>
                {status === 'live' ? '暂无变更事件——集群平静是好事' : '等待连接...'}
              </div>
            )
          ) : (
            visible.map((evt, idx) => {
              const meta = EVENT_META[evt.type] || { color: 'default', icon: <ReloadOutlined />, label: evt.type }
              const summary = eventSummary(evt)
              return (
                <Flex key={`${evt.ts}-${idx}`} gap={8} align="center" style={{ padding: '4px 4px', borderBottom: '1px solid #f5f5f5' }}>
                  <Tag color={meta.color} style={{ flexShrink: 0, margin: 0, minWidth: 52, textAlign: 'center' }}>
                    {meta.icon} {meta.label}
                  </Tag>
                  <Tag style={{ flexShrink: 0, margin: 0 }}>{evt.kind}</Tag>
                  <Text strong style={{ fontSize: 12, flex: 1, minWidth: 0 }} ellipsis>
                    {evt.namespace ? `${evt.namespace}/` : ''}{evt.name}
                  </Text>
                  {summary && (
                    <Tooltip title={`${evt.kind} 当前状态（来自 Informer 摘要）`}>
                      <Text style={{ fontSize: 11, flexShrink: 0, color: summary.color, maxWidth: 180 }} ellipsis>
                        {summary.text}
                      </Text>
                    </Tooltip>
                  )}
                  <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                    {new Date(evt.ts).toLocaleTimeString('zh-CN', { hour12: false })}
                  </Text>
                </Flex>
              )
            })
          )}
        </div>
      </Space>
    </Drawer>
  )
}
