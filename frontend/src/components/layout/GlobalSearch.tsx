import { useState, useEffect, useMemo } from 'react'
import { AutoComplete, Input, Tag } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useFlatRoutes } from './menuConfig'

/** 全局搜索：快速跳转页面，支持 Ctrl+K / Cmd+K 唤起 */
export default function GlobalSearch() {
  const navigate = useNavigate()
  // 数据源：按当前用户权限过滤后的扁平路由，避免搜出无权限入口
  const routes = useFlatRoutes()
  const [value, setValue] = useState('')
  const [open, setOpen] = useState(false)

  // Ctrl+K / Cmd+K 聚焦
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        const el = document.getElementById('global-search-input')
        el?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const options = useMemo(() => {
    const kw = value.trim().toLowerCase()
    const matched = kw
      ? routes.filter((r) => r.label.toLowerCase().includes(kw) || (r.groupLabel ?? '').toLowerCase().includes(kw))
      : routes
    return matched.slice(0, 8).map((r) => ({
      value: r.path,
      label: (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>{r.label}</span>
          {r.groupLabel && <Tag style={{ marginInlineEnd: 0 }}>{r.groupLabel}</Tag>}
        </div>
      ),
    }))
  }, [value, routes])

  const handleSelect = (path: string) => {
    navigate(path)
    setValue('')
    setOpen(false)
  }

  return (
    <AutoComplete
      value={value}
      options={options}
      open={open}
      onDropdownVisibleChange={setOpen}
      onSearch={setValue}
      onSelect={handleSelect}
      style={{ width: 260 }}
      popupMatchSelectWidth={280}
    >
      <Input
        id="global-search-input"
        prefix={<SearchOutlined style={{ color: '#9ca3af' }} />}
        suffix={<Tag style={{ marginInlineEnd: 0, fontSize: 11, color: '#9ca3af' }}>Ctrl K</Tag>}
        placeholder="搜索页面…"
        allowClear
      />
    </AutoComplete>
  )
}
