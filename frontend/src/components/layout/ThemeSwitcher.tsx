/** 全局主题切换：图标按钮 + 下拉三项（浅色 / 深色 / 跟随系统），与铃铛同一交互模式 */
import { Dropdown, Button, Flex } from 'antd'
import type { MenuProps } from 'antd'
import {
  SunOutlined, MoonOutlined, DesktopOutlined, CheckOutlined,
} from '@ant-design/icons'
import { useTheme, type ThemeMode } from '@/theme'

const OPTIONS: { key: ThemeMode; label: string; icon: React.ReactNode }[] = [
  { key: 'light', label: '浅色', icon: <SunOutlined /> },
  { key: 'dark', label: '深色', icon: <MoonOutlined /> },
  { key: 'system', label: '跟随系统', icon: <DesktopOutlined /> },
]

export default function ThemeSwitcher() {
  const { mode, setMode } = useTheme()

  const items: MenuProps['items'] = OPTIONS.map((o) => ({
    key: o.key,
    label: (
      <Flex align="center" gap={8} style={{ minWidth: 120 }}>
        {o.icon}
        <span style={{ flex: 1 }}>{o.label}</span>
        {mode === o.key ? <CheckOutlined style={{ color: '#1677ff' }} /> : null}
      </Flex>
    ),
  }))

  const TriggerIcon =
    mode === 'dark' ? <MoonOutlined /> : mode === 'light' ? <SunOutlined /> : <DesktopOutlined />

  return (
    <Dropdown
      menu={{ items, onClick: ({ key }) => setMode(key as ThemeMode) }}
      placement="bottomRight"
      trigger={['click']}
    >
      <Button
        type="text"
        icon={TriggerIcon}
        style={{ fontSize: 16 }}
        aria-label="切换主题"
      />
    </Dropdown>
  )
}
