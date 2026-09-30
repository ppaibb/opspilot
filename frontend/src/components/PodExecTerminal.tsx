/**
 * Pod Exec 终端（WebSocket 交互式 shell）。
 *
 * 与 PodLogViewer 同一套一次性 token 会话；WS 双向帧协议：
 *   上行 {type: "input",  data} → 容器 stdin
 *   下行 {type: "output", data} ← stdout/stderr
 *   下行 {type: "system"|"error", message}
 *
 * 交互模型：行式终端（不引入 xterm 依赖）——
 * - 输出区滚动展示，命令提示符后回车提交一行并回显；
 * - 支持上下方向键翻历史（↑/↓）、Ctrl+C 发送中断；
 * - 服务端空闲 5 分钟自动断开（与后端 IDLE_TIMEOUT 对齐）。
 */
import { useEffect, useRef, useState } from 'react'
import { Drawer, Select, Space, Typography, App, Badge, Tooltip, Button } from 'antd'
import { ClearOutlined } from '@ant-design/icons'
import { clusterOpsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'

const { Text } = Typography

interface PodExecTerminalProps {
  open: boolean
  clusterId: string
  namespace: string
  podName: string
  /** 容器清单（来自 Pod 详情）。 */
  containers: string[]
  onClose: () => void
}

const MAX_OUTPUT_CHARS = 200_000

export default function PodExecTerminal({ open, clusterId, namespace, podName, containers, onClose }: PodExecTerminalProps) {
  const { message } = App.useApp()
  const [container, setContainer] = useState<string>(containers[0] || '')
  const [input, setInput] = useState('')
  const [output, setOutput] = useState<string[]>([])
  const [connected, setConnected] = useState<'idle' | 'connecting' | 'live' | 'closed' | 'error'>('idle')
  const [history, setHistory] = useState<string[]>([])
  const [historyIdx, setHistoryIdx] = useState(-1)

  const wsRef = useRef<WebSocket | null>(null)
  const outRef = useRef<HTMLDivElement>(null)
  const restartSeq = useRef(0)

  const appendOutput = (text: string) => {
    setOutput((prev) => {
      const next = [...prev, text]
      // 单帧过长截断 + 总量环形裁剪，防止长任务刷爆 DOM。
      return next.join('\n').slice(-MAX_OUTPUT_CHARS).split('\n')
    })
  }

  useEffect(() => {
    if (!open || !podName) return
    let cancelled = false
    const seq = ++restartSeq.current
    // 新会话状态复位延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => {
      setOutput([])
      setConnected('connecting')
    })

    ;(async () => {
      try {
        const params = new URLSearchParams()
        if (container) params.set('container', container)
        params.set('command', '/bin/sh')
        const session = await clusterOpsApi.createExecSession(clusterId, namespace, podName, {
          container: container || undefined,
        })
        if (cancelled || seq !== restartSeq.current) return

        const { session_id, session_token, ws_path } = session
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const ws = new WebSocket(
          `${proto}//${window.location.host}${ws_path}?session_id=${encodeURIComponent(session_id)}&token=${encodeURIComponent(session_token)}`,
        )
        wsRef.current = ws
        ws.onopen = () => { if (!cancelled) setConnected('live') }
        ws.onmessage = (event) => {
          if (cancelled || seq !== restartSeq.current) return
          try {
            const frame = JSON.parse(event.data)
            if (frame.type === 'output' && frame.data != null) {
              appendOutput(String(frame.data))
            } else if (frame.type === 'system') {
              appendOutput(`【系统】${frame.message}`)
              if (String(frame.message).includes('disconnecting') || String(frame.message).includes('closed')) {
                setConnected('closed')
              }
            } else if (frame.type === 'error') {
              message.error(frame.message || '终端错误')
              setConnected('error')
            }
          } catch { /* 忽略非 JSON 帧 */ }
        }
        ws.onerror = () => { if (!cancelled) setConnected('error') }
        ws.onclose = (event) => {
          if (cancelled || seq !== restartSeq.current) return
          setConnected((prev) => (prev === 'error' ? 'error' : 'closed'))
          if (event.code === 4401) message.error('终端会话已过期，请关闭后重新打开')
        }
      } catch (error: unknown) {
        if (!cancelled) {
          setConnected('error')
          message.error(extractAxiosDetail(error) ?? '创建终端会话失败（需集群管理权限）')
        }
      }
    })()

    return () => {
      cancelled = true
      wsRef.current?.close()
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, clusterId, namespace, podName, container])

  // 输出变化时滚到底部（终端语义：总是跟随）。
  useEffect(() => {
    if (outRef.current) outRef.current.scrollTop = outRef.current.scrollHeight
  }, [output])

  const sendLine = () => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'input', data: `${input}\n` }))
    if (input.trim()) {
      setHistory((prev) => [...prev.slice(-50), input])
      setHistoryIdx(-1)
    }
    setInput('')
  }

  const handleKeydown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      sendLine()
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (!history.length) return
      const next = historyIdx < 0 ? history.length - 1 : Math.max(0, historyIdx - 1)
      setHistoryIdx(next)
      setInput(history[next])
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (historyIdx < 0) return
      const next = historyIdx + 1
      if (next >= history.length) { setHistoryIdx(-1); setInput('') }
      else { setHistoryIdx(next); setInput(history[next]) }
    } else if (event.key === 'c' && event.ctrlKey) {
      // Ctrl+C：发送中断（不本地拦截，直接透传）。
      event.preventDefault()
      wsRef.current?.send(JSON.stringify({ type: 'input', data: '\x03' }))
      setInput('')
    }
  }

  const statusBadge = {
    idle: <Badge status="default" text="未连接" />,
    connecting: <Badge status="processing" text="连接中..." />,
    live: <Badge status="success" text="已连接" />,
    closed: <Badge status="warning" text="已断开" />,
    error: <Badge status="error" text="连接错误" />,
  }[connected]

  return (
    <Drawer
      title={
        <Space size={8} wrap>
          <span>Pod 终端</span>
          <Text code style={{ fontSize: 12 }}>{namespace}/{podName}</Text>
          {statusBadge}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={820}
      destroyOnClose
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        {containers.length > 0 && (
          <Space wrap>
            <Select
              value={container || containers[0]}
              onChange={setContainer}
              style={{ width: 200 }}
              options={containers.map((c) => ({ value: c, label: c }))}
              disabled={connected === 'live'}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>连接后切换容器需重开终端</Text>
            <Tooltip title="清屏">
              <Button size="small" icon={<ClearOutlined />} onClick={() => setOutput([])} />
            </Tooltip>
          </Space>
        )}

        <div
          ref={outRef}
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
          {output.length === 0 ? (
            <div style={{ color: '#8b949e', padding: 16 }}>
              {connected === 'live' ? '终端已就绪，输入命令回车执行（Ctrl+C 中断，↑↓ 翻历史）' : '正在建立终端连接...'}
            </div>
          ) : (
            output.map((line, idx) => (
              <div key={idx} style={{ whiteSpace: 'pre-wrap' }}>{line}</div>
            ))
          )}
        </div>

        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeydown}
          disabled={connected !== 'live'}
          placeholder={connected === 'live' ? '$ 输入命令，回车执行' : '等待终端连接...'}
          spellCheck={false}
          style={{
            width: '100%',
            background: '#161b22',
            border: '1px solid #30363d',
            color: '#c9d1d9',
            borderRadius: 6,
            padding: '8px 12px',
            fontFamily: 'ui-monospace, SFMono-Regular, monospace',
            fontSize: 12,
            outline: 'none',
          }}
        />
      </Space>
    </Drawer>
  )
}
