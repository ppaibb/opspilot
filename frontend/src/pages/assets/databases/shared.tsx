import { Card, Statistic } from 'antd'

/** 概览统计小卡片（点击可联动筛选，与资产页概览卡视觉保持一致）。 */
export function StatCard({
  title, value, icon, color, onClick, active, suffix,
}: {
  title: string
  value: number
  icon: React.ReactNode
  color: string
  onClick?: () => void
  active?: boolean
  suffix?: string
}) {
  return (
    <Card
      size="small"
      hoverable={!!onClick}
      onClick={onClick}
      styles={{
        body: { padding: '12px 16px' },
        ...(active ? { border: `1px solid ${color}` } : {}),
      }}
    >
      <Statistic
        title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>{title}</span>}
        value={value}
        suffix={suffix ? <span style={{ fontSize: 12, color: '#8c8c8c' }}>{suffix}</span> : undefined}
        prefix={icon}
        valueStyle={{ fontWeight: 600, fontSize: 20, color: value > 0 ? color : 'inherit' }}
      />
    </Card>
  )
}
