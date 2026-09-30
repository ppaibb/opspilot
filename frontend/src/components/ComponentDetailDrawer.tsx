import { Drawer, Descriptions, Tabs, Timeline, Button, Space, Tag, Empty, Spin, Typography, Alert } from 'antd'
import {
  CloseOutlined, PlayCircleOutlined, PauseCircleOutlined, ReloadOutlined,
  DeleteOutlined, CheckCircleOutlined, CloseCircleOutlined, ClockCircleOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import type { ApplicationComponentResponse } from '@/types/api'
import client from '@/api/client'

const { Text, Paragraph } = Typography

interface ComponentDetailDrawerProps {
  open: boolean
  onClose: () => void
  component: ApplicationComponentResponse | null
  appId: string
  onAction?: (componentId: string, action: string) => void
  onDelete?: (componentId: string) => void
}

const DEPLOY_TYPE_LABEL: Record<string, string> = {
  docker: 'Docker 容器',
  docker_compose: 'Docker Compose',
  supervisor: 'Supervisor',
  systemd: 'Systemd',
  binary: '二进制程序',
}

const CONTROL_METHOD_LABEL: Record<string, string> = {
  docker: 'Docker CLI',
  docker_compose: 'Docker Compose CLI',
  supervisor: 'supervisorctl',
  systemd: 'systemctl',
  manual: '手动管理',
}

const STATUS_CONFIG = {
  running: { color: 'success', icon: <CheckCircleOutlined />, text: '运行中' },
  stopped: { color: 'default', icon: <PauseCircleOutlined />, text: '已停止' },
  unknown: { color: 'warning', icon: <ClockCircleOutlined />, text: '未知' },
}

/** GET /applications/{appId}/components/{componentId}/detail 响应：
 * 在组件基础字段之上附带关联资源与控制历史（后端应用组件控制详情接口）。 */
type ComponentDetail = ApplicationComponentResponse & {
  last_control_duration_ms?: number
  compose_project?: string
}

interface ComponentDetailResponse {
  component?: ComponentDetail
  bound_resources?: ComponentBoundResource[]
  control_history?: ComponentControlHistoryItem[]
}

/** 关联资源条目（detail 接口的 bound_resources 数组元素）。 */
interface ComponentBoundResource {
  type?: string
  id?: number | string
  name?: string
}

/** 控制历史条目（detail 接口的 control_history 数组元素）。 */
interface ComponentControlHistoryItem {
  action?: string
  result?: string
  duration_ms?: number
  executed_at?: string
  operator?: string
  output?: string
}

export default function ComponentDetailDrawer({
  open,
  onClose,
  component,
  appId,
  onAction,
  onDelete,
}: ComponentDetailDrawerProps) {
  // 获取详细信息（包含控制历史等）
  const { data: detailData, isLoading } = useQuery({
    queryKey: ['component-detail', appId, component?.component_id],
    queryFn: async (): Promise<ComponentDetailResponse | null> =>
      client
        .get<ComponentDetailResponse>(
          `/applications/${appId}/components/${component?.component_id}/detail`,
        )
        .then((res) => res.data)
        .catch(() => null), // 如果接口不存在，降级为使用传入的 component 数据
    enabled: !!component && open,
  })

  if (!component) return null

  const detail: ComponentDetail = detailData?.component || component
  const boundResources = detailData?.bound_resources || []
  const controlHistory = detailData?.control_history || []

  const statusConfig = STATUS_CONFIG[detail.status as keyof typeof STATUS_CONFIG] || STATUS_CONFIG.unknown

  return (
    <Drawer
      title={
        <Space>
          <span>部署组件详情</span>
          <Tag color="blue">{DEPLOY_TYPE_LABEL[detail.deployment_type] || detail.deployment_type}</Tag>
        </Space>
      }
      open={open}
      onClose={onClose}
      width={720}
      extra={
        <Button icon={<CloseOutlined />} onClick={onClose}>
          关闭
        </Button>
      }
      footer={
        <Space>
          {onAction && (
            <>
              <Button
                icon={<PlayCircleOutlined />}
                onClick={() => onAction(detail.component_id, 'start')}
                disabled={detail.status === 'running'}
              >
                启动
              </Button>
              <Button
                icon={<PauseCircleOutlined />}
                onClick={() => onAction(detail.component_id, 'stop')}
                disabled={detail.status === 'stopped'}
              >
                停止
              </Button>
              <Button
                icon={<ReloadOutlined />}
                onClick={() => onAction(detail.component_id, 'status')}
              >
                刷新状态
              </Button>
            </>
          )}
          {onDelete && (
            <Button
              danger
              icon={<DeleteOutlined />}
              onClick={() => onDelete(detail.component_id)}
            >
              删除
            </Button>
          )}
        </Space>
      }
    >
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin size="large" tip="加载详情..." />
        </div>
      ) : (
        <Tabs
          defaultActiveKey="basic"
          items={[
            {
              key: 'basic',
              label: '基本信息',
              children: (
                <Descriptions column={1} bordered size="small">
                  <Descriptions.Item label="组件名称">
                    <Text strong>{detail.name}</Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="组件 ID">
                    <Text code copyable>{detail.component_id}</Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="部署类型">
                    {DEPLOY_TYPE_LABEL[detail.deployment_type] || detail.deployment_type}
                  </Descriptions.Item>
                  <Descriptions.Item label="控制方式">
                    {CONTROL_METHOD_LABEL[detail.control_method] || detail.control_method}
                  </Descriptions.Item>
                  <Descriptions.Item label="运行状态">
                    <Space>
                      {statusConfig.icon}
                      <Text type={statusConfig.color === 'success' ? 'success' : statusConfig.color === 'warning' ? 'warning' : undefined}>
                        {statusConfig.text}
                      </Text>
                    </Space>
                  </Descriptions.Item>
                  <Descriptions.Item label="控制标识">
                    <Paragraph
                      code
                      copyable
                      style={{ marginBottom: 0, wordBreak: 'break-all' }}
                    >
                      {detail.control_ref}
                    </Paragraph>
                  </Descriptions.Item>
                  {detail.description && (
                    <Descriptions.Item label="描述">
                      {detail.description}
                    </Descriptions.Item>
                  )}
                  {detail.owner && (
                    <Descriptions.Item label="负责人">
                      {detail.owner}
                    </Descriptions.Item>
                  )}
                  <Descriptions.Item label="创建时间">
                    {detail.created_at || '-'}
                  </Descriptions.Item>
                  {detail.last_control_at && (
                    <Descriptions.Item label="最近操作时间">
                      {detail.last_control_at}
                    </Descriptions.Item>
                  )}
                </Descriptions>
              ),
            },
            {
              key: 'resources',
              label: '关联资源',
              children: boundResources.length === 0 ? (
                <Empty description="无关联资源" />
              ) : (
                <Descriptions column={1} bordered size="small">
                  {boundResources.map((res, index: number) => (
                    <Descriptions.Item
                      key={index}
                      label={
                        res.type === 'host' ? '所属主机' :
                        res.type === 'vm' ? '虚拟机' : '资源'
                      }
                    >
                      <Space>
                        <Text strong>{res.name}</Text>
                        <Text type="secondary" code>{res.id}</Text>
                      </Space>
                    </Descriptions.Item>
                  ))}
                  {detail.compose_project && (
                    <Descriptions.Item label="Compose 项目">
                      <Tag color="blue">{detail.compose_project}</Tag>
                    </Descriptions.Item>
                  )}
                </Descriptions>
              ),
            },
            {
              key: 'history',
              label: '控制记录',
              children: controlHistory.length === 0 ? (
                <Empty description="暂无控制记录">
                  {detail.last_control_action && (
                    <Alert
                      type="info"
                      message="最近一次操作"
                      description={
                        <Space direction="vertical" size={2}>
                          <Text>
                            操作：{detail.last_control_action} |
                            结果：{detail.last_control_result === 'success' ? '成功' : '失败'}
                          </Text>
                          {detail.last_control_duration_ms && (
                            <Text type="secondary">
                              耗时：{detail.last_control_duration_ms}ms
                            </Text>
                          )}
                        </Space>
                      }
                      style={{ marginTop: 16 }}
                    />
                  )}
                </Empty>
              ) : (
                <Timeline
                  items={controlHistory.map((item) => ({
                    dot: item.result === 'success' ?
                      <CheckCircleOutlined style={{ color: '#52c41a' }} /> :
                      <CloseCircleOutlined style={{ color: '#ff4d4f' }} />,
                    children: (
                      <Space direction="vertical" size={4}>
                        <Space>
                          <Tag color={
                            item.action === 'start' ? 'green' :
                            item.action === 'stop' ? 'orange' :
                            item.action === 'restart' ? 'blue' : 'default'
                          }>
                            {item.action}
                          </Tag>
                          <Text type={item.result === 'success' ? 'success' : 'danger'}>
                            {item.result === 'success' ? '成功' : '失败'}
                          </Text>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {item.duration_ms}ms
                          </Text>
                        </Space>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {item.executed_at} | {item.operator}
                        </Text>
                        {item.output && (
                          <Paragraph
                            code
                            copyable
                            ellipsis={{ rows: 3, expandable: true }}
                            style={{ marginBottom: 0, fontSize: 11 }}
                          >
                            {item.output}
                          </Paragraph>
                        )}
                      </Space>
                    ),
                  }))}
                />
              ),
            },
          ]}
        />
      )}
    </Drawer>
  )
}
