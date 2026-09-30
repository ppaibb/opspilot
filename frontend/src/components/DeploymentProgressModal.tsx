import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Modal, Progress, Typography, Alert, Space, Button } from 'antd'
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined } from '@ant-design/icons'
import client from '@/api/client'

const { Paragraph, Text } = Typography

interface DeploymentTask {
  task_id: string
  app_id: string
  task_type: string
  status: 'pending' | 'running' | 'success' | 'failed' | 'cancelled'
  progress: number
  current_step: string
  total_steps?: number
  result_message?: string
  output_log?: string
  created_at: string
  started_at?: string
  finished_at?: string
}

interface Props {
  taskId: string | null
  open: boolean
  onClose: () => void
  onSuccess?: () => void
}

export default function DeploymentProgressModal({ taskId, open, onClose, onSuccess }: Props) {
  const { data: task } = useQuery<DeploymentTask>({
    queryKey: ['deployment-task', taskId],
    queryFn: () => client.get<DeploymentTask>(`/applications/tasks/${taskId}`).then(r => r.data),
    refetchInterval: (query) => {
      // 如果任务未完成，每 2 秒轮询一次
      const data = query.state.data
      if (!data || (data.status !== 'success' && data.status !== 'failed' && data.status !== 'cancelled')) {
        return 2000
      }
      return false
    },
    enabled: !!taskId && open,
  })

  useEffect(() => {
    if (task?.status === 'success' && onSuccess) {
      onSuccess()
    }
  }, [task?.status, onSuccess])

  if (!taskId || !open) return null

  const isFinished = task?.status === 'success' || task?.status === 'failed' || task?.status === 'cancelled'
  const isSuccess = task?.status === 'success'
  const isFailed = task?.status === 'failed'

  return (
    <Modal
      title="部署进度"
      open={open}
      onCancel={onClose}
      footer={
        isFinished ? (
          <Button type="primary" onClick={onClose}>
            {isSuccess ? '完成' : '关闭'}
          </Button>
        ) : null
      }
      width={600}
      maskClosable={false}
      closable={isFinished}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="large">
        {/* 进度条 */}
        <div>
          <Progress
            percent={task?.progress || 0}
            status={
              isFailed ? 'exception' :
              isSuccess ? 'success' :
              'active'
            }
            strokeColor={
              isFailed ? '#ff4d4f' :
              isSuccess ? '#52c41a' :
              '#1677ff'
            }
          />
          <div style={{ marginTop: 8, color: '#8c8c8c', fontSize: 13 }}>
            {task?.current_step || '准备中...'}
          </div>
        </div>

        {/* 状态提示 */}
        {isSuccess && (
          <Alert
            message="部署成功"
            description={task?.result_message || '应用部署完成'}
            type="success"
            showIcon
            icon={<CheckCircleOutlined />}
          />
        )}

        {isFailed && (
          <Alert
            message="部署失败"
            description={task?.result_message || '部署过程中出现错误'}
            type="error"
            showIcon
            icon={<CloseCircleOutlined />}
          />
        )}

        {task?.status === 'running' && (
          <Alert
            message="正在部署"
            description="请勿关闭此窗口，部署过程可能需要几分钟..."
            type="info"
            showIcon
            icon={<LoadingOutlined />}
          />
        )}

        {/* 执行日志 */}
        {task?.output_log && (
          <div>
            <Text strong style={{ fontSize: 13 }}>执行日志：</Text>
            <Paragraph
              style={{
                marginTop: 8,
                padding: 12,
                backgroundColor: '#f5f5f5',
                borderRadius: 4,
                fontSize: 12,
                fontFamily: 'monospace',
                maxHeight: 200,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {task.output_log}
            </Paragraph>
          </div>
        )}
      </Space>
    </Modal>
  )
}
