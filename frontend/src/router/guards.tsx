/**
 * 路由守卫组件集合。
 *
 * - AuthGuard：登录态 + 登录后初始化（bootstrap）；
 *   getMe（非 401）或 RBAC metadata 失败 → 进入错误状态页（不放行），
 *   仅 risk-levels 失败可降级；401 交由 axios 拦截器跳转登录页；
 * - GuestGuard：已登录用户访问登录页时重定向首页；
 * - PermissionRoute：页面级 RBAC 守卫，无权限（且非管理员）渲染 403。
 */
import { useEffect, type ReactNode } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import axios from 'axios'
import { Button, Result, Spin } from 'antd'
import { useAuthStore, isAdminRole } from '@/store/auth'
import { usePermission } from '@/hooks/usePermission'
import { authApi, rbacApi } from '@/api'
import type { RiskLevelsResponse } from '@/types/api'

// ── 全屏/页面级加载态 ──
export function PageLoading() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 240 }}>
      <Spin size="large" />
    </div>
  )
}

// ── 登录后初始化：并行拉取用户信息/风险等级/RBAC 元数据 ──
async function bootstrap(): Promise<void> {
  const store = useAuthStore.getState()
  const [meRes, riskRes, rbacRes] = await Promise.allSettled([
    authApi.getMe(),
    authApi.getRiskLevels(),
    rbacApi.metadata(),
  ])

  const is401 = (e: unknown) => axios.isAxiosError(e) && e.response?.status === 401

  // 关键数据失败：标记错误态（bootstrapped 保持 false，不放行进入系统）
  if (meRes.status === 'rejected') {
    if (is401(meRes.reason)) return // 拦截器负责跳登录
    store.setBootstrapError(true)
    return
  }
  if (rbacRes.status === 'rejected') {
    if (is401(rbacRes.reason)) return
    store.setBootstrapError(true)
    return
  }

  // 用户信息
  const me = meRes.value as { id: number; username: string; role: string; name?: string; email?: string }
  store.setAuth(store.token!, { id: me.id, username: me.username, role: me.role })

  // 风险等级（失败可降级：风险确认按未知等级处理）。后端返回 {"levels": {...}} 信封
  if (riskRes.status === 'fulfilled' && riskRes.value) {
    const envelope = riskRes.value as RiskLevelsResponse
    store.setRiskLevels(envelope.levels ?? {})
  }

  // 当前用户的有效权限（基于 RBAC 元数据）
  const userId = useAuthStore.getState().userInfo?.id
  if (userId) {
    try {
      const access = await rbacApi.getUserAccess(userId) as { effective_permissions: Record<string, boolean> }
      store.setPermissions(access.effective_permissions ?? {})
    } catch {
      store.setPermissions({})
    }
  }

  store.setBootstrapped(true)
}

// ── bootstrap 失败状态页：重试或退出登录 ──
function BootstrapErrorPage() {
  const navigate = useNavigate()
  const resetBootstrapError = useAuthStore((s) => s.resetBootstrapError)
  const logout = useAuthStore((s) => s.logout)

  return (
    <Result
      status="error"
      title="初始化失败"
      subTitle="用户信息或权限数据加载失败，无法进入系统。请重试或重新登录。"
      extra={[
        <Button key="retry" type="primary" onClick={resetBootstrapError}>
          重试
        </Button>,
        <Button
          key="logout"
          onClick={() => {
            logout()
            navigate('/login')
          }}
        >
          退出登录
        </Button>,
      ]}
    />
  )
}

export function AuthGuard({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token)
  const bootstrapped = useAuthStore((s) => s.bootstrapped)
  const bootstrapError = useAuthStore((s) => s.bootstrapError)

  useEffect(() => {
    if (token && !bootstrapped && !bootstrapError) {
      bootstrap()
    }
  }, [token, bootstrapped, bootstrapError])

  if (!token) return <Navigate to="/login" replace />

  // 初始化失败：错误状态页（重试 / 退出登录），绝不放行
  if (bootstrapError) return <BootstrapErrorPage />

  // 初始化中：全屏加载（bootstrapped 未就绪时不会渲染子路由）
  if (!bootstrapped) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh' }}>
        <Spin size="large" tip="加载中..." />
      </div>
    )
  }

  return <>{children}</>
}

export function GuestGuard({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token)
  if (token) return <Navigate to="/" replace />
  return <>{children}</>
}

// ── 403 无权限页 ──
function ForbiddenPage() {
  const navigate = useNavigate()
  return (
    <Result
      status="403"
      title="403"
      subTitle="抱歉，你没有访问该页面的权限"
      extra={
        <Button type="primary" onClick={() => navigate('/')}>
          返回首页
        </Button>
      }
    />
  )
}

export function PermissionRoute({ permission, children }: { permission?: string; children: ReactNode }) {
  const role = useAuthStore((s) => s.userInfo?.role)
  const { has } = usePermission()

  if (permission && !isAdminRole(role) && !has(permission)) {
    return <ForbiddenPage />
  }
  return <>{children}</>
}

// ── 404 页面 ──
export function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <Result
      status="404"
      title="404"
      subTitle="抱歉，页面不存在或已被移除"
      extra={
        <Button type="primary" onClick={() => navigate('/')}>
          返回首页
        </Button>
      }
    />
  )
}
