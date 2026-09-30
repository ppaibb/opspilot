/**
 * Pod 日志查看器（WebSocket 实时流）。
 *
 * 对接后端一次性 token 会话：POST logs/session 换 session_id+token，
 * 再连 ws_path?session_id&token 建立流式通道；帧格式 {type: log|system|error, message|data}。
 *
 * 功能对标 Rancher/KubeSphere 的 Pod 日志页：
 * - 容器切换（多容器 Pod）、起始行数（tail）、previous（崩溃前日志）
 * - 暂停/继续接收（暂停只停渲染不停传输，缓冲上限 5000 行防 OOM）
 * - 自动滚动开关（用户上翻时自动关闭，贴底时自动恢复）
 * - 关键字过滤（前端 contains 匹配）+ 清屏 + 下载当前缓冲为 .log 文件
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Drawer, Select, InputNumber, Switch, Button, Space, Typography, Tag, Tooltip, Input, App, Badge,
} from 'antd'
import {
  PauseCircleOutlined, PlayCircleOutlined, ClearOutlined, DownloadOutlined, VerticalAlignBottomOutlined,
} from '@ant-design/icons'
import client from '@/api/client'
import { extractAxiosDetail } from '@/hooks/usePermission'

const { Text } = Typography

interface PodLogViewerProps {
  open: boolean
  clusterId: string
  namespace: string
  podName: string
  /** Pod 详情返回的容器清单（含初始化容器则一并列出）。 */
  containers: string[]
  onClose: () => void
}

const MAX_BUFFER_LINES = 5000

