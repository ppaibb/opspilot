import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Flex, Input, Button, Typography, Badge, Tag, Space, Avatar,
  Spin, Switch, Tooltip, Popconfirm, App, Popover, Card,
} from 'antd'
import type { InputRef } from 'antd'
import {
  PlusOutlined, SendOutlined, DeleteOutlined, EditOutlined,
  RobotOutlined, UserOutlined, CopyOutlined,
  CheckOutlined, SafetyCertificateOutlined, HddOutlined,
  ThunderboltOutlined, WarningOutlined, BulbOutlined,
  StopOutlined, MessageOutlined, DownloadOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { aiApi } from '@/api'
import { authenticatedFetch } from '@/lib/fetch'
import type { AIMessageItem, AISkillItem } from '@/types/api'

const { Text, Title, Paragraph } = Typography

// 预设快捷 Prompt 提示词
const QUICK_PROMPTS = [
  { icon: <HddOutlined style={{ color: '#1677ff' }} />, label: '全网主机健康巡检', prompt: '@skill.assets.overview 当前主机整体健康度如何？有哪些异常主机需要优先处理？' },
  { icon: <SafetyCertificateOutlined style={{ color: '#faad14' }} />, label: '证书到期风险排查', prompt: '@skill.certificates.overview 检查系统中即将到期或已经过期的 SSL 证书。' },
  { icon: <ThunderboltOutlined style={{ color: '#722ed1' }} />, label: 'PVE 虚拟机状态探查', prompt: '@skill.vms.overview PVE 虚拟机运行情况怎么样？有哪些虚机处于停止状态？' },
  { icon: <WarningOutlined style={{ color: '#ff4d4f' }} />, label: '活动告警优先分析', prompt: '@skill.alerts.active 现在系统有哪些活动告警？哪个告警最值得优先处理？' },
]

/** 助手消息元信息（随消息持久化，notice 为异常提示）。 */
interface AssistantMessageMeta {
  notice?: string
  [key: string]: unknown
}

/** SSE 数据帧（meta/delta/done 事件共用，按事件名取用字段）。 */
interface AssistantStreamFrame {
  text?: string
  answer?: string
  notice?: string
  [key: string]: unknown
}

/** 流式回复的最终结果：text/meta 供落库，failed 标记流中途异常（非主动中止）。 */
interface AssistantStreamOutcome {
  text: string
  meta: AssistantMessageMeta
  failed: boolean
  error?: string
}

/** 生成本地临时消息 ID（模块级函数：Date.now 不纯，不能在组件渲染作用域内调用）。 */
function makeLocalMessageIds(): { userId: string; assistantId: string } {
  const base = Date.now()
  return { userId: base.toString(), assistantId: (base + 1).toString() }
}

/**
 * 发送消息并消费 AI 助手 SSE 流（模块级函数：流式时序逻辑不进组件作用域，便于静态判定无渲染期副作用）。
 * 每解析出一帧就通过 onUpdate 回传当前已聚合的文本与元信息；主动中止（AbortError）原样抛出，
 * 其余异常返回 failed=true 并附带已聚合的部分文本。
 */
async function streamAssistantReply(params: {
  streamUrl: string
  content: string
  skillKeys: string[]
  signal: AbortSignal
  onUpdate: (text: string, meta: AssistantMessageMeta) => void
}): Promise<AssistantStreamOutcome> {
  const { streamUrl, content, skillKeys, signal, onUpdate } = params
  let text = ''
  let meta: AssistantMessageMeta = {}

  try {
    const response = await authenticatedFetch(streamUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        message: content,
        use_llm: true,
        focus_skills: skillKeys,
      }),
      signal,
    })

    if (!response.ok) {
      let detail = ''
      try {
        detail = (await response.text()).slice(0, 200)
      } catch { /* ignore */ }
      throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ''}`)
    }
    if (!response.body) {
      throw new Error('响应无正文流')
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    // 空闲超时：60s 收不到任何字节就取消读流，避免永远「思考中」
    let idleTimer: ReturnType<typeof setTimeout> | undefined
    let idleHit = false
    const resetIdle = () => {
      if (idleTimer) clearTimeout(idleTimer)
      idleTimer = setTimeout(() => {
        idleHit = true
        void reader.cancel().catch(() => undefined)
      }, 60_000)
    }
    resetIdle()

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        resetIdle()

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n\n')
        buffer = lines.pop() || ''

        for (const block of lines) {
          if (!block.trim()) continue
          let eventName = 'message'
          let eventData = ''

          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) eventName = line.replace('event:', '').trim()
            if (line.startsWith('data:')) eventData = line.replace('data:', '').trim()
          }

          if (eventData) {
            try {
              const parsed = JSON.parse(eventData) as AssistantStreamFrame
              if (eventName === 'meta') {
                meta = { ...parsed }
              } else if (eventName === 'delta') {
                if (parsed.text) {
                  text += parsed.text
                }
              } else if (eventName === 'done') {
                // done 优先采用完整 answer（覆盖过程中的占位/拼接文本）
                if (parsed.answer) {
                  text = parsed.answer
                }
                if (parsed.notice) {
                  meta.notice = parsed.notice
                }
              }

              onUpdate(text, meta)
            } catch {
              /* ignore */
            }
          }
        }
      }
    } finally {
      if (idleTimer) clearTimeout(idleTimer)
    }
    if (idleHit && !text) {
      throw new Error('读取模型流超时（60s 无数据）')
    }
    return { text, meta, failed: false }
  } catch (err) {
    // 主动中止原样上抛，由调用方区分"用户停止"与"流异常"
    if ((err as Error)?.name === 'AbortError') throw err
    const reason = (err as Error)?.message || '连接中断或服务未响应'
    return { text, meta: { ...meta, notice: reason }, failed: true, error: reason }
  }
}

export default function AiWorkbenchPage() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  // 状态管理
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [inputMessage, setInputMessage] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [copiedIndex, setCopiedIndex] = useState<string | null>(null)
  const [editingTitleId, setEditingTitleId] = useState<string | null>(null)
  const [editingTitleText, setEditingTitleText] = useState('')

  // @Mention 下拉选单控制
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionFilter, setMentionFilter] = useState('')
  const inputRef = useRef<InputRef>(null)
  const mentionBoxRef = useRef<HTMLDivElement>(null)

  // 监听点击外部区域或按 ESC 自动关闭 @Mention 下拉选单
  useEffect(() => {
    if (!mentionOpen) return

    const handleClickOutside = (e: MouseEvent) => {
      if (
        mentionBoxRef.current &&
        !mentionBoxRef.current.contains(e.target as Node)
      ) {
        setMentionOpen(false)
      }
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMentionOpen(false)
      }
    }

    const timer = setTimeout(() => {
      window.addEventListener('click', handleClickOutside)
      window.addEventListener('keydown', handleKeyDown)
    }, 50)

    return () => {
      clearTimeout(timer)
      window.removeEventListener('click', handleClickOutside)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [mentionOpen])

  // 消息流与控制器
  const [messages, setMessages] = useState<AIMessageItem[]>([])
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  // ── Queries ──
  const { data: sessions = [], isLoading: loadingSessions } = useQuery<import('@/types/api').AISessionItem[]>({
    queryKey: ['ai-sessions'],
    queryFn: aiApi.listSessions,
  })

  const { data: skills = [], isLoading: loadingSkills } = useQuery<import('@/types/api').AISkillItem[]>({
    queryKey: ['ai-skills'],
    queryFn: aiApi.listSkills,
  })

  const { data: configs } = useQuery<import('@/types/api').LLMConfigItem[]>({
    queryKey: ['ai-configs'],
    queryFn: aiApi.listConfigs,
  })

  // ── Mutations ──
  const createSessionMut = useMutation({
    mutationFn: (title?: string) => aiApi.createSession(title),
    onSuccess: (newSession) => {
      queryClient.invalidateQueries({ queryKey: ['ai-sessions'] })
      setCurrentSessionId(newSession.session_id)
      setMessages([])
    },
  })

  const deleteSessionMut = useMutation({
    mutationFn: (sessionId: string) => aiApi.deleteSession(sessionId),
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['ai-sessions'] })
      if (currentSessionId === deletedId) {
        setCurrentSessionId(null)
        setMessages([])
      }
      message.success('会话已删除')
    },
  })

  // 导出会话为 Markdown 文件（当前激活会话；含用户/助手双方消息）
  const handleExportSession = async () => {
    const sessionId = currentSessionId
    if (!sessionId) {
      message.warning('当前没有激活的会话')
      return
    }
    try {
      const detail = await aiApi.getSessionDetail(sessionId)
      const lines: string[] = [`# ${detail.title || 'AI 对话'}`, '']
      for (const m of detail.messages || []) {
        const who = m.role === 'user' ? '## 🧑 用户' : '## 🤖 助手'
        lines.push(who, '', m.content || '', '')
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `AI对话-${(detail.title || sessionId).replace(/[\\/:*?"<>|]/g, '_').slice(0, 30)}-${dayjs().format('YYYYMMDD')}.md`
      a.click()
      URL.revokeObjectURL(a.href)
      message.success('会话已导出为 Markdown')
    } catch {
      message.error('导出失败，请稍后重试')
    }
  }

  const renameSessionMut = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => aiApi.renameSession(id, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ai-sessions'] })
      setEditingTitleId(null)
    },
  })

  const updateSkillMut = useMutation({
    mutationFn: (updatedSkills: Array<{ key: string; enabled: boolean }>) => aiApi.updateSkills(updatedSkills),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ai-skills'] })
      message.success('Skill 技能状态已更新')
    },
  })

  // ── 监听切换会话加载历史消息 ──
  // 流式期间禁止用服务端快照覆盖本地气泡（否则 onUpdate 对不上 id，界面会永远「思考中」）
  const streamingRef = useRef(false)
  useEffect(() => {
    streamingRef.current = isStreaming
  }, [isStreaming])

  useEffect(() => {
    if (!currentSessionId) return
    if (streamingRef.current) return
    aiApi.getSessionDetail(currentSessionId).then((res) => {
      // 流式过程中服务端可能尚未落库，避免再次清空本地消息
      if (streamingRef.current) return
      setMessages(res.messages || [])
    }).catch(() => {
      if (!streamingRef.current) setMessages([])
    })
  }, [currentSessionId])

  // 自动滚动底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isStreaming])

  const handleNewSession = () => {
    createSessionMut.mutate('新对话')
  }

  const handleToggleSkill = (skillKey: string, checked: boolean) => {
    const nextSkills = skills.map((s) => ({
      key: s.key,
      enabled: s.key === skillKey ? checked : s.enabled,
    }))
    updateSkillMut.mutate(nextSkills)
  }

  const handleCopyCode = (code: string, id: string) => {
    navigator.clipboard.writeText(code)
    setCopiedIndex(id)
    message.success('代码已复制')
    setTimeout(() => setCopiedIndex(null), 2000)
  }

  const handleStopStream = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    setIsStreaming(false)
  }

  // ── 输入框键入监听与 @Mention 控制 ──
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value
    setInputMessage(val)

    // 检查是否输入了 @ 字符
    const lastAtPos = val.lastIndexOf('@')
    if (lastAtPos !== -1) {
      const charAfterAt = val.slice(lastAtPos + 1)
      if (!charAfterAt.includes(' ')) {
        setMentionFilter(charAfterAt.toLowerCase())
        setMentionOpen(true)
        return
      }
    }
    setMentionOpen(false)
  }

  // 选择插入 Skill 标签
  const handleSelectSkillTag = (skill: AISkillItem) => {
    const lastAtPos = inputMessage.lastIndexOf('@')
    const newInput = lastAtPos !== -1
      ? inputMessage.slice(0, lastAtPos) + `@${skill.key} `
      : inputMessage + `@${skill.key} `
    setInputMessage(newInput)
    setMentionOpen(false)
    // 如果该 skill 尚未开启，自动将其开启
    if (!skill.enabled) {
      handleToggleSkill(skill.key, true)
    }
    inputRef.current?.focus()
  }

  // ── 核心 SSE 流数据接收 ──
  const handleSendMessage = async (textToSend?: string) => {
    const content = (textToSend || inputMessage).trim()
    if (!content || isStreaming) return

    let activeSessionId = currentSessionId
    // 先进入流式态：防止「新建会话 → effect 拉历史」把本地气泡冲掉
    streamingRef.current = true
    setIsStreaming(true)

    if (!activeSessionId) {
      try {
        // 会话标题取首条消息的可读摘要：剥掉开头的 @skill.xxx 引用（否则快捷技能会话全叫技能名）
        const titleSource = content.replace(/^@[\w.]+\s*/, '').trim() || content
        const newSession = await aiApi.createSession(titleSource.slice(0, 18))
        activeSessionId = newSession.session_id
        setCurrentSessionId(activeSessionId)
        queryClient.invalidateQueries({ queryKey: ['ai-sessions'] })
      } catch {
        streamingRef.current = false
        setIsStreaming(false)
        message.error('创建会话失败')
        return
      }
    }

    // 从发送文本中自动提取包含的 @skill 项
    const matchedSkillKeys = skills
      .filter((s) => content.includes(`@${s.key}`) || content.includes(s.title))
      .map((s) => s.key)

    const { userId: userMsgId, assistantId: assistantMsgId } = makeLocalMessageIds()
    const userMsg: AIMessageItem = { id: userMsgId, role: 'user', content, created_at: dayjs().format('HH:mm:ss') }
    setMessages((prev) => [...prev, userMsg])
    setInputMessage('')
    setMentionOpen(false)

    try {
      await aiApi.appendMessage(activeSessionId, 'user', content)
    } catch {
      /* ignore */
    }

    setMessages((prev) => [
      ...prev,
      { id: assistantMsgId, role: 'assistant', content: '', created_at: dayjs().format('HH:mm:ss') },
    ])

    const abortController = new AbortController()
    abortControllerRef.current = abortController

    try {
      // 流式时序逻辑收敛在模块级 streamAssistantReply，组件只消费回调
      const outcome = await streamAssistantReply({
        streamUrl: aiApi.getStreamUrl('ops'),
        content,
        skillKeys: matchedSkillKeys,
        signal: abortController.signal,
        onUpdate: (frameText, frameMeta) => {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? { ...m, content: frameText, meta: frameMeta }
                : m,
            ),
          )
        },
      })

      if (outcome.failed) {
        const reason = outcome.error || outcome.meta?.notice || '连接中断或服务未响应'
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? { ...m, content: outcome.text || `⚠️ ${reason}`, meta: { notice: reason } }
              : m,
          ),
        )
      } else if (!outcome.text) {
        // 流正常结束但没有正文：不要永远停在「思考中…」
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? {
                  ...m,
                  content: '⚠️ 本次没有返回内容，请重试或检查模型配置。',
                  meta: { ...(outcome.meta || {}), notice: outcome.meta?.notice || '空响应' },
                }
              : m,
          ),
        )
      }

      if (outcome.text && activeSessionId) {
        await aiApi.appendMessage(activeSessionId, 'assistant', outcome.text, outcome.meta)
        queryClient.invalidateQueries({ queryKey: ['ai-sessions'] })
      }
    } catch (err: unknown) {
      if ((err as Error)?.name !== 'AbortError') {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId
              ? { ...m, content: '⚠️ 连接中断或服务未响应', meta: { notice: '连接错误' } }
              : m,
          ),
        )
      }
    } finally {
      setIsStreaming(false)
      abortControllerRef.current = null
    }
  }

  // 渲染内联 @skill 标签与常规粗体代码
  const renderInlineFormatted = (inlineText: string) => {
    // 正则提取 @skill.xxx 标签、**粗体** 与 `代码`
    const tokens = inlineText.split(/(@skill\.[a-zA-Z0-9._-]+|\*\*[\s\S]*?\*\*|`[^`]+`)/g)
    return tokens.map((token, idx) => {
      if (token.startsWith('@skill.')) {
        const skillKey = token.slice(1)
        const sk = skills.find((s) => s.key === skillKey)
        return (
          <Tag key={idx} color="purple" style={{ borderRadius: 10, margin: '0 4px', fontSize: 12 }}>
            @{sk ? sk.title : skillKey}
          </Tag>
        )
      }
      if (token.startsWith('**') && token.endsWith('**')) {
        return (
          <Text key={idx} style={{ fontWeight: 600, color: '#0f172a' }}>
            {token.slice(2, -2)}
          </Text>
        )
      }
      if (token.startsWith('`') && token.endsWith('`')) {
        return (
          <code key={idx} style={{ background: '#f1f5f9', color: '#0284c7', padding: '2px 6px', borderRadius: 4, fontFamily: 'monospace', fontSize: 12, margin: '0 2px' }}>
            {token.slice(1, -1)}
          </code>
        )
      }
      return token
    })
  }

  // 辅助渲染按行解析的 Markdown 块
  const renderMarkdownBlock = (blockText: string) => {
    const lines = blockText.split('\n')
    return (
      <Flex vertical gap={4}>
        {lines.map((line, lineIdx) => {
          const trimmed = line.trim()
          if (!trimmed) return <div key={lineIdx} style={{ height: 4 }} />

          const headingMatch = line.match(/^(#{1,6})\s+(.*)$/)
          if (headingMatch) {
            const level = headingMatch[1].length
            const titleContent = headingMatch[2]
            return (
              <div key={lineIdx} style={{ marginTop: 8, marginBottom: 4 }}>
                <Text style={{ fontSize: level === 1 ? 18 : level === 2 ? 16 : 14, fontWeight: 600, color: '#0f172a' }}>
                  {renderInlineFormatted(titleContent)}
                </Text>
              </div>
            )
          }

          const subListMatch = line.match(/^(\s{2,}|\t+)[-*+]\s+(.*)$/)
          if (subListMatch) {
            return (
              <Flex key={lineIdx} align="flex-start" gap={6} style={{ paddingLeft: 20, margin: '1px 0' }}>
                <span style={{ color: '#94a3b8', fontSize: 10, lineHeight: '22px' }}>▫</span>
                <Text style={{ fontSize: 13, color: '#475569', lineHeight: 1.6 }}>
                  {renderInlineFormatted(subListMatch[2])}
                </Text>
              </Flex>
            )
          }

          const unorderedListMatch = line.match(/^[-*+]\s+(.*)$/)
          if (unorderedListMatch) {
            return (
              <Flex key={lineIdx} align="flex-start" gap={8} style={{ paddingLeft: 4, margin: '2px 0' }}>
                <span style={{ color: '#1677ff', fontSize: 12, lineHeight: '22px' }}>•</span>
                <Text style={{ fontSize: 14, color: '#1e293b', lineHeight: 1.6 }}>
                  {renderInlineFormatted(unorderedListMatch[1])}
                </Text>
              </Flex>
            )
          }

          const orderedListMatch = line.match(/^(\d+)\.\s+(.*)$/)
          if (orderedListMatch) {
            return (
              <Flex key={lineIdx} align="flex-start" gap={8} style={{ paddingLeft: 4, margin: '2px 0' }}>
                <span style={{ color: '#1677ff', fontSize: 13, fontWeight: 600, lineHeight: '22px' }}>{orderedListMatch[1]}.</span>
                <Text style={{ fontSize: 14, color: '#1e293b', lineHeight: 1.6 }}>
                  {renderInlineFormatted(orderedListMatch[2])}
                </Text>
              </Flex>
            )
          }

          return (
            <Paragraph key={lineIdx} style={{ margin: 0, lineHeight: 1.7, color: '#1f2937', fontSize: 14 }}>
              {renderInlineFormatted(line)}
            </Paragraph>
          )
        })}
      </Flex>
    )
  }

  // 代码块极简优雅渲染
  const renderMessageContent = (text: string, msgId: string | number) => {
    const parts = text.split(/(```[\s\S]*?```)/g)
    return (
      <Flex vertical gap={6}>
        {parts.map((part, i) => {
          if (part.startsWith('```')) {
            const lines = part.slice(3, -3).trim().split('\n')
            const lang = lines[0].match(/^[a-zA-Z0-9_-]+$/) ? lines[0] : ''
            const code = lang ? lines.slice(1).join('\n') : lines.join('\n')
            const codeId = `${msgId}-${i}`

            return (
              <div key={i} style={{ background: '#0f172a', borderRadius: 8, padding: '10px 14px', margin: '6px 0', border: '1px solid #1e293b' }}>
                <Flex justify="space-between" align="center" style={{ borderBottom: '1px solid #334155', paddingBottom: 6, marginBottom: 8 }}>
                  <Text style={{ color: '#94a3b8', fontSize: 12, fontFamily: 'monospace' }}>{lang || 'bash'}</Text>
                  <Space size="small">
                    <Button
                      size="small"
                      type="text"
                      icon={copiedIndex === codeId ? <CheckOutlined style={{ color: '#4ade80' }} /> : <CopyOutlined style={{ color: '#94a3b8' }} />}
                      onClick={() => handleCopyCode(code, codeId)}
                      style={{ color: '#94a3b8' }}
                    >
                      {copiedIndex === codeId ? '已复制' : '复制'}
                    </Button>
                  </Space>
                </Flex>
                <pre style={{ margin: 0, color: '#38bdf8', fontFamily: 'Fira Code, monospace', fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                  <code>{code}</code>
                </pre>
              </div>
            )
          }
          return (
            <div key={i}>
              {renderMarkdownBlock(part)}
            </div>
          )
        })}
      </Flex>
    )
  }

  const activeModelName =
    (Array.isArray(configs) ? configs.find((c) => c.is_active || (c as { active?: boolean }).active)?.name : undefined) ||
    '未配置模型'

  // 过滤后的 Mention Skill 选单
  const filteredMentionSkills = skills.filter((s: AISkillItem) => {
    if (!mentionFilter) return true
    return s.title.toLowerCase().includes(mentionFilter) || s.key.toLowerCase().includes(mentionFilter) || s.category.toLowerCase().includes(mentionFilter)
  })

  // Skill 浮层 Popover 内容
  const skillsPopoverContent = (
    <div style={{ width: 320, padding: '4px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 8, paddingBottom: 6, borderBottom: '1px solid #f0f0f0' }}>
        <Text style={{ fontWeight: 600, fontSize: 13 }}>@ 运维 Skill 索引库</Text>
        <Text type="secondary" style={{ fontSize: 11 }}>点击快速唤起或输入 @</Text>
      </Flex>
      <Flex vertical gap={8} style={{ maxHeight: 320, overflowY: 'auto' }}>
        {loadingSkills ? (
          <Spin size="small" />
        ) : (
          skills.map((s: AISkillItem) => (
            <Card key={s.key} size="small" styles={{ body: { padding: '8px 10px' } }} style={{ borderRadius: 6, borderColor: '#f0f0f0' }}>
              <Flex justify="space-between" align="center">
                <div>
                  <Tag color="purple" style={{ fontSize: 11, borderRadius: 8, cursor: 'pointer' }} onClick={() => handleSelectSkillTag(s)}>
                    @{s.title}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 4 }}>{s.description}</Text>
                </div>
                <Switch
                  size="small"
                  checked={s.enabled}
                  onChange={(checked) => handleToggleSkill(s.key, checked)}
                />
              </Flex>
            </Card>
          ))
        )}
      </Flex>
    </div>
  )

  return (
    <Flex style={{ height: 'calc(100vh - 100px)', background: '#fff', borderRadius: 12, overflow: 'hidden', border: '1px solid #e5e7eb' }}>
      {/* ── 1. 左侧 Ant Design Pro 风格极简历史侧边栏 ── */}
      <Flex vertical style={{ width: 250, borderRight: '1px solid #f0f0f0', background: '#fafafa', padding: 14 }} gap={12}>
        <Button
          type="primary"
          block
          icon={<PlusOutlined />}
          onClick={handleNewSession}
          loading={createSessionMut.isPending}
          style={{ background: '#e6f4ff', color: '#1677ff', borderColor: '#91caff', boxShadow: 'none', fontWeight: 500, height: 36 }}
        >
          新建对话
        </Button>

        <Text type="secondary" style={{ fontSize: 12, paddingLeft: 4 }}>历史对话</Text>

        <Flex vertical style={{ flex: 1, overflowY: 'auto' }} gap={2}>
          {loadingSessions ? (
            <Flex justify="center" style={{ paddingTop: 20 }}><Spin size="small" /></Flex>
          ) : sessions.length === 0 ? (
            <Text type="secondary" style={{ textAlign: 'center', paddingTop: 20, fontSize: 12 }}>暂无历史记录</Text>
          ) : (
            sessions.map((s) => {
              const active = s.session_id === currentSessionId
              const isEditing = editingTitleId === s.session_id

              return (
                <Flex
                  key={s.session_id}
                  align="center"
                  justify="space-between"
                  onClick={() => setCurrentSessionId(s.session_id)}
                  style={{
                    padding: '8px 10px',
                    borderRadius: 6,
                    cursor: 'pointer',
                    background: active ? '#ffffff' : 'transparent',
                    boxShadow: active ? '0 1px 3px rgba(0,0,0,0.05)' : 'none',
                    color: active ? '#1677ff' : '#4b5563',
                    transition: 'all 0.15s',
                  }}
                >
                  <Flex align="center" gap={8} style={{ overflow: 'hidden', flex: 1 }}>
                    <MessageOutlined style={{ color: active ? '#1677ff' : '#9ca3af', fontSize: 13 }} />
                    {isEditing ? (
                      <Input
                        size="small"
                        value={editingTitleText}
                        onChange={(e) => setEditingTitleText(e.target.value)}
                        onPressEnter={() => renameSessionMut.mutate({ id: s.session_id, title: editingTitleText })}
                        onBlur={() => renameSessionMut.mutate({ id: s.session_id, title: editingTitleText })}
                        autoFocus
                      />
                    ) : (
                      <Text ellipsis style={{ color: active ? '#1677ff' : 'inherit', fontSize: 13, fontWeight: active ? 500 : 400 }}>
                        {s.title}
                      </Text>
                    )}
                  </Flex>
                  <Space size={2} style={{ opacity: active ? 1 : 0.3 }}>
                    <Tooltip title="导出为 Markdown">
                      <Button
                        size="small"
                        type="text"
                        icon={<DownloadOutlined style={{ fontSize: 11 }} />}
                        onClick={(e) => {
                          e.stopPropagation()
                          if (active) { handleExportSession(); return }
                          // 非激活会话：先拉详情再导出
                          aiApi.getSessionDetail(s.session_id).then((detail) => {
                            const lines: string[] = [`# ${detail.title || 'AI 对话'}`, '']
                            for (const m of detail.messages || []) {
                              lines.push(m.role === 'user' ? '## 🧑 用户' : '## 🤖 助手', '', m.content || '', '')
                            }
                            const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
                            const a = document.createElement('a')
                            a.href = URL.createObjectURL(blob)
                            a.download = `AI对话-${(detail.title || s.session_id).replace(/[\\/:*?"<>|]/g, '_').slice(0, 30)}.md`
                            a.click()
                            URL.revokeObjectURL(a.href)
                            message.success('会话已导出为 Markdown')
                          }).catch(() => message.error('导出失败'))
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="重命名">
                      <Button
                        size="small"
                        type="text"
                        icon={<EditOutlined style={{ fontSize: 11 }} />}
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditingTitleId(s.session_id)
                          setEditingTitleText(s.title)
                        }}
                      />
                    </Tooltip>
                    <Popconfirm title="确定删除？" onConfirm={(e) => { e?.stopPropagation(); deleteSessionMut.mutate(s.session_id) }}>
                      <Button size="small" type="text" danger icon={<DeleteOutlined style={{ fontSize: 11 }} />} onClick={(e) => e.stopPropagation()} />
                    </Popconfirm>
                  </Space>
                </Flex>
              )
            })
          )}
        </Flex>
      </Flex>

      {/* ── 2. 右侧 Ant Design Pro 极简核心对话区 ── */}
      <Flex vertical style={{ flex: 1, minWidth: 0, height: '100%', background: '#fff' }}>
        {/* 轻量无缝 Header */}
        <Flex justify="space-between" align="center" style={{ padding: '12px 24px', borderBottom: '1px solid #f0f0f0' }}>
          <Space size="middle">
            <Text style={{ fontSize: 15, fontWeight: 600, color: '#111827' }}>AI 助手</Text>
            <Badge status="success" text={<span style={{ fontSize: 12, color: '#6b7280' }}>Agent Online</span>} />
            <Tag color="blue" style={{ borderRadius: 10, padding: '0 8px', fontSize: 11 }}>{activeModelName}</Tag>
          </Space>

          <Popover content={skillsPopoverContent} trigger="click" placement="bottomRight">
            <Button size="small" icon={<ThunderboltOutlined style={{ color: '#722ed1' }} />} style={{ borderRadius: 6, fontSize: 12 }}>
              @ 技能索引 ({skills.length})
            </Button>
          </Popover>
        </Flex>

        {/* 极简空灵消息水波视口 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '24px 32px', background: '#fafafa' }}>
          {messages.length === 0 ? (
            <Flex vertical align="center" justify="center" style={{ height: '100%', paddingTop: 20 }}>
              <Avatar size={48} icon={<RobotOutlined />} style={{ background: '#1677ff', marginBottom: 14 }} />
              <Title level={4} style={{ marginBottom: 6, color: '#111827', fontWeight: 600 }}>你好，我是 OpsPilot 智能助手</Title>
              <Text type="secondary" style={{ marginBottom: 28, fontSize: 13 }}>您可以问我关于系统主机巡检、SSL 证书到期排查，或输入 @ 指定技能</Text>

              {/* 快捷推荐 Tag 气泡 */}
              <Flex wrap gap={10} justify="center" style={{ maxWidth: 640 }}>
                {QUICK_PROMPTS.map((item, idx) => (
                  <Button
                    key={idx}
                    type="default"
                    onClick={() => handleSendMessage(item.prompt)}
                    style={{
                      borderRadius: 20,
                      background: '#ffffff',
                      borderColor: '#e5e7eb',
                      color: '#374151',
                      fontSize: 13,
                      height: 36,
                      padding: '0 16px',
                      boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
                    }}
                    icon={item.icon}
                  >
                    {item.label}
                  </Button>
                ))}
              </Flex>
            </Flex>
          ) : (
            <Flex vertical gap={20} style={{ maxWidth: 840, margin: '0 auto' }}>
              {messages.map((msg) => (
                <Flex key={msg.id} justify={msg.role === 'user' ? 'flex-end' : 'flex-start'} gap={12}>
                  {msg.role === 'assistant' && (
                    <Avatar size={32} icon={<RobotOutlined />} style={{ background: '#1677ff', flexShrink: 0 }} />
                  )}

                  <div style={{ maxWidth: '85%' }}>
                    {/* 上下文提示 */}
                    {msg.role === 'assistant' && msg.meta?.context_blocks?.length ? (
                      <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                        <BulbOutlined /> 已检索上下文 ({msg.meta.context_blocks.length} 个来源)
                      </Text>
                    ) : null}

                    {/* ProChat 风格气泡 */}
                    <div
                      style={{
                        padding: msg.role === 'user' ? '10px 16px' : '0',
                        background: msg.role === 'user' ? '#e6f4ff' : 'transparent',
                        color: msg.role === 'user' ? '#1677ff' : '#1f2937',
                        borderRadius: msg.role === 'user' ? '16px 16px 2px 16px' : '0',
                        border: msg.role === 'user' ? '1px solid #bae0ff' : 'none',
                      }}
                    >
                      {msg.role === 'user' ? (
                        <Text style={{ color: '#1677ff', fontSize: 14, whiteSpace: 'pre-wrap' }}>
                          {renderInlineFormatted(msg.content)}
                        </Text>
                      ) : (
                        renderMessageContent(msg.content || '思考中...', msg.id)
                      )}
                    </div>
                  </div>

                  {msg.role === 'user' && (
                    <Avatar size={32} icon={<UserOutlined />} style={{ background: '#9ca3af', flexShrink: 0 }} />
                  )}
                </Flex>
              ))}

              <div ref={messagesEndRef} />
            </Flex>
          )}
        </div>

        {/* ── 3. 底部悬浮 Sender 输入框（含 @Mention 下拉弹窗） ── */}
        <div style={{ padding: '16px 32px 20px', background: '#fafafa', borderTop: '1px solid #f0f0f0', position: 'relative' }}>
          <div
            style={{
              maxWidth: 840,
              margin: '0 auto',
              background: '#ffffff',
              borderRadius: 16,
              border: '1px solid #d1d5db',
              padding: '10px 14px',
              boxShadow: '0 2px 8px rgba(0,0,0,0.04)',
              position: 'relative',
            }}
          >
            {/* @Mention 下拉选单 */}
            {mentionOpen && (
              <div
                ref={mentionBoxRef}
                style={{
                  position: 'absolute',
                  bottom: '100%',
                  left: 16,
                  marginBottom: 8,
                  width: 320,
                  background: '#ffffff',
                  borderRadius: 10,
                  border: '1px solid #e5e7eb',
                  boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)',
                  zIndex: 100,
                  padding: '8px 0',
                }}
              >
                <div style={{ padding: '4px 12px 6px', borderBottom: '1px solid #f0f0f0', fontSize: 11, color: '#9ca3af', fontWeight: 600 }}>
                  选择关联召唤的运维 Skill 技能
                </div>
                <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                  {filteredMentionSkills.length === 0 ? (
                    <div style={{ padding: '12px 16px', fontSize: 12, color: '#9ca3af', textAlign: 'center' }}>未匹配到 Skill</div>
                  ) : (
                    filteredMentionSkills.map((s: AISkillItem) => (
                      <Flex
                        key={s.key}
                        align="center"
                        justify="space-between"
                        onClick={() => handleSelectSkillTag(s)}
                        style={{
                          padding: '8px 12px',
                          cursor: 'pointer',
                          transition: 'background 0.15s',
                        }}
                        className="hover:bg-purple-50"
                      >
                        <Flex align="center" gap={8}>
                          <Tag color="purple" style={{ borderRadius: 8, margin: 0, fontSize: 11 }}>@{s.title}</Tag>
                          <Text type="secondary" style={{ fontSize: 11 }}>{s.category}</Text>
                        </Flex>
                        <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>{s.key}</Text>
                      </Flex>
                    ))
                  )}
                </div>
              </div>
            )}

            <Input.TextArea
              ref={inputRef}
              value={inputMessage}
              onChange={handleInputChange}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault()
                  handleSendMessage()
                }
              }}
              placeholder="输入消息，键入 @ 快速召集运维技能分析 (Shift+Enter 换行)"
              variant="borderless"
              autoSize={{ minRows: 2, maxRows: 5 }}
              style={{ fontSize: 14, padding: 0 }}
            />

            <Flex justify="space-between" align="center" style={{ marginTop: 8 }}>
              <Space size="small" align="center">
                <Tag
                  color="purple"
                  style={{ borderRadius: 10, cursor: 'pointer', fontSize: 11, margin: 0 }}
                  onClick={() => {
                    setInputMessage((prev) => prev + '@')
                    setMentionOpen(true)
                    inputRef.current?.focus()
                  }}
                >
                  @ 召唤 Skill
                </Tag>
                <Text type="secondary" style={{ fontSize: 11 }}>按 Enter 发送，Shift+Enter 换行</Text>
              </Space>

              {isStreaming ? (
                <Button size="small" danger shape="circle" icon={<StopOutlined />} onClick={handleStopStream} />
              ) : (
                <Button
                  size="small"
                  type="primary"
                  shape="circle"
                  icon={<SendOutlined style={{ fontSize: 12 }} />}
                  onClick={() => handleSendMessage()}
                  disabled={!inputMessage.trim()}
                  style={{ background: inputMessage.trim() ? '#1677ff' : '#d1d5db', border: 'none' }}
                />
              )}
            </Flex>
          </div>
        </div>
      </Flex>
    </Flex>
  )
}
