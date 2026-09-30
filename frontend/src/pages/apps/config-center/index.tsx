import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Input, Select, Table, Drawer, Form, Popconfirm, Space, App, Tag, Radio, Modal, Typography } from 'antd'
import { PlusOutlined, HistoryOutlined, ImportOutlined, SearchOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { applicationsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { ApplicationConfigResponse, ApplicationConfigPayload, ApplicationConfigRevisionResponse } from '@/types/api'

const { TextArea } = Input
const { Paragraph } = Typography

export default function ConfigCenterPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()

  const [selectedAppId, setSelectedAppId] = useState<string>()
  const [searchText, setSearchText] = useState('')
  const [envFilter, setEnvFilter] = useState<string>()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingConfig, setEditingConfig] = useState<ApplicationConfigResponse | null>(null)
  const [historyDrawerOpen, setHistoryDrawerOpen] = useState(false)
  const [historyConfigId, setHistoryConfigId] = useState<string>()
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [form] = Form.useForm<ApplicationConfigPayload>()
  const [importForm] = Form.useForm<{ content: string; env: string; conflict_mode: 'skip' | 'overwrite' | 'ask' }>()

  const { data: apps = [] } = useQuery({
    queryKey: ['applications'],
    queryFn: () => applicationsApi.list(),
  })

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['application-configs', selectedAppId, envFilter, searchText],
    queryFn: () => applicationsApi.listConfigs(selectedAppId!, { env: envFilter, q: searchText }),
    enabled: !!selectedAppId,
  })

  const { data: credentials = [] } = useQuery({
    queryKey: ['credential-refs'],
    queryFn: () => applicationsApi.getCredentialRefs(),
  })

  const { data: revisions = [] } = useQuery({
    queryKey: ['application-config-revisions', selectedAppId, historyConfigId],
    queryFn: () => applicationsApi.listConfigRevisions(selectedAppId!, historyConfigId!),
    enabled: !!selectedAppId && !!historyConfigId,
  })

  const saveMutation = useMutation({
    mutationFn: (data: ApplicationConfigPayload) => {
      if (editingConfig) {
        return applicationsApi.updateConfig(selectedAppId!, editingConfig.config_id, data)
      }
      return applicationsApi.createConfig(selectedAppId!, data)
    },
    onSuccess: () => {
      message.success(editingConfig ? '配置已更新' : '配置已创建')
      queryClient.invalidateQueries({ queryKey: ['application-configs', selectedAppId] })
      setDrawerOpen(false)
      form.resetFields()
      setEditingConfig(null)
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) ?? '操作失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (configId: string) => applicationsApi.deleteConfig(selectedAppId!, configId),
    onSuccess: () => {
      message.success('配置已删除')
      queryClient.invalidateQueries({ queryKey: ['application-configs', selectedAppId] })
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) ?? '删除失败')
    },
  })

  const importMutation = useMutation({
    mutationFn: (data: { content: string; env: string; conflict_mode: 'skip' | 'overwrite' | 'ask' }) =>
      applicationsApi.importConfigs(selectedAppId!, data),
    onSuccess: (result) => {
      message.success(`导入成功：${result.imported_count} 条新增，${result.overwritten_count} 条覆盖，${result.skipped_count} 条跳过`)
      queryClient.invalidateQueries({ queryKey: ['application-configs', selectedAppId] })
      setImportModalOpen(false)
      importForm.resetFields()
    },
    onError: (err: unknown) => {
      message.error(extractAxiosDetail(err) ?? '导入失败')
    },
  })

  const handleAdd = () => {
    setEditingConfig(null)
    form.resetFields()
    form.setFieldsValue({ value_type: 'plain', env: 'production' })
    setDrawerOpen(true)
  }

  const handleEdit = (record: ApplicationConfigResponse) => {
    setEditingConfig(record)
    form.setFieldsValue({
      key: record.key,
      value: record.value,
      value_type: record.value_type,
      credential_id: record.credential_id,
      env: record.env,
      description: record.description,
    })
    setDrawerOpen(true)
  }

  const handleDelete = (configId: string) => {
    deleteMutation.mutate(configId)
  }

  const handleHistory = (record: ApplicationConfigResponse) => {
    setHistoryConfigId(record.config_id)
    setHistoryDrawerOpen(true)
  }

  const handleSave = () => {
    form.validateFields().then((values) => {
      saveMutation.mutate(values)
    })
  }

  const handleImport = () => {
    setImportModalOpen(true)
    importForm.setFieldsValue({ env: 'production', conflict_mode: 'skip' })
  }

  const handleImportSubmit = () => {
    importForm.validateFields().then((values) => {
      importMutation.mutate(values)
    })
  }

  const valueTypeWatch = Form.useWatch('value_type', form)

  const columns: ColumnsType<ApplicationConfigResponse> = [
    {
      title: 'Key',
      dataIndex: 'key',
      width: 200,
      ellipsis: true,
    },
    {
      title: 'Value',
      dataIndex: 'value',
      width: 300,
      ellipsis: true,
      render: (val, record) => {
        if (record.value_type === 'credential_ref') {
          return (
            <Space>
              <Tag color="orange">凭证引用</Tag>
              <span>{record.credential_name || record.credential_id}</span>
            </Space>
          )
        }
        return <Paragraph copyable ellipsis={{ rows: 1 }} style={{ marginBottom: 0 }}>{val}</Paragraph>
      },
    },
    {
      title: '类型',
      dataIndex: 'value_type',
      width: 100,
      render: (val) => (val === 'credential_ref' ? <Tag color="orange">凭证</Tag> : <Tag>明文</Tag>),
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
      title: '描述',
      dataIndex: 'description',
      ellipsis: true,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 160,
      render: (val) => new Date(val).toLocaleString('zh-CN'),
    },
    {
      title: '更新人',
      dataIndex: 'updated_by',
      width: 100,
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Button size="small" onClick={() => handleEdit(record)}>编辑</Button>
          <Button size="small" icon={<HistoryOutlined />} onClick={() => handleHistory(record)}>历史</Button>
          <Popconfirm title="确认删除此配置？" onConfirm={() => handleDelete(record.config_id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const revisionColumns: ColumnsType<ApplicationConfigRevisionResponse> = [
    {
      title: '变更时间',
      dataIndex: 'created_at',
      width: 160,
      render: (val) => new Date(val).toLocaleString('zh-CN'),
    },
    {
      title: '变更类型',
      dataIndex: 'change_type',
      width: 100,
      render: (val) => {
        const map: Record<string, { color: string; label: string }> = {
          create: { color: 'green', label: '新增' },
          update: { color: 'blue', label: '修改' },
          delete: { color: 'red', label: '删除' },
        }
        const cfg = map[val] || { color: 'default', label: val }
        return <Tag color={cfg.color}>{cfg.label}</Tag>
      },
    },
    {
      title: '旧值',
      dataIndex: 'old_value',
      ellipsis: true,
      render: (val, record) => {
        if (record.old_value_type === 'credential_ref') {
          return <Tag color="orange">凭证 {record.old_credential_id}</Tag>
        }
        return val || '-'
      },
    },
    {
      title: '新值',
      dataIndex: 'new_value',
      ellipsis: true,
      render: (val, record) => {
        if (record.new_value_type === 'credential_ref') {
          return <Tag color="orange">凭证 {record.new_credential_id}</Tag>
        }
        return val || '-'
      },
    },
    {
      title: '操作人',
      dataIndex: 'created_by',
      width: 100,
    },
    {
      title: '备注',
      dataIndex: 'remark',
      ellipsis: true,
    },
  ]

  return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 16, display: 'flex', gap: 16, alignItems: 'center' }}>
        <Select
          placeholder="选择应用"
          style={{ width: 300 }}
          value={selectedAppId}
          onChange={setSelectedAppId}
          showSearch
          optionFilterProp="label"
          options={apps.map((app) => ({ value: app.app_id, label: `${app.name} (${app.app_id})` }))}
        />
        {selectedAppId && (
          <>
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
            <Input
              placeholder="搜索 Key"
              prefix={<SearchOutlined />}
              style={{ width: 200 }}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              allowClear
            />
            <div style={{ flex: 1 }} />
            <Button icon={<ImportOutlined />} onClick={handleImport}>导入 .env</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>新增配置</Button>
          </>
        )}
      </div>

      {selectedAppId ? (
        <Table
          columns={columns}
          dataSource={configs}
          rowKey="config_id"
          loading={isLoading}
          pagination={{ pageSize: 20 }}
          scroll={{ x: 1400 }}
        />
      ) : (
        <div style={{ textAlign: 'center', padding: '60px 0', color: '#999' }}>
          请先选择一个应用查看配置
        </div>
      )}

      <Drawer
        title={editingConfig ? '编辑配置' : '新增配置'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={600}
        extra={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" onClick={handleSave} loading={saveMutation.isPending}>
              保存
            </Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item label="Key" name="key" rules={[{ required: true, message: '请输入配置 Key' }]}>
            <Input placeholder="例如：DATABASE_URL" disabled={!!editingConfig} />
          </Form.Item>
          <Form.Item label="值类型" name="value_type" rules={[{ required: true }]}>
            <Radio.Group>
              <Radio value="plain">明文</Radio>
              <Radio value="credential_ref">凭证引用</Radio>
            </Radio.Group>
          </Form.Item>
          {valueTypeWatch === 'plain' ? (
            <Form.Item label="Value" name="value" rules={[{ required: true, message: '请输入配置值' }]}>
              <TextArea rows={4} placeholder="配置值" />
            </Form.Item>
          ) : (
            <Form.Item label="凭证" name="credential_id" rules={[{ required: true, message: '请选择凭证' }]}>
              <Select
                placeholder="选择凭证"
                showSearch
                optionFilterProp="label"
                options={credentials.map((c) => ({ value: c.cred_id, label: `${c.name} (${c.type})` }))}
              />
            </Form.Item>
          )}
          <Form.Item label="环境" name="env" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'production', label: '生产' },
                { value: 'staging', label: '预发' },
                { value: 'test', label: '测试' },
                { value: 'development', label: '开发' },
              ]}
            />
          </Form.Item>
          <Form.Item label="描述" name="description">
            <TextArea rows={2} placeholder="配置用途说明" />
          </Form.Item>
        </Form>
      </Drawer>

      <Drawer
        title="配置变更历史"
        open={historyDrawerOpen}
        onClose={() => setHistoryDrawerOpen(false)}
        width={900}
      >
        <Table
          columns={revisionColumns}
          dataSource={revisions}
          rowKey="revision_id"
          pagination={{ pageSize: 20 }}
        />
      </Drawer>

      <Modal
        title="导入 .env 文件"
        open={importModalOpen}
        onCancel={() => setImportModalOpen(false)}
        onOk={handleImportSubmit}
        confirmLoading={importMutation.isPending}
        width={600}
      >
        <Form form={importForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item label="环境" name="env" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'production', label: '生产' },
                { value: 'staging', label: '预发' },
                { value: 'test', label: '测试' },
                { value: 'development', label: '开发' },
              ]}
            />
          </Form.Item>
          <Form.Item label="冲突处理" name="conflict_mode" rules={[{ required: true }]}>
            <Radio.Group>
              <Radio value="skip">跳过（保留原值）</Radio>
              <Radio value="overwrite">覆盖（使用新值）</Radio>
            </Radio.Group>
          </Form.Item>
          <Form.Item
            label=".env 内容"
            name="content"
            rules={[{ required: true, message: '请粘贴 .env 文件内容' }]}
          >
            <TextArea
              rows={10}
              placeholder={`DATABASE_URL=postgres://...
REDIS_HOST=127.0.0.1
API_KEY=abc123`}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