export default function PodLogViewer({ open, clusterId, namespace, podName, containers, onClose }: PodLogViewerProps) {
  const { message } = App.useApp()
  const [container, setContainer] = useState<string>(containers[0] || '')
  const [tail, setTail] = useState(500)
  const [previous, setPrevious] = useState(false)
  const [paused, setPaused] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const [filter, setFilter] = useState('')
  const [lines, setLines] = useState<{ text: string; ts: number }[]>([])
  const [connected, setConnState] = useState<'idle' | 'connecting' | 'live' | 'closed' | 'error'>('idle')

  const wsRef = useRef<WebSocket | null>(null)
  const pausedRef = useRef(false)
  const bufferRef = useRef<{ text: string; ts: number }[]>([])
  const logBoxRef = useRef<HTMLDivElement>(null)
  const restartSeq = useRef(0)

  // 暂停标记用 ref 同步给 WS onmessage（避免闭包旧值）。
  useEffect(() => { pausedRef.current = paused }, [paused])

  /** 建立 WS 会话：每次 container/tail/previous 变化都重连。 */
  useEffect(() => {
    if (!open || !podName) return
    let cancelled = false
    const seq = ++restartSeq.current;
    // 新会话状态复位延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => {
      setLines([]);
      setConnState('connecting');
    });
    bufferRef.current = [];

    (async () => {
      try {
        const params = new URLSearchParams()
        if (container) params.set('container', container)
        params.set('tail', String(tail))
        params.set('previous', String(previous))
        const session = await client.post(
          `/clusters/${clusterId}/pods/${namespace}/${podName}/logs/session?${params.toString()}`,
        )
        if (cancelled || seq !== restartSeq.current) return

        const { session_id, session_token, ws_path } = session.data || {}
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const url = `${proto}//${window.location.host}${ws_path}?session_id=${encodeURIComponent(session_id)}&token=${encodeURIComponent(session_token)}`

        const ws = new WebSocket(url)
        wsRef.current = ws
        ws.onopen = () => { if (!cancelled) setConnState('live') }
        ws.onmessage = (event) => {
          if (cancelled || seq !== restartSeq.current) return
          try {
            const frame = JSON.parse(event.data)
            if (frame.type === 'log' && frame.data != null) {
              const line = { text: String(frame.data), ts: Date.now() }
              bufferRef.current = [...bufferRef.current.slice(-MAX_BUFFER_LINES + 1), line]
              if (!pausedRef.current) setLines(bufferRef.current)
            } else if (frame.type === 'error') {
              message.error(frame.message || '日志流错误')
              setConnState('error')
            }
          } catch { /* 非 JSON 帧忽略 */ }
        }
        ws.onerror = () => { if (!cancelled) setConnState('error') }
        ws.onclose = (event) => {
          if (cancelled || seq !== restartSeq.current) return
          setConnState((prev) => (prev === 'error' ? 'error' : 'closed'))
          if (event.code === 4401) message.error('日志会话已过期，请关闭后重新打开')
        }
      } catch (error: unknown) {
        if (!cancelled) {
          setConnState('error')
          message.error(extractAxiosDetail(error) ?? '创建日志会话失败')
        }
      }
    })()

    return () => {
      cancelled = true
      wsRef.current?.close()
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, clusterId, namespace, podName, container, tail, previous])

  // 自动滚动：仅当开关打开时贴底。
  useEffect(() => {
    if (autoScroll && logBoxRef.current) {
      logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight
    }
  }, [lines, autoScroll])

  const visibleLines = useMemo(
    () => (filter ? lines.filter((l) => l.text.toLowerCase().includes(filter.toLowerCase())) : lines),
    [lines, filter],
  )

  /** 用户手动上滚时关闭自动滚动；重新贴底时恢复。 */
  const handleScroll = () => {
    const box = logBoxRef.current
    if (!box) return
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40
    if (!atBottom && autoScroll) setAutoScroll(false)
  }

  const downloadLog = () => {
    const blob = new Blob([lines.map((l) => l.text).join('\n')], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${podName}${container ? `-${container}` : ''}.log`
    a.click()
    URL.revokeObjectURL(url)
  }

  const statusBadge = {
    idle: <Badge status="default" text="未连接" />,
    connecting: <Badge status="processing" text="连接中..." />,
    live: <Badge status="success" text="实时接收中" />,
    closed: <Badge status="warning" text="流已结束" />,
    error: <Badge status="error" text="连接错误" />,
  }[connected]

  return (
    <Drawer
      title={
        <Space size={8} wrap>
          <span>Pod 日志</span>
          <Text code style={{ fontSize: 12 }}>{namespace}/{podName}</Text>
          {statusBadge}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={860}
      destroyOnClose
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Space wrap size="small">
          {containers.length > 0 && (
            <Select
              value={container || containers[0]}
              onChange={setContainer}
              style={{ width: 200 }}
              options={containers.map((c) => ({ value: c, label: c }))}
              placeholder="容器"
            />
          )}
          <Tooltip title="起始回看行数">
            <Space size={4}>
              <Text type="secondary">回看</Text>
              <InputNumber min={50} max={1000} step={100} value={tail} onChange={(v) => setTail(v || 500)} />
              <Text type="secondary">行</Text>
            </Space>
          </Tooltip>
          <Tooltip title="查看崩溃重启前的日志（不跟随新输出）">
            <Space size={4}>
              <Text type="secondary">previous</Text>
              <Switch size="small" checked={previous} onChange={setPrevious} />
            </Space>
          </Tooltip>
          <Button
            size="small"
            icon={paused ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
            onClick={() => {
              setPaused(!paused)
              if (paused) setLines(bufferRef.current) // 恢复时把暂停期间的行一次性补上
            }}
          >
            {paused ? '继续' : '暂停'}
          </Button>
          <Tooltip title="清屏（不影响日志流接收）">
            <Button size="small" icon={<ClearOutlined />} onClick={() => { setLines([]); bufferRef.current = [] }} />
          </Tooltip>
          <Tooltip title="下载当前缓冲为 .log">
            <Button size="small" icon={<DownloadOutlined />} disabled={!lines.length} onClick={downloadLog} />
          </Tooltip>
        </Space>

        <Space wrap size="small" style={{ width: '100%', justifyContent: 'space-between' }}>
          <Input.Search
            placeholder="关键字过滤（大小写不敏感）"
            allowClear
            style={{ width: 280 }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <Space size={12}>
            <Tooltip title="滚动到最新行时自动跟随">
              <Space size={4}>
                <VerticalAlignBottomOutlined />
                <Text type="secondary">自动滚动</Text>
                <Switch size="small" checked={autoScroll} onChange={setAutoScroll} />
              </Space>
            </Tooltip>
            <Tag>{visibleLines.length}{filter ? `/${lines.length}` : ''} 行</Tag>
          </Space>
        </Space>

        <div
          ref={logBoxRef}
          onScroll={handleScroll}
          style={{
            background: '#0d1117',
            color: '#c9d1d9',
            fontFamily: 'ui-monospace, SFMono-Regular, monospace',
            fontSize: 12,
            lineHeight: '18px',
            borderRadius: 6,
            padding: '8px 12px',
            height: 'calc(100vh - 240px)',
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {visibleLines.length === 0 ? (
            <div style={{ color: '#8b949e', padding: 16 }}>
              {connected === 'live' ? '等待日志输出...' : connected === 'connecting' ? '正在建立日志流...' : '暂无日志（可调整回看行数或检查容器状态）'}
            </div>
          ) : (
            visibleLines.map((line, idx) => (
              <div key={idx}>{line.text}</div>
            ))
          )}
        </div>
      </Space>
    </Drawer>
  )
}
