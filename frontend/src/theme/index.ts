/**
 * 全局主题：浅色 / 深色 / 跟随系统（zustand 全局单例，切换即时全站生效）。
 */
import { create } from 'zustand'
import { useEffect } from 'react'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'opspilot-theme'

function readMode(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    /* ignore */
  }
  return 'system'
}

function systemDark() {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

function resolve(mode: ThemeMode): ResolvedTheme {
  return mode === 'system' ? (systemDark() ? 'dark' : 'light') : mode
}

function apply(resolved: ResolvedTheme) {
  document.documentElement.dataset.theme = resolved
  document.documentElement.style.colorScheme = resolved
}

interface ThemeState {
  mode: ThemeMode
  resolved: ResolvedTheme
  setMode: (mode: ThemeMode) => void
  /** 跟随系统时由外部调用刷新 */
  refreshSystem: () => void
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  mode: readMode(),
  resolved: resolve(readMode()),
  setMode: (mode) => {
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch {
      /* ignore */
    }
    const resolved = resolve(mode)
    apply(resolved)
    set({ mode, resolved })
  },
  refreshSystem: () => {
    const { mode } = get()
    if (mode !== 'system') return
    const resolved = resolve(mode)
    apply(resolved)
    set({ resolved })
  },
}))

/** 组件入口调用一次：应用初始主题 + 监听系统切换 */
export function useThemeBootstrap() {
  const refreshSystem = useThemeStore((s) => s.refreshSystem)
  useEffect(() => {
    const { mode, resolved } = useThemeStore.getState()
    apply(resolved)
    if (mode !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => refreshSystem()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [refreshSystem])
}

/** 兼容旧调用 */
export function useTheme() {
  const mode = useThemeStore((s) => s.mode)
  const resolved = useThemeStore((s) => s.resolved)
  const setMode = useThemeStore((s) => s.setMode)
  return { mode, resolved, setMode }
}
