/**
 * MetricChart 共用常量：时间窗选项与色板。
 * 单独成文件，满足 react-refresh/only-export-components。
 */

/** 统一时间窗选项（分钟）。Host 抽屉 / 指标页 / 实时看板共用。 */
export const RANGE_OPTIONS: { label: string; value: string; minutes: number; step: number }[] = [
  { label: '5 分钟', value: '5m', minutes: 5, step: 15 },
  { label: '15 分钟', value: '15m', minutes: 15, step: 15 },
  { label: '1 小时', value: '1h', minutes: 60, step: 30 },
  { label: '3 小时', value: '3h', minutes: 180, step: 60 },
  { label: '6 小时', value: '6h', minutes: 360, step: 120 },
  { label: '24 小时', value: '24h', minutes: 1440, step: 300 },
  { label: '7 天', value: '7d', minutes: 10080, step: 1800 },
  { label: '30 天', value: '30d', minutes: 43200, step: 7200 },
]

/** 统一色板（与 Grafana 默认系列色接近，保证可读对比度）。 */
export const CHART_COLORS = ['#2f54eb', '#1677ff', '#52c41a', '#fa8c16', '#722ed1', '#eb2f96', '#13c2c2', '#a0d911']

/** 读 CSS 变量（深浅主题），失败回退固定色。 */
export function cssColor(name: string, fallback: string): string {
  if (typeof window === 'undefined' || !window.getComputedStyle) return fallback
  const v = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

/** 图表主题色（优先 CSS 变量 --chart-1..4）。 */
export function chartPalette(): string[] {
  return [
    cssColor('--chart-1', '#2563eb'),
    cssColor('--chart-2', '#14b8a6'),
    cssColor('--chart-3', '#f59e0b'),
    cssColor('--chart-4', '#a855f7'),
  ]
}
