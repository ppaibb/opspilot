/**
 * OpsPilot 居中悬浮 macOS 风格 WebSSH 终端 Modal (旗舰增强版)
 * 包含：
 * 1. 🤖 AI 运维命令助手：自然语言一键生成 Linux 命令并实时注入终端。
 * 2. ⚡ 常用快捷命令：一键注入 uname, free -h, df -h, docker ps 等常用运维指令。
 * 3. 🎨 终端主题切换：支持 Slate Dark, Matrix Green, Cyberpunk, Solarized 4 款尊享配色。
 * 4. 📁 SFTP 文件管理器：全功能目录导航、新建目录、文件上传/下载、Popconfirm 安全删除。
 * 5. 🧹 一键清屏功能。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Modal, Tooltip, Button, Input, Spin, Empty, Popconfirm, Dropdown, message as antdMessage } from 'antd'
import {
  CloseOutlined, MinusOutlined, FullscreenOutlined, FullscreenExitOutlined,
  FolderOpenOutlined, CompassOutlined, HomeOutlined, ArrowLeftOutlined, DownloadOutlined,
  UploadOutlined, FolderAddOutlined, DeleteOutlined, ReloadOutlined, FileTextOutlined,
  RobotOutlined, ThunderboltOutlined, SkinOutlined, ClearOutlined, SendOutlined
} from '@ant-design/icons'
import { Terminal, useTerminal } from '@wterm/react'
import { terminalApi, aiApi } from '@/api'

const NORMAL_COLS = 120
const NORMAL_ROWS = 28

const MAX_COLS = 145
const MAX_ROWS = 43

const SFTP_DEFAULT_PATH = '/root'
const UPLOAD_LIMIT_BYTES = 100 * 1024 * 1024 // 100MB

// 🎨 主题配色矩阵
const THEMES = {
  slate: { key: 'slate', bg: '#020617', toolbar: '#0f172a', border: '#1e293b', txt: '#f8fafc', txtMuted: '#64748b', name: '夜空深蓝 🌙' },
  matrix: { key: 'matrix', bg: '#05190e', toolbar: '#062c19', border: '#0b522c', txt: '#4ade80', txtMuted: '#16a34a', name: '极客矩阵 🟢' },
  cyberpunk: { key: 'cyberpunk', bg: '#110c1d', toolbar: '#1d1333', border: '#3b2566', txt: '#f472b6', txtMuted: '#a855f7', name: '赛博朋克 🟣' },
  solarized: { key: 'solarized', bg: '#fdf6e3', toolbar: '#eee8d5', border: '#d3c6aa', txt: '#073642', txtMuted: '#657b83', name: '日光浅色 ☀️' },
}

type ThemeKey = keyof typeof THEMES

interface Props {
  open: boolean
  assetId: string
  assetName?: string
  assetIp?: string
  onClose: () => void
}

type ConnState = 'idle' | 'connecting' | 'connected' | 'error' | 'closed'

interface FileItem {
  name: string; path: string; size: number
  is_dir: boolean; mode: string; modified_at: string; is_symlink: boolean
}

// ⚡ 常用运维快捷指令预设
const QUICK_COMMANDS = [
  { label: '系统内核信息', cmd: 'uname -a' },
  { label: '运行时间与负载', cmd: 'uptime' },
  { label: '内存使用情况', cmd: 'free -h' },
  { label: '磁盘空间容量', cmd: 'df -h /' },
  { label: '当前登录用户', cmd: 'whoami' },
  { label: 'Docker 运行容器', cmd: 'docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"' },
  { label: '网络端口监听', cmd: 'ss -tuln' },
  { label: '系统资源占用 top', cmd: 'top -b -n 1 | head -n 20' },
]

// 🤖 AI 命令生成智库预设
const AI_PRESETS = [
  { prompt: '查看 80 和 443 端口被什么进程占用', cmd: 'netstat -tulnp | grep -E ":80|:443"' },
  { prompt: '查找全盘大于 100MB 的文件并降序排列', cmd: 'find / -type f -size +100M -exec ls -lh {} + 2>/dev/null | sort -k5 -rh | head -n 10' },
  { prompt: '清理 Docker 未使用的镜像和构建缓存', cmd: 'docker system prune -af --volumes' },
  { prompt: '按内存消耗对前 10 个进程进行排序', cmd: 'ps aux --sort=-%mem | head -n 11' },
  { prompt: '实时监控网络流量与网卡统计', cmd: 'ip -s link' },
]

function formatFileSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let val = size, i = 0
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++ }
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function splitJsonFrames(raw: string): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = []
  try { out.push(JSON.parse(raw) as Record<string, unknown>); return out } catch { /* continue */ }
  let depth = 0, start = -1, inStr = false, esc = false
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]
    if (esc) { esc = false; continue }
    if (ch === '\\' && inStr) { esc = true; continue }
    if (ch === '"') { inStr = !inStr; continue }
    if (inStr) continue;
    if (ch === '{') { if (depth === 0) start = i; depth++ }
    else if (ch === '}') {
      depth--
      if (depth === 0 && start !== -1) {
        try { out.push(JSON.parse(raw.slice(start, i + 1)) as Record<string, unknown>) } catch { /* skip */ }
        start = -1
      }
    }
  }
  return out
}

