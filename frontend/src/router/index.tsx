/**
 * 路由表：由 routes.ts 的 appRoutes 元数据统一生成。
 *
 * - 守卫组件（AuthGuard/GuestGuard/PermissionRoute）见 guards.tsx；
 * - 页面组件经 React.lazy 懒加载 + Suspense 兜底动画；
 * - 显式 `path="*"` 404 兜底。
 */
import { Suspense, lazy, type ReactNode } from 'react'
import { createBrowserRouter } from 'react-router-dom'
import MainLayout from '@/components/layout/MainLayout'
import { appRoutes, loginRoute, type AppRouteMeta } from './routes'
import { AuthGuard, GuestGuard, PermissionRoute, NotFoundPage, PageLoading } from './guards'

/** 把懒加载工厂包装成带 Suspense 兜底的元素（router 创建时各调用一次） */
function lazyElement(meta: AppRouteMeta): ReactNode {
  const Comp = lazy(meta.load)
  return (
    <Suspense fallback={<PageLoading />}>
      <Comp />
    </Suspense>
  )
}

export const router = createBrowserRouter([
  {
    path: loginRoute.path,
    element: <GuestGuard>{lazyElement(loginRoute)}</GuestGuard>,
  },
  {
    path: '/',
    element: (
      <AuthGuard>
        <MainLayout />
      </AuthGuard>
    ),
    children: [
      // 由 appRoutes 生成全部业务路由（首页为 index 路由，页面级权限由 PermissionRoute 把关）
      ...appRoutes.map((r) =>
        r.path === '/'
          ? {
              index: true as const,
              element: (
                <PermissionRoute permission={r.permission}>{lazyElement(r)}</PermissionRoute>
              ),
            }
          : {
              path: r.path.slice(1),
              element: (
                <PermissionRoute permission={r.permission}>{lazyElement(r)}</PermissionRoute>
              ),
            },
      ),
      // 显式 404 兜底
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
