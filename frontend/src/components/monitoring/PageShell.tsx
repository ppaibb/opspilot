/**
 * 监控中心五页共用壳：页头（标题 + 状态句 + 主操作）/ 筛选条（左筛右操一行）/ 主次分区（约 2/3 + 1/3）。
 * 颜色一律走 CSS 变量，深浅主题自动适配。
 */
import type { ReactNode } from 'react'
import { Card, Col, Flex, Row, Space, Typography } from 'antd'

const { Title, Text } = Typography

export function PageHeader({
  title,
  subtitle,
  extra,
}: {
  title: string
  subtitle?: ReactNode
  extra?: ReactNode
}) {
  return (
    <Flex justify="space-between" align="center" style={{ marginBottom: 8 }} wrap gap="small">
      <Space size={12}>
        <Title level={5} style={{ margin: 0, color: 'var(--ink)' }}>{title}</Title>
        {subtitle ? (
          <Text type="secondary" style={{ fontSize: 12, color: 'var(--ink-2)' }}>{subtitle}</Text>
        ) : null}
      </Space>
      {extra ? <Space size={12} wrap>{extra}</Space> : null}
    </Flex>
  )
}

export function FilterBar({ left, right }: { left?: ReactNode; right?: ReactNode }) {
  return (
    <Flex
      justify="space-between"
      align="center"
      wrap
      gap="small"
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--line)',
        borderRadius: 8,
        padding: '8px 12px',
        marginBottom: 8,
      }}
    >
      <Space size={8} wrap>{left}</Space>
      <Space size={8} wrap>{right}</Space>
    </Flex>
  )
}

/** 轻量下划线 Tab（与实时看板页头切换一致，替代 Segmented 块感）。 */
export function UnderlineTabs<T extends string>({
  value,
  onChange,
  items,
}: {
  value: T
  onChange: (v: T) => void
  items: { key: T; label: ReactNode }[]
}) {
  return (
    <Space size={4}>
      {items.map((m) => {
        const active = value === m.key
        return (
          <button
            key={m.key}
            type="button"
            onClick={() => onChange(m.key)}
            style={{
              height: 32,
              padding: '0 10px',
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 13,
              fontWeight: active ? 600 : 400,
              color: active ? 'var(--ink, #0f172a)' : 'var(--ink-3, #94a3b8)',
              borderBottom: active ? '2px solid var(--accent, #2563eb)' : '2px solid transparent',
              borderRadius: 0,
            }}
          >
            {m.label}
          </button>
        )
      })}
    </Space>
  )
}

/** 主内容约 2/3 + 侧栏 1/3（说明 / 快捷入口）。窄屏自动堆叠。 */
export function MainSplit({ main, side }: { main: ReactNode; side: ReactNode }) {
  return (
    <Row gutter={[8, 8]}>
      <Col xs={24} lg={16}>{main}</Col>
      <Col xs={24} lg={8}>{side}</Col>
    </Row>
  )
}

export function SideCard({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <Card
      title={title}
      size="small"
      extra={extra}
      styles={{ body: { background: 'var(--bg-card)', color: 'var(--ink-2)' } }}
    >
      {children}
    </Card>
  )
}
