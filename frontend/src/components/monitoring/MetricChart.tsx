/**
 * 指标图公共组件：统一时间轴 / tooltip / loading / 空态 / 色板 / 字号。
 * HostMetricsDrawer 与 metrics 查询页共用，避免两套 ECharts 配置漂移。
 */
import { Empty, Flex, Spin, Typography } from 'antd'
import ReactECharts from 'echarts-for-react'
import { CHART_COLORS, cssColor } from './chartTheme'

const { Text } = Typography

const axisLabel = () => ({ color: cssColor('--ink-3', '#8c8c8c'), fontSize: 11 })
const splitLine = () => ({ lineStyle: { color: cssColor('--line', '#f0f0f0') } })

export interface MetricSeries {
  name: string
  values: (number | null)[]
  color?: string
}

export interface MetricChartProps {
  /** 类目时间轴（已格式化或 ISO 字符串）。 */
  times: string[]
  series: MetricSeries[]
  height?: number
  loading?: boolean
  unit?: string
  /** PromQL 口径说明，展示在图上方。 */
  subtitle?: string
  emptyText?: string
  /** 面积图（单序列主机指标常用）。 */
  area?: boolean
}

export default function MetricChart({
  times,
  series,
  height = 220,
  loading,
  unit = '',
  subtitle,
  emptyText = '暂无数据',
  area = false,
}: MetricChartProps) {
  const hasData = series.some((s) => s.values.some((v) => v != null))

  if (loading) {
    return (
      <Flex align="center" justify="center" style={{ height }}>
        <Spin />
      </Flex>
    )
  }
  if (!hasData) {
    return (
      <Flex align="center" justify="center" style={{ height }}>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />
      </Flex>
    )
  }

  const option = {
    grid: { left: 56, right: 24, top: subtitle ? 40 : 16, bottom: 32 },
    legend: series.length > 1 ? { type: 'scroll', top: 8, textStyle: { fontSize: 11 } } : undefined,
    tooltip: {
      trigger: 'axis',
      confine: true,
      valueFormatter: (v: number | null) => (v == null ? '-' : `${Number(v).toFixed(2)}${unit ? ` ${unit}` : ''}`),
    },
    xAxis: {
      type: 'category',
      data: times,
      boundaryGap: false,
      axisLine: { lineStyle: { color: cssColor('--line', '#e5e7eb') } },
      axisLabel: { ...axisLabel(), rotate: times.length > 8 ? 20 : 0 },
    },
    yAxis: {
      type: 'value',
      splitLine: splitLine(),
      axisLabel: { ...axisLabel(), formatter: unit ? `{value} ${unit}` : '{value}' },
    },
    series: series.map((s, idx) => {
      const color = s.color || CHART_COLORS[idx % CHART_COLORS.length]
      return {
        name: s.name,
        type: 'line',
        smooth: true,
        showSymbol: false,
        connectNulls: true,
        data: s.values,
        lineStyle: { color, width: 2 },
        areaStyle: area
          ? {
              color: {
                type: 'linear',
                x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [
                  { offset: 0, color: `${color}33` },
                  { offset: 1, color: `${color}05` },
                ],
              },
            }
          : undefined,
      }
    }),
  }

  return (
    <div>
      {subtitle ? (
        <Text type="secondary" style={{ display: 'block', marginBottom: 4, fontSize: 12 }}>
          口径：{subtitle}
        </Text>
      ) : null}
      <ReactECharts option={option} style={{ height: subtitle ? height - 18 : height }} notMerge lazyUpdate />
    </div>
  )
}
