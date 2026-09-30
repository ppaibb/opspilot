import { QueryClient, QueryClientProvider, QueryCache } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { ConfigProvider, App as AntApp, theme as antdTheme } from 'antd'
// 注意：必须用 antd/es 的 ESM 直达路径。antd/locale/zh_CN 是 CJS shim
// （module.exports = require('../lib/locale/zh_CN')，lib 版自带 __esModule 标记），
// rolldown-vite 预打包互操作不解包这层标记，default import 会拿到
// { __esModule, default: localeValues } 双层包装 → ConfigProvider 全部组件文案回落英文。
import zhCN from 'antd/es/locale/zh_CN'
import { useEffect } from 'react'
import { router } from '@/router'
import { useTheme, useThemeBootstrap } from '@/theme'

// 在 React 树外运行，不能用 hooks。用全局变量将 antd message 实例注入进来。
let _showError: ((msg: string, key?: string) => void) | null = null

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
  },
  queryCache: new QueryCache({
    onError: (error: unknown) => {
      if (!_showError) return
      const err = error as { response?: { status?: number }; code?: string; message?: string }
      const status = err?.response?.status
      if (!status) {
        // 网络错误 / 超时 / 后端不可达
        _showError('网络连接出现问题，请检查网络后重试', 'net-err')
      } else if (status === 502 || status === 503 || status === 504) {
        _showError('服务暂时不可用，请稍后重试', 'gw-err')
      } else if (status >= 500) {
        _showError('出了点问题，请刷新页面或联系管理员', 'srv-err')
      } else if (status === 403) {
        _showError('没有访问权限', 'forbidden')
      }
      // 404 通常是业务预期，不弹全局提示
    },
  }),
})

/** 插入 React 树内，订阅 antd message 实例并注册到全局 */
function GlobalErrorHandler() {
  const { message } = AntApp.useApp()
  useEffect(() => {
    _showError = (msg, key) => message.error({ content: msg, key, duration: 5 })
    return () => { _showError = null }
  }, [message])
  return null
}

export default function App() {
  useThemeBootstrap()
  const { resolved } = useTheme()
  const dark = resolved === 'dark'
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: dark ? '#4ea3ff' : '#1677ff',
          colorBgContainer: dark ? '#111a2b' : '#ffffff',
          colorBgLayout: dark ? '#0b1220' : '#f4f7fb',
          colorText: dark ? '#e8eefc' : '#0f172a',
          colorBorder: dark ? '#243049' : '#e2e8f0',
          borderRadius: 6,
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <GlobalErrorHandler />
          <RouterProvider router={router} />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  )
}