export default function TerminalModal({ open, assetId, assetName = '', assetIp = '', onClose }: Props) {
  const { ref: termRef, write, resize, focus } = useTerminal()
  const wsRef = useRef<WebSocket | null>(null)
  const mountRef = useRef(true)
  const timerRef = useRef<number | null>(null)
  const manualClose = useRef(false)
  const uploadInputRef = useRef<HTMLInputElement | null>(null)

  // 窗口与主题模式
  const [themeKey, setThemeKey] = useState<ThemeKey>('slate')
  const [isMaximized, setIsMaximized] = useState(false)
  const [isMinimized, setIsMinimized] = useState(false)
  const [sftpOpen, setSftpOpen] = useState(false)
  const [aiModalOpen, setAiModalOpen] = useState(false)
  const [connState, setConnState] = useState<ConnState>('idle')

  // AI 助手输入
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiGeneratedCmd, setAiGeneratedCmd] = useState('')
  const [aiGenerating, setAiGenerating] = useState(false)

  // SFTP 状态
  const [sftpPath, setSftpPath] = useState(SFTP_DEFAULT_PATH)
  const [pathInput, setPathInput] = useState(SFTP_DEFAULT_PATH)
  const [fileItems, setFileItems] = useState<FileItem[]>([])
  const [sftpLoading, setSftpLoading] = useState(false)
  const [mkdirModalOpen, setMkdirModalOpen] = useState(false)
  const [newDirName, setNewDirName] = useState('')
  const [actionBusy, setActionBusy] = useState('')

  const curTheme = THEMES[themeKey]

  const connectRef = useRef<() => Promise<void>>(async () => {})

  // 稳定 onData 引用，断开状态下按回车 (Enter) 自动触发重连
  const handleTerminalData = useCallback((data: string) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data }))
    } else {
      // 处于断开状态且按下 Enter 键 (\r 或 \n) 时自动重连
      if (data.includes('\r') || data.includes('\n')) {
        void connectRef.current()
      }
    }
  }, [])

  const handleTerminalResize = useCallback((cols: number, rows: number) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols, rows }))
    }
  }, [])

  // 注入命令到终端（带回车，直接执行）——仅用于预设安全快捷指令
  const injectCommand = useCallback((cmd: string) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data: `${cmd.trim()}\r` }))
      focus()
      antdMessage.success(`已注入命令: ${cmd}`)
    } else {
      antdMessage.warning('终端未建立连接，请先连接或按回车键重连')
    }
  }, [focus])

  // 填入命令但不回车执行（人在回路）——用于 AI 生成命令，交由用户 review 后手动执行
  const injectCommandForReview = useCallback((cmd: string) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      // 仅取首行避免多行命令自动串行执行，末尾不带 \r
      const firstLine = cmd.split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#'))[0] ?? ''
      ws.send(JSON.stringify({ type: 'input', data: firstLine }))
      focus()
      antdMessage.info('命令已填入终端，请检查无误后按 Enter 执行')
    } else {
      antdMessage.warning('终端未建立连接，请先连接或按回车键重连')
    }
  }, [focus])

  const writeSystem = useCallback((msg: string) => {
    write(`\r\n\x1b[32m[system]\x1b[0m ${msg}\r\n`)
  }, [write])

  const writeError = useCallback((msg: string) => {
    write(`\r\n\x1b[31m[error]\x1b[0m ${msg}\r\n`)
  }, [write])

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) { window.clearTimeout(timerRef.current); timerRef.current = null }
  }, [])

  const disconnect = useCallback(() => {
    clearTimer()
    if (wsRef.current) {
      manualClose.current = true
      wsRef.current.close(1000, 'client-close')
      wsRef.current = null
    }
  }, [clearTimer])

  const connect = useCallback(async () => {
    disconnect()
    manualClose.current = false
    setConnState('connecting')
    writeSystem(`正在连接到 ${assetName} (${assetIp})…`)

    const currentCols = isMaximized ? MAX_COLS : NORMAL_COLS
    const currentRows = isMaximized ? MAX_ROWS : NORMAL_ROWS

    let sessionToken: string, wsPath: string
    try {
      const res = await terminalApi.createSession(assetId, currentCols, currentRows)
      const r = res as Record<string, unknown>
      sessionToken = String(r.session_token ?? '')
      wsPath = String(r.ws_path ?? '')
    } catch (e: unknown) {
      setConnState('error')
      writeError((e as { message?: string })?.message ?? '创建终端会话失败')
      write(`\r\n\x1b[36m[提示]\x1b[0m 请检查网络后，按 \x1b[1;32m[Enter 回车键]\x1b[0m 尝试重新连接...\r\n`)
      return
    }

    if (!sessionToken || !wsPath) {
      setConnState('error')
      writeError('会话凭证响应格式异常')
      return
    }

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${proto}//${location.host}${wsPath}?token=${encodeURIComponent(sessionToken)}`
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      clearTimer()
      setConnState('connected')
      writeSystem(`已成功建立 SSH 终端会话！ (${assetName})`)

      try {
        ws.send(JSON.stringify({ type: 'resize', cols: currentCols, rows: currentRows }))
      } catch { /* ignore */ }

      setTimeout(() => {
        focus()
        resize(currentCols, currentRows)
      }, 50)
    }

    ws.onmessage = (evt: MessageEvent<string>) => {
      const frames = splitJsonFrames(evt.data)
      for (const msg of frames) {
        const type = String(msg.type ?? '')
        if (type === 'output') write(String(msg.data ?? ''))
        else if (type === 'error') writeError(String(msg.data ?? ''))
        else if (type === 'system') writeSystem(String(msg.data ?? ''))
      }
    }

    ws.onerror = () => {
      if (!manualClose.current) {
        setConnState('error')
        writeError('WebSocket 通信连接异常')
        write(`\r\n\x1b[36m[提示]\x1b[0m 请按 \x1b[1;32m[Enter 回车键]\x1b[0m 或点击顶部「重连」恢复会话...\r\n`)
      }
    }
    ws.onclose = () => {
      if (!manualClose.current) {
        setConnState('closed')
        write(`\r\n\x1b[33m[OpsPilot SSH]\x1b[0m SSH 会话已断开（空闲超时或网络中断）。`)
        write(`\r\n\x1b[36m[提示]\x1b[0m 请按 \x1b[1;32m[Enter 回车键]\x1b[0m 或点击顶部「重连」重新连接...\r\n`)
      }
    }
  }, [assetId, assetIp, assetName, disconnect, writeSystem, writeError, clearTimer, focus, resize, write, isMaximized])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  useEffect(() => {
    mountRef.current = true
    if (open && assetId) {
      // 连接动作延后到微任务（connect 内部同步置状态），避免在 effect 内同步 setState
      queueMicrotask(() => void connect())
    } else {
      disconnect()
      // 状态复位延后到微任务，避免在 effect 内同步 setState 触发级联渲染
      queueMicrotask(() => setConnState('idle'))
    }
    return () => { mountRef.current = false; disconnect() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, assetId])

  // 全屏切换
  const toggleMaximize = () => {
    const nextState = !isMaximized
    setIsMaximized(nextState)
    const targetCols = nextState ? MAX_COLS : NORMAL_COLS
    const targetRows = nextState ? MAX_ROWS : NORMAL_ROWS

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'resize', cols: targetCols, rows: targetRows }))
    }

    setTimeout(() => {
      focus()
      resize(targetCols, targetRows)
    }, 150)
  }

  // AI 生成 Linux 指令：优先命中本地预设，否则调用后端专用命令生成端点
  const handleGenerateAiCmd = async (promptText?: string) => {
    const query = (promptText || aiPrompt).trim()
    if (!query) return
    setAiGenerating(true)
    setAiGeneratedCmd('')

    // 匹配预设快捷推荐（离线即时返回）
    const found = AI_PRESETS.find((p) => p.prompt.includes(query) || query.includes(p.prompt))
    if (found) {
      setAiGeneratedCmd(found.cmd)
      setAiGenerating(false)
      return
    }

    try {
      const result = await aiApi.generateCommand(query, 'Linux')
      if (result.ok && result.command) {
        setAiGeneratedCmd(result.command)
      } else {
        setAiGeneratedCmd(`# ${result.notice || 'AI 未能生成命令，请换一种描述再试。'}`)
      }
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setAiGeneratedCmd(`# 命令生成失败：${detail || '请稍后重试或检查 AI 模型配置。'}`)
    } finally {
      setAiGenerating(false)
    }
  }

  // ── SFTP 文件管理逻辑 ──
  const fetchFiles = useCallback(async (targetPath: string) => {
    setSftpLoading(true)
    try {
      const res = await terminalApi.listFiles(assetId, targetPath)
      const data = res as { items?: FileItem[]; path?: string }
      setFileItems(data.items ?? [])
      const actual = data.path ?? targetPath
      setSftpPath(actual); setPathInput(actual)
    } catch {
      antdMessage.error('读取 SFTP 目录失败')
    } finally {
      setSftpLoading(false)
    }
  }, [assetId])

  useEffect(() => {
    // 加载动作延后到微任务（fetchFiles 内部同步置 loading），避免在 effect 内同步 setState
    if (sftpOpen && open) queueMicrotask(() => void fetchFiles(SFTP_DEFAULT_PATH))
  }, [sftpOpen, open, fetchFiles])

  const handleGoParent = () => {
    if (sftpPath === '/' || !sftpPath) return
    const parts = sftpPath.split('/').filter(Boolean)
    parts.pop()
    const parent = '/' + parts.join('/')
    void fetchFiles(parent)
  }

  const handleMkdirSubmit = async () => {
    const name = newDirName.trim()
    if (!name) { antdMessage.warning('目录名不能为空'); return }
    const target = sftpPath === '/' ? `/${name}` : `${sftpPath}/${name}`
    setActionBusy('mkdir')
    try {
      await terminalApi.mkdir(assetId, target)
      antdMessage.success('目录创建成功')
      setMkdirModalOpen(false)
      setNewDirName('')
      void fetchFiles(sftpPath)
    } catch (e: unknown) {
      antdMessage.error((e as { message?: string })?.message ?? '新建目录失败')
    } finally {
      setActionBusy('')
    }
  }

  const handleUploadFile = async (file: File | undefined) => {
    if (!file) return
    if (file.size > UPLOAD_LIMIT_BYTES) {
      antdMessage.error(`文件过大 (${formatFileSize(file.size)})，上限 100MB`)
      if (uploadInputRef.current) uploadInputRef.current.value = ''
      return
    }
    setActionBusy('upload')
    const hide = antdMessage.loading(`正在上传 ${file.name}…`, 0)
    try {
      await terminalApi.uploadFile(assetId, sftpPath, file)
      hide()
      antdMessage.success('文件上传成功')
      void fetchFiles(sftpPath)
    } catch (e: unknown) {
      hide()
      antdMessage.error((e as { message?: string })?.message ?? '上传失败')
    } finally {
      setActionBusy('')
      if (uploadInputRef.current) uploadInputRef.current.value = ''
    }
  }

  const handleDeleteItem = async (item: FileItem) => {
    setActionBusy(`delete:${item.path}`)
    try {
      await terminalApi.deleteFile(assetId, item.path)
      antdMessage.success(`已删除 ${item.name}`)
      void fetchFiles(sftpPath)
    } catch (e: unknown) {
      antdMessage.error((e as { message?: string })?.message ?? '删除失败')
    } finally {
      setActionBusy('')
    }
  }

  if (!open) return null

  if (isMinimized) {
    return (
      <div
        onClick={() => setIsMinimized(false)}
        style={{
          position: 'fixed', bottom: 20, right: 20, zIndex: 9999,
          background: curTheme.toolbar, border: `1px solid ${curTheme.border}`,
          borderRadius: 8, padding: '8px 16px', color: '#ffffff',
          display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
          boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
        }}
      >
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#16a34a' }} />
        <span style={{ fontSize: 13, fontWeight: 600 }}>root@{assetIp} ({assetName})</span>
        <span style={{ fontSize: 11, color: '#facc15' }}>[悬浮中 · 点击展开]</span>
      </div>
    )
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      title={null}
      closable={false}
      destroyOnClose
      centered
      width={isMaximized ? '100vw' : 1140}
      style={isMaximized ? { top: 0, padding: 0, maxWidth: '100vw' } : { padding: 0 }}
      styles={{
        content: {
          background: curTheme.bg,
          padding: 0,
          borderRadius: isMaximized ? 0 : 12,
          overflow: 'hidden',
          boxShadow: isMaximized ? 'none' : '0 25px 60px -12px rgba(0,0,0,0.95), 0 0 0 1px rgba(255,255,255,0.08)',
          border: isMaximized ? 'none' : `1px solid ${curTheme.border}`,
          height: isMaximized ? '100vh' : 650,
          display: 'flex',
          flexDirection: 'column',
        },
        mask: { backdropFilter: 'blur(8px)', background: 'rgba(0,0,0,0.75)' }
      }}
    >
      {/* ── macOS 风格 Header (集成 AI 助手、快捷命令、主题皮肤、SFTP) ────────────────── */}
      <div
        onDoubleClick={toggleMaximize}
        style={{
          height: 42,
          background: curTheme.toolbar,
          borderBottom: `1px solid ${curTheme.border}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 14px',
          userSelect: 'none',
          cursor: 'pointer',
        }}
      >
        {/* 左侧控点 + 标题 + 绿底印章 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span onClick={onClose} style={{ width: 12, height: 12, borderRadius: '50%', background: '#ff5f56', cursor: 'pointer', display: 'inline-block' }} />
            <span onClick={() => setIsMinimized(true)} style={{ width: 12, height: 12, borderRadius: '50%', background: '#ffbd2e', cursor: 'pointer', display: 'inline-block' }} />
            <span onClick={toggleMaximize} style={{ width: 12, height: 12, borderRadius: '50%', background: '#27c93f', cursor: 'pointer', display: 'inline-block' }} />
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 500, color: curTheme.txt }}>
            <span style={{ color: curTheme.txtMuted, fontFamily: 'monospace' }}>&gt;_</span>
            <span>root@{assetIp} ({assetName})</span>
          </div>

          <span
            onClick={() => { if (connState !== 'connected') void connect() }}
            style={{
              padding: '2px 8px',
              borderRadius: 4,
              fontSize: 11,
              background: connState === 'connected' ? '#16a34a' : '#ef4444',
              color: '#ffffff',
              fontWeight: 600,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              cursor: connState !== 'connected' ? 'pointer' : 'default',
            }}
            title={connState !== 'connected' ? '点击或在终端按回车键重新连接' : ''}
          >
            {connState === 'connected' ? '终端已连接' : connState === 'connecting' ? '连接中…' : '已断开 (按 Enter 重连)'}
          </span>
        </div>

        {/* 右侧：🤖 AI 助手 + ⚡ 快捷指令 + 🎨 主题 + 📁 SFTP + [-][⤢][X] */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {/* 🤖 AI 命令助手 */}
          <Button
            size="small"
            type="primary"
            icon={<RobotOutlined style={{ color: '#38bdf8' }} />}
            onClick={(e) => { e.stopPropagation(); setAiModalOpen(true) }}
            style={{ fontSize: 12, background: '#1e293b', color: '#f8fafc', borderColor: '#334155', fontWeight: 500 }}
          >
            AI 助手
          </Button>

          {/* ⚡ 常用快捷指令 Dropdown */}
          <Dropdown
            menu={{
              items: QUICK_COMMANDS.map((item, idx) => ({
                key: idx,
                label: (
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }} onClick={() => injectCommand(item.cmd)}>
                    <span style={{ fontWeight: 500 }}>{item.label}</span>
                    <span style={{ fontFamily: 'monospace', color: '#94a3b8', fontSize: 11 }}>{item.cmd}</span>
                  </div>
                ),
              })),
            }}
            trigger={['click']}
          >
            <Button size="small" type="default" icon={<ThunderboltOutlined style={{ color: '#facc15' }} />} onClick={(e) => e.stopPropagation()} style={{ fontSize: 12 }}>
              快捷指令
            </Button>
          </Dropdown>

          {/* 🎨 主题皮肤切换 */}
          <Dropdown
            menu={{
              items: Object.values(THEMES).map((t) => ({
                key: t.key,
                label: (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }} onClick={() => setThemeKey(t.key as ThemeKey)}>
                    <span style={{ width: 10, height: 10, borderRadius: '50%', background: t.bg, border: '1px solid #94a3b8' }} />
                    <span>{t.name}</span>
                  </div>
                ),
              })),
            }}
            trigger={['click']}
          >
            <Button size="small" type="default" icon={<SkinOutlined />} onClick={(e) => e.stopPropagation()} style={{ fontSize: 12 }}>
              皮肤
            </Button>
          </Dropdown>

          {/* 🧹 一键清屏 */}
          <Tooltip title="清除屏幕日志">
            <Button size="small" type="default" icon={<ClearOutlined />} onClick={(e) => { e.stopPropagation(); injectCommand('clear') }} style={{ fontSize: 12 }} />
          </Tooltip>

          {/* 📁 SFTP 文件管理开关 */}
          <Button
            size="small"
            type={sftpOpen ? 'primary' : 'default'}
            icon={<FolderOpenOutlined />}
            onClick={(e) => { e.stopPropagation(); setSftpOpen(!sftpOpen) }}
            style={{ fontSize: 12 }}
          >
            SFTP 文件
          </Button>

          {/* 窗口三件套 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, marginLeft: 4 }}>
            <Tooltip title="最小化">
              <Button size="small" type="text" icon={<MinusOutlined style={{ color: curTheme.txtMuted }} />} onClick={(e) => { e.stopPropagation(); setIsMinimized(true) }} />
            </Tooltip>
            <Tooltip title={isMaximized ? '还原' : '全屏最大化'}>
              <Button size="small" type="text" icon={isMaximized ? <FullscreenExitOutlined style={{ color: curTheme.txtMuted }} /> : <FullscreenOutlined style={{ color: curTheme.txtMuted }} />} onClick={(e) => { e.stopPropagation(); toggleMaximize() }} />
            </Tooltip>
            <Tooltip title="关闭">
              <Button size="small" type="text" danger icon={<CloseOutlined />} onClick={(e) => { e.stopPropagation(); onClose() }} />
            </Tooltip>
          </div>
        </div>
      </div>

      {/* ── 主体区域 (Terminal + 全功能 SFTP 面板) ────────────── */}
      <div
        onMouseDown={() => focus()}
        onClick={() => focus()}
        style={{ flex: 1, display: 'flex', minHeight: 0, position: 'relative', background: curTheme.bg, cursor: 'text' }}
      >
        <div style={{ flex: 1, height: isMaximized ? 'calc(100vh - 42px)' : '608px', position: 'relative', overflow: 'hidden', background: curTheme.bg }}>
          <style>{`
            .wterm {
              background-color: ${curTheme.bg} !important;
              padding: 6px 10px 28px 10px !important;
              box-sizing: border-box !important;
            }
            .wterm canvas { background-color: ${curTheme.bg} !important; }
            /* 恢复 wterm 自带 scrollback：旧样式用 overflow:hidden !important 盖掉了
               .has-scrollback 的 overflow-y:auto，导致鼠标滚轮无法回看历史输出。 */
            .wterm.has-scrollback {
              overflow-y: auto !important;
              overflow-x: hidden !important;
            }
            .wterm:not(.has-scrollback) {
              overflow: hidden !important;
            }
          `}</style>
          <Terminal
            ref={termRef}
            cols={isMaximized ? MAX_COLS : NORMAL_COLS}
            rows={isMaximized ? MAX_ROWS : NORMAL_ROWS}
            autoResize={true}
            cursorBlink={true}
            onData={handleTerminalData}
            onResize={handleTerminalResize}
            style={{ width: '100%', height: '100%', background: curTheme.bg }}
          />
        </div>

        {/* SFTP 全功能文件管理器侧栏 */}
        {sftpOpen && (
          <div
            onClick={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
            style={{ width: 380, height: '100%', background: curTheme.toolbar, borderLeft: `1px solid ${curTheme.border}`, display: 'flex', flexDirection: 'column' }}
          >
            {/* 顶栏 */}
            <div style={{ padding: '8px 12px', borderBottom: `1px solid ${curTheme.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ color: curTheme.txt, fontWeight: 600, fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
                <FolderOpenOutlined style={{ color: '#facc15' }} /> SFTP 文件管理器
              </span>
              <Button size="small" type="text" icon={<CloseOutlined style={{ color: curTheme.txtMuted }} />} onClick={() => setSftpOpen(false)} />
            </div>

            {/* 路径跳转与动作工具栏 */}
            <div style={{ padding: '10px 12px', borderBottom: `1px solid ${curTheme.border}`, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {/* 路径搜索行 */}
              <div style={{ display: 'flex', gap: 6 }}>
                <Input
                  size="small"
                  value={pathInput}
                  onChange={(e) => setPathInput(e.target.value)}
                  onPressEnter={() => void fetchFiles(pathInput)}
                  placeholder="/root"
                  style={{ background: curTheme.bg, borderColor: curTheme.border, color: curTheme.txt, fontSize: 12, fontFamily: 'monospace' }}
                />
                <Tooltip title="跳转">
                  <Button size="small" icon={<CompassOutlined />} onClick={() => void fetchFiles(pathInput)} />
                </Tooltip>
              </div>

              {/* 操作按钮组：根目录 | 上级 | 刷新 | +新建目录 | ↑上传文件 */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  <Tooltip title="根目录">
                    <Button size="small" icon={<HomeOutlined />} onClick={() => void fetchFiles('/root')}>root</Button>
                  </Tooltip>
                  <Tooltip title="返回上级">
                    <Button size="small" icon={<ArrowLeftOutlined />} onClick={handleGoParent} disabled={sftpPath === '/'} />
                  </Tooltip>
                  <Tooltip title="刷新">
                    <Button size="small" icon={<ReloadOutlined spin={sftpLoading} />} onClick={() => void fetchFiles(sftpPath)} />
                  </Tooltip>
                </div>

                <div style={{ display: 'flex', gap: 4 }}>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<FolderAddOutlined />}
                    onClick={() => setMkdirModalOpen(true)}
                  >
                    新建
                  </Button>

                  <Button
                    size="small"
                    type="primary"
                    icon={<UploadOutlined />}
                    loading={actionBusy === 'upload'}
                    onClick={() => uploadInputRef.current?.click()}
                  >
                    上传
                  </Button>
                  <input
                    ref={uploadInputRef}
                    type="file"
                    style={{ display: 'none' }}
                    onChange={(e) => void handleUploadFile(e.target.files?.[0])}
                  />
                </div>
              </div>
            </div>

            {/* 新建目录 Modal */}
            <Modal
              title="新建目录"
              open={mkdirModalOpen}
              onOk={handleMkdirSubmit}
              confirmLoading={actionBusy === 'mkdir'}
              onCancel={() => setMkdirModalOpen(false)}
              okText="创建"
              cancelText="取消"
              destroyOnClose
            >
              <div style={{ padding: '12px 0' }}>
                <div style={{ fontSize: 12, color: curTheme.txtMuted, marginBottom: 6 }}>在 {sftpPath} 下创建文件夹：</div>
                <Input
                  placeholder="请输入目录名"
                  value={newDirName}
                  onChange={(e) => setNewDirName(e.target.value)}
                  onPressEnter={handleMkdirSubmit}
                  autoFocus
                />
              </div>
            </Modal>

            {/* 文件列表区域 */}
            <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
              {sftpLoading ? (
                <div style={{ textAlign: 'center', padding: '40px 0' }}><Spin size="small" tip="加载目录中…" /></div>
              ) : fileItems.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ color: curTheme.txtMuted }}>当前目录为空</span>} />
              ) : (
                fileItems.map((item) => (
                  <div
                    key={item.path}
                    onDoubleClick={() => item.is_dir && fetchFiles(item.path)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '8px 12px',
                      fontSize: 12,
                      borderBottom: `1px solid ${curTheme.border}44`,
                      cursor: item.is_dir ? 'pointer' : 'default',
                      userSelect: 'none',
                    }}
                    className="hover:bg-slate-800/60 transition-colors"
                  >
                    {/* 左侧：图标 + 名称 + 细节 */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flex: 1 }}>
                      {item.is_dir ? (
                        <FolderOpenOutlined style={{ color: '#38bdf8', fontSize: 14, flexShrink: 0 }} />
                      ) : (
                        <FileTextOutlined style={{ color: '#94a3b8', fontSize: 14, flexShrink: 0 }} />
                      )}
                      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
                        <span
                          title={item.name}
                          style={{
                            color: item.is_dir ? curTheme.txt : curTheme.txtMuted,
                            fontWeight: item.is_dir ? 500 : 400,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {item.name}
                        </span>
                        <span style={{ fontSize: 10, color: curTheme.txtMuted, fontFamily: 'monospace' }}>
                          {item.is_dir ? '目录' : formatFileSize(item.size)} {item.mode ? `· ${item.mode}` : ''}
                        </span>
                      </div>
                    </div>

                    {/* 右侧：下载 / 删除动作 */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
                      {!item.is_dir && (
                        <Tooltip title="下载文件">
                          <a
                            href={terminalApi.downloadUrl(assetId, item.path)}
                            download={item.name}
                            style={{
                              color: '#38bdf8',
                              padding: '2px 6px',
                              borderRadius: 4,
                              background: '#38bdf815',
                              display: 'inline-flex',
                              alignItems: 'center',
                            }}
                          >
                            <DownloadOutlined style={{ fontSize: 12 }} />
                          </a>
                        </Tooltip>
                      )}

                      <Popconfirm
                        title={`确定删除 ${item.is_dir ? '目录' : '文件'} ${item.name}？`}
                        description="删除后无法恢复，请谨慎操作。"
                        onConfirm={() => void handleDeleteItem(item)}
                        okText="确定删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true, size: 'small' }}
                        cancelButtonProps={{ size: 'small' }}
                      >
                        <Tooltip title="删除">
                          <Button
                            size="small"
                            type="text"
                            danger
                            icon={<DeleteOutlined style={{ fontSize: 12 }} />}
                            loading={actionBusy === `delete:${item.path}`}
                          />
                        </Tooltip>
                      </Popconfirm>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {/* ── 🤖 AI 运维命令助手 Modal ────────────────── */}
      <Modal
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#2563eb', fontSize: 18 }} />
            <span style={{ fontWeight: 600, color: '#0f172a' }}>OpsPilot AI 运维命令助手</span>
          </div>
        }
        open={aiModalOpen}
        onCancel={() => setAiModalOpen(false)}
        footer={null}
        destroyOnClose
        centered
        width={560}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, paddingTop: 8 }}>
          <div style={{ fontSize: 12, color: curTheme.txtMuted }}>
            用自然语言描述您的运维意图，AI 将自动为您拼接并优化 Linux 命令。
          </div>

          <div style={{ display: 'flex', gap: 8 }}>
            <Input
              placeholder="例如：查看 80 端口占用 / 查找大文件"
              value={aiPrompt}
              onChange={(e) => setAiPrompt(e.target.value)}
              onPressEnter={() => handleGenerateAiCmd()}
              autoFocus
            />
            <Button
              type="primary"
              icon={<SendOutlined />}
              loading={aiGenerating}
              onClick={() => handleGenerateAiCmd()}
              style={{ background: '#2563eb', borderColor: '#2563eb', fontWeight: 500 }}
            >
              生成
            </Button>
          </div>

          {/* 预设推荐标签 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            <span style={{ fontSize: 12, color: curTheme.txtMuted, alignSelf: 'center' }}>推荐词：</span>
            {AI_PRESETS.map((p, idx) => (
              <span
                key={idx}
                onClick={() => { setAiPrompt(p.prompt); handleGenerateAiCmd(p.prompt) }}
                style={{
                  fontSize: 11,
                  padding: '2px 8px',
                  borderRadius: 4,
                  background: '#f8fafc',
                  color: '#475569',
                  cursor: 'pointer',
                  border: '1px solid #e2e8f0',
                }}
                className="hover:bg-blue-50 hover:text-blue-600 hover:border-blue-200 transition-colors"
              >
                {p.prompt}
              </span>
            ))}
          </div>

          {/* 生成结果展报区 */}
          {aiGeneratedCmd && (
            <div style={{ background: '#0f172a', borderRadius: 8, padding: 12, border: '1px solid #1e293b', marginTop: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <span style={{ fontSize: 11, color: '#94a3b8', fontWeight: 600 }}>生成命令：</span>
                <Button
                  size="small"
                  type="primary"
                  icon={<SendOutlined />}
                  onClick={() => { injectCommandForReview(aiGeneratedCmd); setAiModalOpen(false) }}
                  style={{ background: '#2563eb', borderColor: '#2563eb', fontSize: 12 }}
                >
                  填入终端（需确认执行）
                </Button>
              </div>
              <pre style={{ margin: 0, color: '#4ade80', fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 180, overflowY: 'auto' }}>
                {aiGeneratedCmd}
              </pre>
            </div>
          )}
        </div>
      </Modal>
    </Modal>
  )
}
