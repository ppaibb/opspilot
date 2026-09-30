import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  userInfo: { id: number; username: string; role: string; name?: string; email?: string } | null
  permissions: Record<string, boolean> | null
  riskLevels: Record<string, 'high' | 'medium' | 'low'> | null
  bootstrapped: boolean  // 登录后权限是否已加载
  bootstrapError: boolean // 登录后初始化（me/rbac metadata）是否失败，失败时不放行进入系统

  setAuth: (token: string, user: { id: number; username: string; role: string }) => void
  setPermissions: (perms: Record<string, boolean>) => void
  setRiskLevels: (levels: Record<string, 'high' | 'medium' | 'low'>) => void
  setBootstrapped: (v: boolean) => void
  setBootstrapError: (v: boolean) => void
  /** 清除 bootstrap 错误态（供「重试」按钮重新触发初始化） */
  resetBootstrapError: () => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      userInfo: null,
      permissions: null,
      riskLevels: null,
      bootstrapped: false,
      bootstrapError: false,

      setAuth: (token, userInfo) => set({ token, userInfo, bootstrapped: false }),
      setPermissions: (permissions) => set({ permissions }),
      setRiskLevels: (riskLevels) => set({ riskLevels }),
      setBootstrapped: (bootstrapped) => set({ bootstrapped }),
      setBootstrapError: (bootstrapError) => set({ bootstrapError }),
      resetBootstrapError: () => set({ bootstrapError: false }),
      logout: () =>
        set({
          token: null,
          userInfo: null,
          permissions: null,
          riskLevels: null,
          bootstrapped: false,
          bootstrapError: false,
        }),
    }),
    {
      name: 'opspilot-auth',
      partialize: (state) => ({ token: state.token, userInfo: state.userInfo }),
    },
  ),
)

/** 后端管理员角色的所有别名（与 backend ROLE_ALIAS_MAP 对齐，含中文角色名） */
const ADMIN_ROLES = new Set(['admin', 'administrator', '管理员'])

/** 判断当前角色是否为管理员（管理员豁免页面级权限检查） */
export function isAdminRole(role?: string | null): boolean {
  if (!role) return false
  return ADMIN_ROLES.has(role.trim().toLowerCase())
}
