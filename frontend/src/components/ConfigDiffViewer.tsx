import { useState } from 'react'
import { Drawer, Button, Space, Descriptions, Spin, App, Tabs, Alert } from 'antd'
import { CloseOutlined, RollbackOutlined } from '@ant-design/icons'
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { applicationsApi } from '@/api'
import { useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'

interface ConfigDiffViewerProps {
  open: boolean
  onClose: () => void
  appId: string
  configId: string
  targetRevisionId: number
  currentVersion?: string
  targetVersion?: string
}

export default function ConfigDiffViewer({
  open,
  onClose,
  appId,
  configId,
  targetRevisionId,
  currentVersion = '当前版本',
  targetVersion = '历史版本',
}: ConfigDiffViewerProps) {
  const { message } = App.useApp()
  const { confirm } = useRiskConfirm()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<'diff' | 'side-by-side'>('diff')

  // 走 axios API（自动鉴权；回滚还需 X-Risk-Ticket 票据通道），不再裸 fetch
  const { data: compareData, isLoading, error: compareError } = useQuery({
    queryKey: ['config-compare', appId, configId, targetRevisionId],
    queryFn: () => applicationsApi.compareConfig(appId, configId, targetRevisionId),
    enabled: open,
  })

  const rollbackMut = useMutation({
    mutationFn: () => applicationsApi.rollbackConfig(appId, configId, targetRevisionId),
    onSuccess: () => {
      message.success('配置已回滚')
      queryClient.invalidateQueries({ queryKey: ['config-revisions', appId, configId] })
      queryClient.invalidateQueries({ queryKey: ['application-configs', appId] })
      onClose()
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '回滚失败'),
  })

  const handleRollback = () => {
    confirm('config:rollback', () => {
      rollbackMut.mutate()
    })
  }

  return (
    <Drawer
      title="配置版本对比"
      open={open}
      onClose={onClose}
      width={1200}
      extra={
        <Space>
          <Button
            type="primary"
            icon={<RollbackOutlined />}
            onClick={handleRollback}
            loading={rollbackMut.isPending}
            disabled={!compareData?.has_changes}
          >
            回滚到此版本
          </Button>
          <Button icon={<CloseOutlined />} onClick={onClose}>
            关闭
          </Button>
        </Space>
      }
    >
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin size="large" tip="加载对比数据..." />
        </div>
      ) : compareError ? (
        <Alert type="error" showIcon message="加载对比数据失败" description={extractAxiosDetail(compareError) ?? '请稍后重试'} />
      ) : compareData ? (
        <>
          <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="当前版本">
              版本 {compareData.current.revision_id || 'N/A'} | {compareData.current.updated_at}
              {compareData.current.updated_by && ` | ${compareData.current.updated_by}`}
            </Descriptions.Item>
            <Descriptions.Item label="目标版本">
              版本 {compareData.target.revision_id} | {compareData.target.updated_at}
              {compareData.target.updated_by && ` | ${compareData.target.updated_by}`}
            </Descriptions.Item>
            <Descriptions.Item label="当前值类型">
              {compareData.current.value_type}
            </Descriptions.Item>
            <Descriptions.Item label="目标值类型">
              {compareData.target.value_type}
            </Descriptions.Item>
            {compareData.current.credential_ref && (
              <Descriptions.Item label="当前凭证引用" span={2}>
                {compareData.current.credential_ref}
              </Descriptions.Item>
            )}
            {compareData.target.credential_ref && (
              <Descriptions.Item label="目标凭证引用" span={2}>
                {compareData.target.credential_ref}
              </Descriptions.Item>
            )}
          </Descriptions>

          {!compareData.has_changes ? (
            <div style={{ textAlign: 'center', padding: 40, color: '#8c8c8c' }}>
              两个版本的配置内容完全相同
            </div>
          ) : (
            <Tabs
              activeKey={activeTab}
              onChange={(key) => setActiveTab(key as typeof activeTab)}
              items={[
                {
                  key: 'diff',
                  label: '统一差异视图',
                  children: (
                    <ReactDiffViewer
                      oldValue={compareData.target.value}
                      newValue={compareData.current.value}
                      splitView={false}
                      compareMethod={DiffMethod.WORDS}
                      leftTitle={`${targetVersion} (版本 ${compareData.target.revision_id})`}
                      rightTitle={`${currentVersion} (版本 ${compareData.current.revision_id || 'N/A'})`}
                      styles={{
                        diffContainer: {
                          fontSize: 13,
                          lineHeight: 1.6,
                        },
                      }}
                    />
                  ),
                },
                {
                  key: 'side-by-side',
                  label: '左右对比视图',
                  children: (
                    <ReactDiffViewer
                      oldValue={compareData.target.value}
                      newValue={compareData.current.value}
                      splitView={true}
                      compareMethod={DiffMethod.WORDS}
                      leftTitle={`${targetVersion} (版本 ${compareData.target.revision_id})`}
                      rightTitle={`${currentVersion} (版本 ${compareData.current.revision_id || 'N/A'})`}
                      styles={{
                        diffContainer: {
                          fontSize: 13,
                          lineHeight: 1.6,
                        },
                      }}
                    />
                  ),
                },
              ]}
            />
          )}
        </>
      ) : null}
    </Drawer>
  )
}
