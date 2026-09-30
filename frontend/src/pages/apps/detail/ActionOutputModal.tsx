import {
  Modal, Button, Flex, Descriptions,
} from 'antd'
import type { ApplicationRuntimeActionResponse } from '@/types/api'

export default function ActionOutputModal({
  actionOutput,
  onClose,
}: {
  actionOutput: { title: string; response: ApplicationRuntimeActionResponse } | null
  onClose: () => void
}) {
  return (
    <Modal title={actionOutput?.title} open={!!actionOutput} onCancel={onClose}
      footer={<Button onClick={onClose}>关闭</Button>} width={640}>
      {actionOutput && (
        <Flex vertical gap={12}>
          <Descriptions size="small" bordered column={2}>
            <Descriptions.Item label="结果">{actionOutput.response.ok ? '成功' : '失败'}</Descriptions.Item>
            <Descriptions.Item label="退出码">{actionOutput.response.code}</Descriptions.Item>
            <Descriptions.Item label="目标数">{actionOutput.response.target_count}</Descriptions.Item>
            <Descriptions.Item label="耗时">{actionOutput.response.duration_ms} ms</Descriptions.Item>
          </Descriptions>
          <pre style={{ background: '#1e1e1e', color: '#d4d4d4', padding: 16, borderRadius: 6, fontSize: 13, maxHeight: 400, overflowY: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {[actionOutput.response.stdout, actionOutput.response.stderr].filter(Boolean).join('\n') || actionOutput.response.notice || '（远程命令无输出）'}
          </pre>
        </Flex>
      )}
    </Modal>
  )
}
