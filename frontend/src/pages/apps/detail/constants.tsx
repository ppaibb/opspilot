import {
  DesktopOutlined, HddOutlined, GlobalOutlined, SafetyCertificateOutlined,
  PlayCircleOutlined, PauseCircleOutlined, ReloadOutlined, SearchOutlined,
} from '@ant-design/icons'

export const ENV_COLOR: Record<string, string> = { prod: 'red', test: 'blue', dev: 'default', mixed: 'purple' }
export const ENV_LABEL: Record<string, string> = { prod: '生产', test: '测试', dev: '开发', mixed: '混合' }
export const STATUS_COLOR: Record<string, 'success' | 'processing' | 'error' | 'default' | 'warning'> = { active: 'success', maintenance: 'warning', offline: 'default' }
export const STATUS_LABEL: Record<string, string> = { active: '运行中', maintenance: '维护中', offline: '已下线' }
export const HEALTH_COLOR: Record<string, 'success' | 'warning' | 'error' | 'default'> = {
  healthy: 'success', warning: 'warning', degraded: 'error', offline: 'default', unknown: 'default',
}
export const RESOURCE_TYPE_LABEL: Record<string, { icon: React.ReactNode; label: string; color: string }> = {
  host:        { icon: <DesktopOutlined />,           label: '主机', color: 'geekblue' },
  vm:          { icon: <HddOutlined />,               label: '虚机', color: 'purple' },
  domain:      { icon: <GlobalOutlined />,            label: '域名', color: 'cyan' },
  certificate: { icon: <SafetyCertificateOutlined />, label: '证书', color: 'green' },
}
export const COMPONENT_STATUS_COLOR: Record<string, 'success' | 'warning' | 'error' | 'default'> = {
  active: 'success', running: 'success', stopped: 'default', offline: 'error', maintenance: 'warning', unknown: 'warning',
}
export const COMPONENT_STATUS_LABEL: Record<string, string> = {
  active: '运行中', running: '运行中', stopped: '已停止', offline: '已离线', maintenance: '维护中', unknown: '未知',
}

export const DEPLOY_TYPES = [
  { value: 'systemd', label: 'Systemd' },
  { value: 'supervisor', label: 'Supervisor' },
  { value: 'docker', label: 'Docker 容器' },
  { value: 'docker_compose', label: 'Docker Compose 服务' },
  { value: 'kubernetes', label: 'Kubernetes（纳管）', disabled: true },
  { value: 'helm', label: 'Helm（纳管）', disabled: true },
  { value: 'external', label: '外部托管' },
]

export const DISCOVERY_TYPES = [
  { value: 'docker', label: 'Docker' },
  { value: 'docker_compose', label: 'Docker Compose' },
  { value: 'supervisor', label: 'Supervisor' },
  { value: 'systemd', label: 'Systemd' },
]

export const EXPECTED_SCAN_ERRORS = [
  { pattern: /supervisorctl|supervisor/i, label: 'Supervisor' },
  { pattern: /docker(?:d|\s|:|$)|docker-compose/i, label: 'Docker' },
]

export const getSkippedEnvironment = (error: string) => {
  if (!/command not found|not found|no such file/i.test(error)) return null
  return EXPECTED_SCAN_ERRORS.find(({ pattern }) => pattern.test(error))?.label || '运行环境'
}

export const controlActions = [
  { key: 'start',   label: '启动',    icon: <PlayCircleOutlined /> },
  { key: 'stop',    label: '停止',    icon: <PauseCircleOutlined /> },
  { key: 'restart', label: '重启',    icon: <ReloadOutlined /> },
  { key: 'status',  label: '刷新状态', icon: <SearchOutlined /> },
]
