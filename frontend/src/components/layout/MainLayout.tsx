import { useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Dropdown, Avatar, theme, Tooltip, Space } from 'antd'
import { LogoutOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { useAuthStore } from '@/store/auth'
import { useMenuItems } from './menuConfig'
import Breadcrumbs from './Breadcrumbs'
import GlobalSearch from './GlobalSearch'
import NotificationBell from './NotificationBell'
import ThemeSwitcher from './ThemeSwitcher'

const { Header, Sider, Content } = Layout

export default function MainLayout() {
  const [isCollapsed, setIsCollapsed] = useState(false)
  const [isHovered, setIsHovered] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const userInfo = useAuthStore((s) => s.userInfo)
  const logout = useAuthStore((s) => s.logout)
  const { token: themeToken } = theme.useToken()
  // 菜单树：由 appRoutes 派生并按当前用户权限过滤（admin 全显）
  const menuItems = useMenuItems()

  // 当前活跃的一级分组（用于重置 openKeys）
  const pathParts = location.pathname.split('/').filter(Boolean)
  const activeGroup = pathParts.length > 0 ? [pathParts[0]] : []

  // openKeys 手动管理，折叠/离开时重置为仅当前活跃项
  const [openKeys, setOpenKeys] = useState<string[]>(activeGroup)

  // 视觉折叠态：锁定折叠 且 未 hover 时才真正折叠
  const visualCollapsed = isCollapsed && !isHovered

  const selectedKeys = [location.pathname]

  // ── 折叠按钮点击 ──
  const handleCollapseToggle = () => {
    if (isCollapsed) {
      setIsCollapsed(false)
      setOpenKeys(activeGroup)
    } else {
      setIsCollapsed(true)
      setIsHovered(false)
      // 折叠时只保留当前活跃分组，避免 hover 唤醒时一堆展开
      setOpenKeys(activeGroup)
    }
  }

  // ── Hover 进入：临时展开 ──
  const handleMouseEnter = () => {
    if (isCollapsed) {
      setIsHovered(true)
      setOpenKeys(activeGroup)
    }
  }

  // ── Hover 离开：收回，重置 openKeys ──
  const handleMouseLeave = () => {
    if (isCollapsed) {
      setIsHovered(false)
      setOpenKeys(activeGroup)
    }
  }

  const onMenuClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key)
  }

  const userMenu: MenuProps['items'] = [
    { key: 'logout', label: '退出登录', icon: <LogoutOutlined />, danger: true },
  ]

  const handleUserMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'logout') {
      logout()
      navigate('/login')
    }
  }

  return (
    // 锁死视口高度：侧栏菜单再长也只在侧栏内滚，不把主内容顶出屏幕
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      {/* ── 侧边栏 ── */}
      <Sider
        trigger={null}
        collapsible
        collapsed={visualCollapsed}
        width={240}
        collapsedWidth={64}
        theme="dark"
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        style={{
          transition: 'all 0.3s cubic-bezier(0.645, 0.045, 0.355, 1)',
          overflow: 'hidden',
          height: '100vh',
        }}
      >
        {/* antd 会包一层 .ant-layout-sider-children，flex 壳必须放在这里面 */}
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        {/* Logo */}
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            padding: '0 20px',
            gap: 10,
            overflow: 'hidden',
            flexShrink: 0,
          }}
        >
          <div
            style={{
              width: 28, height: 28, borderRadius: 6, flexShrink: 0,
              background: 'rgba(255,255,255,0.15)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: '#fff', fontWeight: 700, fontSize: 14,
            }}
          >
            O
          </div>
          <span
            style={{
              color: '#fff', fontWeight: 700, fontSize: 16,
              whiteSpace: 'nowrap',
              opacity: visualCollapsed ? 0 : 1,
              transform: visualCollapsed ? 'translateX(-6px)' : 'translateX(0)',
              transition: 'opacity 0.25s cubic-bezier(0.645, 0.045, 0.355, 1), transform 0.25s cubic-bezier(0.645, 0.045, 0.355, 1)',
            }}
          >
            OpsPilot
          </span>
        </div>

        {/* 菜单：独立滚动区（minHeight:0 允许 flex 子项收缩） */}
        <div
          className="sider-menu-scroll"
          style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}
        >
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={selectedKeys}
            openKeys={openKeys}
            onOpenChange={(keys) => setOpenKeys(keys)}
            items={menuItems}
            onClick={onMenuClick}
            inlineCollapsed={visualCollapsed}
          />
        </div>

        {/* ── 底部折叠按钮 ── */}
        <div
          style={{
            borderTop: '1px solid rgba(255,255,255,0.08)',
            padding: '12px 0',
            flexShrink: 0,
          }}
        >
          <Tooltip
            title={isCollapsed ? '展开侧栏' : '收起侧栏'}
            placement="right"
            mouseEnterDelay={0.5}
          >
            <div
              onClick={handleCollapseToggle}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '8px 20px',
                cursor: 'pointer',
                color: 'rgba(255,255,255,0.45)',
                transition: 'color 0.15s, background 0.15s',
                borderRadius: 6,
                margin: '0 8px',
                userSelect: 'none',
              }}
              onMouseEnter={(e) => {
                ;(e.currentTarget as HTMLElement).style.color = 'rgba(255,255,255,0.85)'
                ;(e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.06)'
              }}
              onMouseLeave={(e) => {
                ;(e.currentTarget as HTMLElement).style.color = 'rgba(255,255,255,0.45)'
                ;(e.currentTarget as HTMLElement).style.background = 'transparent'
              }}
            >
              {/* 图标：折叠态 → 右箭头，展开态 → 左箭头 */}
              <span style={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>
                {isCollapsed ? <RightOutlined /> : <LeftOutlined />}
              </span>
              <span
                style={{
                  fontSize: 13,
                  whiteSpace: 'nowrap',
                  opacity: visualCollapsed ? 0 : 1,
                  transform: visualCollapsed ? 'translateX(-6px)' : 'translateX(0)',
                  transition: 'opacity 0.25s cubic-bezier(0.645, 0.045, 0.355, 1), transform 0.25s cubic-bezier(0.645, 0.045, 0.355, 1)',
                }}
              >
                {isCollapsed ? '展开侧栏' : '收起侧栏'}
              </span>
            </div>
          </Tooltip>
        </div>
        </div>
      </Sider>

      {/* ── 主体：与侧栏同高，内容区独立滚动 ── */}
      <Layout style={{ height: '100vh', overflow: 'hidden', minWidth: 0 }}>
        <Header
          style={{
            padding: '0 24px',
            background: themeToken.colorBgContainer,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: `1px solid ${themeToken.colorBorderSecondary}`,
            flexShrink: 0,
            lineHeight: '64px',
          }}
        >
          {/* 左：面包屑 */}
          <Breadcrumbs />

          {/* 右：搜索 + 铃铛 + 用户 */}
          <Space size={16}>
            <GlobalSearch />
            <ThemeSwitcher />
            <NotificationBell />
            <Dropdown menu={{ items: userMenu, onClick: handleUserMenuClick }}>
              <div style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Avatar size="small" style={{ backgroundColor: themeToken.colorPrimary }}>
                  {userInfo?.username?.charAt(0).toUpperCase()}
                </Avatar>
                <span>{userInfo?.username}</span>
              </div>
            </Dropdown>
          </Space>
        </Header>
        <Content
          style={{
            margin: 24,
            padding: 24,
            background: themeToken.colorBgContainer,
            borderRadius: themeToken.borderRadiusLG,
            minHeight: 0,
            overflow: 'auto',
          }}
        >
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
