import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Input, Select, Table, Drawer, Space, App, Tag, Descriptions, Typography, Popconfirm, Alert } from 'antd'
import { SearchOutlined, ReloadOutlined, EyeOutlined, SyncOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { applicationsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { ApplicationReleaseResponse } from '@/types/api'

const { TextArea } = Input
const { Paragraph } = Typography

export default function DeploymentPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()

  const [searchText, setSearchText] = useState('')
  const [appFilter, setAppFilter] = useState<string>()
  const [envFilter, setEnvFilter] = useState<string>()
  const [statusFilter, setStatusFilter] = useState<string>()
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false)
  const [selectedRelease, setSelectedRelease] = useState<ApplicationReleaseResponse | null>(null)

  const { data: apps = [] } = useQuery({
    queryKey: ['applications'],
    queryFn: () => applicationsApi.list(),
  })

  const { data: releases = [], isLoading } = useQuery({
    queryKey: ['application-releases', appFilter, envFilter, statusFilter],
    queryFn: () =>
      applicationsApi.listReleases({
        app_id: appFilter,
        env: envFilter,
        status: statusFilter,
      }),
  })

  const retryMutation = useMutation({
    mutationFn: (release: ApplicationReleaseResponse) =>
      applicationsApi.retryRelease(release.app_id, release.release_id),
    onSuccess: () => {
      message.success('重试已提交')
      queryClient.invalidateQueries({ queryKey: ['application-releases'] })
      setDetailDrawerOpen(false)
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) ?? '重试失败')
    },
  })

  const handleViewDetail = (record: ApplicationReleaseResponse) => {
    setSelectedRelease(record)
    setDetailDrawerOpen(true)
  }

  const handleRetry = (release: ApplicationReleaseResponse) => {
    retryMutation.mutate(release)
  }

  const filteredReleases = releases.filter((r) => {
    if (searchText) {
      const text = searchText.toLowerCase()
      return (
        r.app_name.toLowerCase().includes(text) ||
        r.component_name?.toLowerCase().includes(text) ||
        r.version.toLowerCase().includes(text) ||
        r.operator.toLowerCase().includes(text)
      )
    }
    return true
  })

  const columns: ColumnsType<ApplicationReleaseResponse> = [
    {
      title: '发布ID',
      dataIndex: 'release_id',
      width: 150,
      ellipsis: true,
    },
    {
      title: '应用',
      dataIndex: 'app_name',
      width: 150,
      ellipsis: true,
    },
    {
      title: '组件',
      dataIndex: 'component_name',
      width: 150,
      ellipsis: true,
      render: (val) => val || '-',
    },
    {
      title: '版本',
      dataIndex: 'version',
      width: 120,
      ellipsis: true,
    },
    {
      title: '环境',
      dataIndex: 'env',
      width: 100,
      render: (val) => {
        const envMap: Record<string, { color: string; label: string }> = {
          production: { color: 'red', label: '生产' },
          staging: { color: 'orange', label: '预发' },
          test: { color: 'blue', label: '测试' },
          development: { color: 'default', label: '开发' },
        }
        const cfg = envMap[val] || { color: 'default', label: val }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      },
    },
    {
      title: '发布类型',
      dataIndex: 'release_type',
      width: 120,
      render: (val) => {
        const typeMap: Record<string, { color: string; label: string }> = {
          deploy: { color: 'blue', label: '部署' },
          upgrade: { color: 'cyan', label: '升级' },
          rollback: { color: 'orange', label: '回滚' },
          restart: { color: 'purple', label: '重启' },
        }
        const cfg = typeMap[val] || { color: 'default', label: val }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (val) => {
        const statusMap: Record<string, { color: string; label: string }> = {
          pending: { color: 'default', label: '待执行' },
          running: { color: 'processing', label: '执行中' },
          success: { color: 'success', label: '成功' },
          failed: { color: 'error', label: '失败' },
          cancelled: { color: 'default', label: '已取消' },
        }
        const cfg = statusMap[val] || { color: 'default', label: val }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      },
    },
    {
      title: '操作人',
      dataIndex: 'operator',
      width: 100,
    },
    {
      title: '开始时间',
      dataIndex: 'started_at',
      width: 160,
      render: (val) => (val ? new Date(val).toLocaleString('zh-CN') : '-'),
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      width: 100,
      render: (val) => {
        if (!val) return '-'
        const seconds = Math.floor(val / 1000)
        if (seconds < 60) return `${seconds}秒`
        const minutes = Math.floor(seconds / 60)
        return `${minutes}分${seconds % 60}秒`
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Button size="small" icon={<EyeOutlined />} onClick={() => handleViewDetail(record)}>
            详情
          </Button>
          {record.status === 'failed' && (
            <Popconfirm title="确认重试此发布？" onConfirm={() => handleRetry(record)}>
              <Button size="small" icon={<SyncOutlined />}>
                重试
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 16, display: 'flex', gap: 16, alignItems: 'center' }}>
        <Input
          placeholder="搜索应用、组件、版本、操作人"
          prefix={<SearchOutlined />}
          style={{ width: 300 }}
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          allowClear
        />
        <Select
          placeholder="应用筛选"
          style={{ width: 200 }}
          value={appFilter}
          onChange={setAppFilter}
          allowClear
          showSearch
          optionFilterProp="label"
          options={apps.map((app) => ({ value: app.app_id, label: app.name }))}
        />
        <Select
          placeholder="环境筛选"
          style={{ width: 150 }}
          value={envFilter}
          onChange={setEnvFilter}
          allowClear
          options={[
            { value: 'production', label: '生产' },
            { value: 'staging', label: '预发' },
            { value: 'test', label: '测试' },
            { value: 'development', label: '开发' },
          ]}
        />
        <Select
          placeholder="状态筛选"
          style={{ width: 150 }}
          value={statusFilter}
          onChange={setStatusFilter}
          allowClear
          options={[
            { value: 'pending', label: '待执行' },
            { value: 'running', label: '执行中' },
            { value: 'success', label: '成功' },
            { value: 'failed', label: '失败' },
            { value: 'cancelled', label: '已取消' },
          ]}
        />
        <div style={{ flex: 1 }} />
        <Button
          icon={<ReloadOutlined />}
          onClick={() => queryClient.invalidateQueries({ queryKey: ['application-releases'] })}
        >
          刷新
        </Button>
      </div>

      <Table
        columns={columns}
        dataSource={filteredReleases}
        rowKey="release_id"
        loading={isLoading}
        pagination={{ pageSize: 20, showTotal: (total) => `共 ${total} 条记录` }}
        scroll={{ x: 1600 }}
      />

      <Drawer
        title="发布详情"
        open={detailDrawerOpen}
        onClose={() => setDetailDrawerOpen(false)}
        width={800}
        extra={
          selectedRelease?.status === 'failed' && (
            <Popconfirm title="确认重试此发布？" onConfirm={() => handleRetry(selectedRelease)}>
              <Button type="primary" icon={<SyncOutlined />} loading={retryMutation.isPending}>
                重试
              </Button>
            </Popconfirm>
          )
        }
      >
        {selectedRelease && (
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="发布ID" span={2}>
                {selectedRelease.release_id}
              </Descriptions.Item>
              <Descriptions.Item label="应用">{selectedRelease.app_name}</Descriptions.Item>
              <Descriptions.Item label="组件">{selectedRelease.component_name || '-'}</Descriptions.Item>
              <Descriptions.Item label="版本">{selectedRelease.version}</Descriptions.Item>
              <Descriptions.Item label="环境">
                {(() => {
                  const envMap: Record<string, { color: string; label: string }> = {
                    production: { color: 'red', label: '生产' },
                    staging: { color: 'orange', label: '预发' },
                    test: { color: 'blue', label: '测试' },
                    development: { color: 'default', label: '开发' },
                  }
                  const cfg = envMap[selectedRelease.env] || { color: 'default', label: selectedRelease.env }
                  return <Tag color={cfg.color}>{cfg.label}</Tag>
                })()}
              </Descriptions.Item>
              <Descriptions.Item label="发布类型">
                {(() => {
                  const typeMap: Record<string, { color: string; label: string }> = {
                    deploy: { color: 'blue', label: '部署' },
                    upgrade: { color: 'cyan', label: '升级' },
                    rollback: { color: 'orange', label: '回滚' },
                    restart: { color: 'purple', label: '重启' },
                  }
                  const cfg = typeMap[selectedRelease.release_type] || { color: 'default', label: selectedRelease.release_type }
                  return <Tag color={cfg.color}>{cfg.label}</Tag>
                })()}
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                {(() => {
                  const statusMap: Record<string, { color: string; label: string }> = {
                    pending: { color: 'default', label: '待执行' },
                    running: { color: 'processing', label: '执行中' },
                    success: { color: 'success', label: '成功' },
                    failed: { color: 'error', label: '失败' },
                    cancelled: { color: 'default', label: '已取消' },
                  }
                  const cfg = statusMap[selectedRelease.status] || { color: 'default', label: selectedRelease.status }
                  return <Tag color={cfg.color}>{cfg.label}</Tag>
                })()}
              </Descriptions.Item>
              <Descriptions.Item label="操作人">{selectedRelease.operator}</Descriptions.Item>
              <Descriptions.Item label="开始时间">
                {selectedRelease.started_at ? new Date(selectedRelease.started_at).toLocaleString('zh-CN') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="完成时间">
                {selectedRelease.finished_at ? new Date(selectedRelease.finished_at).toLocaleString('zh-CN') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="耗时">
                {(() => {
                  if (!selectedRelease.duration_ms) return '-'
                  const seconds = Math.floor(selectedRelease.duration_ms / 1000)
                  if (seconds < 60) return `${seconds}秒`
                  const minutes = Math.floor(seconds / 60)
                  return `${minutes}分${seconds % 60}秒`
                })()}
              </Descriptions.Item>
              {selectedRelease.template_name && (
                <Descriptions.Item label="发布模板" span={2}>
                  {selectedRelease.template_name}
                </Descriptions.Item>
              )}
              {selectedRelease.artifact_ref && (
                <Descriptions.Item label="制品引用" span={2}>
                  {selectedRelease.artifact_ref}
                </Descriptions.Item>
              )}
              {selectedRelease.rollback_release_id && (
                <Descriptions.Item label="回滚至" span={2}>
                  {selectedRelease.rollback_release_id}
                </Descriptions.Item>
              )}
              {selectedRelease.external_url && (
                <Descriptions.Item label="外部链接" span={2}>
                  <a href={selectedRelease.external_url} target="_blank" rel="noopener noreferrer">
                    {selectedRelease.external_url}
                  </a>
                </Descriptions.Item>
              )}
              {selectedRelease.target_names && selectedRelease.target_names.length > 0 && (
                <Descriptions.Item label="目标主机" span={2}>
                  <Space wrap>
                    {selectedRelease.target_names.map((name, idx) => (
                      <Tag key={idx}>{name}</Tag>
                    ))}
                  </Space>
                </Descriptions.Item>
              )}
              {selectedRelease.summary && (
                <Descriptions.Item label="发布说明" span={2}>
                  <Paragraph style={{ marginBottom: 0 }}>{selectedRelease.summary}</Paragraph>
                </Descriptions.Item>
              )}
            </Descriptions>

            {selectedRelease.output && (
              <div>
                <div style={{ marginBottom: 8, fontWeight: 500 }}>执行日志</div>
                <TextArea
                  value={selectedRelease.output}
                  readOnly
                  autoSize={{ minRows: 10, maxRows: 30 }}
                  style={{ fontFamily: 'monospace', fontSize: 12 }}
                />
              </div>
            )}

            {selectedRelease.status === 'failed' && (
              <Alert
                message="发布失败"
                description="可以点击右上角的「重试」按钮重新执行此发布任务，或联系管理员排查日志。"
                type="error"
                showIcon
              />
            )}
          </Space>
        )}
      </Drawer>
    </div>
  )
}
