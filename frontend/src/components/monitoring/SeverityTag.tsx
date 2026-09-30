/**
 * 严重度标签：图标 + 文案（不只靠颜色），满足可达性。
 */
import { Tag } from 'antd'
import { CloseCircleOutlined, WarningOutlined, InfoCircleOutlined, BellOutlined, EyeInvisibleOutlined } from '@ant-design/icons'

const MAP: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  critical: { color: 'red', icon: <CloseCircleOutlined />, label: '严重' },
  warning: { color: 'orange', icon: <WarningOutlined />, label: '警告' },
  info: { color: 'blue', icon: <InfoCircleOutlined />, label: '提示' },
  suppressed: { color: 'default', icon: <EyeInvisibleOutlined />, label: '已静默' },
}

export default function SeverityTag({ severity, showText = true }: { severity?: string; showText?: boolean }) {
  const key = (severity || '').toLowerCase()
  const cfg = MAP[key] || { color: 'default', icon: <BellOutlined />, label: severity || '未知' }
  return (
    <Tag color={cfg.color} icon={cfg.icon}>
      {showText ? cfg.label : (severity || '-').toUpperCase()}
    </Tag>
  )
}
