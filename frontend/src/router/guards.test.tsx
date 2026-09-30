import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { App } from 'antd'
import { AuthGuard, GuestGuard, PermissionRoute } from './guards'
import { useAuthStore, isAdminRole } from '@/store/auth'

vi.mock('@/api', () => ({
  authApi: {
    getMe: vi.fn(),
    getRiskLevels: vi.fn(),
  },
  rbacApi: {
    metadata: vi.fn(),
    getUserAccess: vi.fn(),
  },
}))

function wrap(_ui: React.ReactNode, initialPath = '/') {
  return render(
    <App>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/login" element={<div>登录页</div>} />
          <Route path="/" element={<AuthGuard><div>系统首页</div></AuthGuard>} />
          <Route path="/settings/users" element={<PermissionRoute permission="module.settings.users"><div>用户管理</div></PermissionRoute>} />
          <Route path="/apps" element={<GuestGuard><div>登录表单</div></GuestGuard>} />
        </Routes>
      </MemoryRouter>
    </App>,
  )
}

describe('AuthGuard', () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: 'tk-1',
      userInfo: { id: 1, username: 'ops', role: '管理员' },
      permissions: {},
      bootstrapped: false,
      bootstrapError: false,
      riskLevels: {},
    } as never)
  })

  it('无 token 时重定向到登录页', async () => {
    useAuthStore.setState({ token: null, bootstrapped: false } as never)
    wrap(<></>)
    await waitFor(() => {
      expect(screen.getByText('登录页')).toBeTruthy()
    })
  })

  it('bootstrap 成功后放行子路由', async () => {
    const { authApi, rbacApi } = await import('@/api')
    vi.mocked(authApi.getMe).mockResolvedValue({ id: 1, username: 'ops', role: '管理员' })
    vi.mocked(authApi.getRiskLevels).mockResolvedValue({ levels: { 'assets:delete': 'high' } })
    vi.mocked(rbacApi.metadata).mockResolvedValue({ permissions: [] } as never)
    vi.mocked(rbacApi.getUserAccess).mockResolvedValue({ effective_permissions: { 'module.dashboard': true } })

    wrap(<></>)
    await waitFor(() => {
      expect(screen.getByText('系统首页')).toBeTruthy()
    })
    expect(useAuthStore.getState().bootstrapped).toBe(true)
    expect(useAuthStore.getState().permissions).toMatchObject({ 'module.dashboard': true })
  })

  it('getMe 失败（非 401）进入错误态且不放行', async () => {
    const { authApi } = await import('@/api')
    vi.mocked(authApi.getMe).mockRejectedValue(new Error('boom'))
    vi.mocked(authApi.getRiskLevels).mockResolvedValue({ levels: {} })
    const { rbacApi } = await import('@/api')
    vi.mocked(rbacApi.metadata).mockResolvedValue({ permissions: [] } as never)

    wrap(<></>)
    await waitFor(() => {
      expect(useAuthStore.getState().bootstrapError).toBe(true)
    })
    expect(screen.queryByText('系统首页')).toBeNull()
  })
})

describe('GuestGuard', () => {
  it('已登录访问登录页会被带走', async () => {
    useAuthStore.setState({
      token: 'tk-1',
      bootstrapped: true,
      bootstrapError: false,
      userInfo: { id: 1, username: 'ops', role: '管理员' },
      permissions: {},
    } as never)
    wrap(<></>, '/apps')
    await waitFor(() => {
      expect(screen.queryByText('登录表单')).toBeNull()
    })
  })
})

describe('PermissionRoute', () => {
  it('无权限时渲染 403 而不是页面', async () => {
    useAuthStore.setState({
      token: 'tk-1',
      bootstrapped: true,
      bootstrapError: false,
      userInfo: { id: 2, username: 'dev', role: '开发' },
      permissions: {},
    } as never)
    wrap(<></>, '/settings/users')
    await waitFor(() => {
      expect(screen.queryByText('用户管理')).toBeNull()
    })
  })

  it('管理员绕过权限键', () => {
    expect(isAdminRole('管理员')).toBe(true)
    expect(isAdminRole('admin')).toBe(true)
    expect(isAdminRole('开发')).toBe(false)
  })
})
