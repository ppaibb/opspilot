/**
 * 菜单 / 面包屑 / 全局搜索的派生层。
 *
 * 唯一数据源是 router/routes.tsx 的 appRoutes：
 * - menuItems 改为 useMenuItems() 按当前用户权限动态过滤（admin 全显）；
 * - flatRoutes 保持原导出名（面包屑用，不做权限过滤——用户已在页面内）；
 * - 全局搜索用 useFlatRoutes()（按权限过滤，避免搜出无权限入口）。
 */
import { useMemo, type ReactNode } from 'react'
import type { MenuProps } from 'antd'
import {
  DashboardOutlined,
  CloudServerOutlined,
  RobotOutlined,
  AppstoreOutlined,
  MonitorOutlined,
  ToolOutlined,
  SettingOutlined,
  AccountBookOutlined,
} from '@ant-design/icons'
import { appRoutes, type AppRouteMeta } from '@/router/routes'
import { useAuthStore, isAdminRole } from '@/store/auth'

export type MenuItem = Required<MenuProps>['items'][number]

/** 一级分组 key → 图标映射（保留原菜单视觉） */
const GROUP_ICONS: Record<string, ReactNode> = {
  assets: <CloudServerOutlined />,
  ai: <RobotOutlined />,
  apps: <AppstoreOutlined />,
  monitoring: <MonitorOutlined />,
  finance: <AccountBookOutlined />,
  tools: <ToolOutlined />,
  settings: <SettingOutlined />,
}

/** 一级分组 key → 中文名（由 appRoutes 派生，面包屑用） */
export const groupLabels: Record<string, string> = (() => {
  const map: Record<string, string> = {}
  for (const r of appRoutes) {
    if (r.groupKey && r.group && !(r.groupKey in map)) map[r.groupKey] = r.group
  }
  return map
})()

/** 当前用户对某权限键是否可见：未设防 / 管理员 / 显式授权 */
function isAllowed(permission: string | undefined, permissions: Record<string, boolean> | null, role?: string): boolean {
  if (!permission) return true
  if (isAdminRole(role)) return true
  return !!permissions?.[permission]
}

/** 由 appRoutes 构建侧边栏菜单树（保持原分组与图标映射） */
function buildMenuItems(permissions: Record<string, boolean> | null, role?: string): MenuItem[] {
  const tops: MenuItem[] = []
  const groups = new Map<string, MenuItem[]>()

  for (const r of appRoutes) {
    if (!r.menu || !isAllowed(r.permission, permissions, role)) continue
    if (!r.groupKey) {
      // 一级页面（如仪表盘）
      tops.push({ key: r.path, icon: <DashboardOutlined />, label: r.label })
    } else {
      const list = groups.get(r.groupKey) ?? []
      list.push({ key: r.path, label: r.label })
      groups.set(r.groupKey, list)
    }
  }

  // 分组按 appRoutes 中首次出现的顺序展开为子菜单
  for (const [groupKey, children] of groups) {
    tops.push({
      key: groupKey,
      icon: GROUP_ICONS[groupKey] ?? <AppstoreOutlined />,
      label: groupLabels[groupKey] ?? groupKey,
      children,
    })
  }
  return tops
}

/** 侧边栏菜单树（按当前用户权限动态过滤；admin 或未设 permission 的全显） */
export function useMenuItems(): MenuItem[] {
  const permissions = useAuthStore((s) => s.permissions)
  const role = useAuthStore((s) => s.userInfo?.role)
  return useMemo(() => buildMenuItems(permissions, role), [permissions, role])
}

export interface FlatRoute {
  path: string
  label: string
  group?: string
  groupLabel?: string
}

/** appRoutes 菜单项 → 扁平路由 */
function toFlat(r: AppRouteMeta): FlatRoute {
  return { path: r.path, label: r.label, group: r.groupKey, groupLabel: r.group || undefined }
}

/** 扁平化所有菜单叶子路由（面包屑用，保持导出名；不做权限过滤） */
export const flatRoutes: FlatRoute[] = appRoutes.filter((r) => r.menu).map(toFlat)

/** 按当前用户权限过滤后的扁平路由（全局搜索用，避免搜出无权限入口） */
export function useFlatRoutes(): FlatRoute[] {
  const permissions = useAuthStore((s) => s.permissions)
  const role = useAuthStore((s) => s.userInfo?.role)
  return useMemo(
    () => appRoutes.filter((r) => r.menu && isAllowed(r.permission, permissions, role)).map(toFlat),
    [permissions, role],
  )
}

/**
 * 根据当前路径匹配路由：精确匹配 → 动态段匹配（:clusterId / :appId 等）。
 * 只在注册过的菜单路由里做前缀回落，避免深层未注册路径拿不到分组名。
 */
function matchRoute(pathname: string): AppRouteMeta | null {
  const exact = appRoutes.find((r) => r.path === pathname)
  if (exact) return exact
  const segs = pathname.split('/').filter(Boolean)
  const dynamic = appRoutes.find((r) => {
    const parts = r.path.split('/').filter(Boolean)
    if (parts.length !== segs.length) return false
    return parts.every((p, i) => p.startsWith(':') || p === segs[i])
  })
  if (dynamic) return dynamic
  // 前缀回落：取命中的最长菜单路由（如 /apps/xxx/yyy → 应用列表）
  return (
    appRoutes
      .filter((r) => r.menu && r.path !== '/' && pathname.startsWith(`${r.path}/`))
      .sort((a, b) => b.path.length - a.path.length)[0] ?? null
  )
}

/** 根据当前路径返回面包屑节点（支持 /assets/clusters/:clusterId 等动态路由） */
export function resolveBreadcrumb(pathname: string): { label: string; path?: string }[] {
  if (pathname === '/') return [{ label: '仪表盘' }]
  const match = matchRoute(pathname)
  if (!match) return [{ label: '仪表盘', path: '/' }]
  const crumbs: { label: string; path?: string }[] = [{ label: '仪表盘', path: '/' }]
  if (match.group) crumbs.push({ label: match.group })
  crumbs.push({ label: match.label })
  return crumbs
}
