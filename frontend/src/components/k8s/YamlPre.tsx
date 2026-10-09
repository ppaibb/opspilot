import { useState } from 'react'
import { Button, App } from 'antd'
import { CopyOutlined, CheckOutlined } from '@ant-design/icons'

/**
 * 只读 YAML / JSON 展示块：复制按钮 + 限高滚动。
 * 刻意不引语法高亮库（AGENTS.md：新增依赖先问），保持零依赖。
 */
export default function YamlPre({ text, maxHeight = '75vh' }: { text: string; maxHeight?: string }) {
  const { message } = App.useApp()
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      message.success('已复制到剪贴板')
      setTimeout(() => setCopied(false), 1500)
    } catch {
      message.warning('浏览器不支持自动复制，请手动选择')
    }
  }

  return (
    <div>
      <div style={{ textAlign: 'right', marginBottom: 4 }}>
        <Button size="small" type="text" icon={copied ? <CheckOutlined /> : <CopyOutlined />} onClick={copy}>
          {copied ? '已复制' : '复制'}
        </Button>
      </div>
      <pre style={{
        background: '#fafafa', padding: 12, borderRadius: 6, fontSize: 12,
        whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight, overflow: 'auto',
      }}>
        {text}
      </pre>
    </div>
  )
}
