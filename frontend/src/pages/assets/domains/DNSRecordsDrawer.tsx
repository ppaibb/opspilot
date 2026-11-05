import { useState } from 'react'
import { Alert, App, Button, Drawer, Flex, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { domainsApi } from '@/api'
import { extractAxiosDetail, usePermission, useRiskConfirm } from '@/hooks/usePermission'
import type { DomainDNSRecordCreate, DomainDNSRecordResponse, DomainProviderItem, DomainResponse } from '@/types/api'
import { buildRecordPayload } from './providerForm'

/** DNS记录列表与编辑抽屉；字段由厂商能力驱动，不在组件中判断厂商名称。 */
export default function DNSRecordsDrawer({ domain, providers, onClose }: {
  domain: DomainResponse; providers: DomainProviderItem[]; onClose: () => void
}) {
  const { has } = usePermission()
  const { confirm } = useRiskConfirm()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<DomainDNSRecordResponse | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [form] = Form.useForm<DomainDNSRecordCreate>()
  const recordType = Form.useWatch('record_type', form) ?? 'A'
  const proxied = Form.useWatch('proxied', form)
  const spec = providers.find((item) => item.key === domain.provider)
  const canManage = has('action.assets.domains.manage') && !!spec?.supports_dns_records
  const records = useQuery({ queryKey: ['domain-records', domain.id], queryFn: () => domainsApi.listRecords(domain.id), retry: false })
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['domain-records', domain.id] })
    queryClient.invalidateQueries({ queryKey: ['domains'] })
  }
  const syncMutation = useMutation({ mutationFn: () => domainsApi.syncRecords(domain.id), onSuccess: () => {
    refresh()
    message.success('云端记录已同步')
  }, onError: (error) => message.error(extractAxiosDetail(error) ?? '同步失败') })
  const saveMutation = useMutation({
    mutationFn: (values: DomainDNSRecordCreate) => {
      const payload = buildRecordPayload(values, spec ?? {})
      return editing ? domainsApi.updateRecord(domain.id, editing.id, payload) : domainsApi.createRecord(domain.id, payload)
    },
    onSuccess: () => { refresh(); setFormOpen(false); message.success('解析记录已保存') },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })
  const deleteMutation = useMutation({ mutationFn: (id: number) => domainsApi.deleteRecord(domain.id, id),
    onSuccess: () => { refresh(); message.success('解析记录已删除') },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '删除失败') })
  const openForm = (record: DomainDNSRecordResponse | null) => {
    setEditing(record)
    form.resetFields()
    form.setFieldsValue(record ? {
      host: record.host, record_type: record.record_type,
      record_value: record.record_values?.join('\n') ?? record.record_value,
      ttl: record.ttl, remark: record.remark, proxied: record.proxied ?? false, priority: record.priority ?? 10,
    } : { host: '@', record_type: 'A', record_value: '', ttl: 600, proxied: false, priority: 10 })
    setFormOpen(true)
  }
  const columns: ColumnsType<DomainDNSRecordResponse> = [
    { title: '主机记录', dataIndex: 'host', width: 120 },
    { title: '类型', dataIndex: 'record_type', width: 120, render: (type, row) => <Space><Tag>{type}</Tag>{row.read_only && <span style={{ whiteSpace: 'nowrap' }}>只读</span>}</Space> },
    { title: '记录值', dataIndex: 'record_value', render: (value: string) => <span style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value}</span> },
    { title: 'TTL', dataIndex: 'ttl', width: 80, render: (value: number) => value === 1 && spec?.supports_proxy ? '自动' : value },
    ...(spec?.supports_proxy ? [{ title: '代理', dataIndex: 'proxied', width: 70, render: (value: boolean | null) => value ? '开启' : '关闭' }] : []),
    ...(spec?.supports_priority ? [{ title: 'MX优先级', dataIndex: 'priority', width: 95, render: (value: number | null) => value ?? '-' }] : []),
    ...(canManage ? [{ title: '操作', key: 'action', width: 115, render: (_: unknown, row: DomainDNSRecordResponse) => (
      <Space>
        <Tooltip title={row.read_only ? '该记录只读' : '编辑'}>
          <Button aria-label="编辑解析记录" size="small" icon={<EditOutlined />} disabled={row.read_only || deleteMutation.isPending} onClick={() => openForm(row)} />
        </Tooltip>
        <Popconfirm title="删除该云端解析记录？" disabled={row.read_only} onConfirm={() => confirm('domains:delete', () => deleteMutation.mutate(row.id))}>
          <Button aria-label="删除解析记录" size="small" danger icon={<DeleteOutlined />} disabled={row.read_only} loading={deleteMutation.isPending && deleteMutation.variables === row.id} />
        </Popconfirm>
      </Space>
    ) }] : []),
  ]
  const filtered = (records.data ?? []).filter((row) => `${row.host} ${row.record_type} ${row.record_value}`.toLowerCase().includes(search.toLowerCase()))
  return (
    <Drawer title={`DNS 解析记录 · ${domain.display_domain || domain.domain}`} open width={980} onClose={onClose}>
      <Flex vertical gap={16}>
        {records.isError && <Alert type="error" showIcon message="解析记录读取失败" description={extractAxiosDetail(records.error) ?? '请重新同步后重试'} />}
        {!spec?.supports_dns_records && <Alert type="info" message="该厂商尚未接入云端解析管理，当前仅显示已有记录" />}
        <Flex justify="space-between" gap={12} wrap>
          <Input.Search aria-label="搜索解析记录" placeholder="搜索记录名称、类型或解析值" value={search} onChange={(event) => setSearch(event.target.value)} style={{ width: 300 }} allowClear />
          <Space>
            {canManage && spec?.supports_import && <Button icon={<ReloadOutlined />} loading={syncMutation.isPending} onClick={() => syncMutation.mutate()}>同步云端</Button>}
            {canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => openForm(null)}>新增解析记录</Button>}
          </Space>
        </Flex>
        <Table rowKey="id" size="small" dataSource={filtered} loading={records.isLoading} columns={columns} pagination={{ defaultPageSize: 15 }} scroll={{ x: 850 }} />
      </Flex>
      <Drawer title={editing ? '编辑解析记录' : '新增解析记录'} open={formOpen} width={520} onClose={() => setFormOpen(false)}
        closable={!saveMutation.isPending} maskClosable={!saveMutation.isPending}
        extra={<Button type="primary" loading={saveMutation.isPending} onClick={() => form.validateFields().then((values) => saveMutation.mutate(values))}>保存</Button>}>
        <Form form={form} layout="vertical" disabled={saveMutation.isPending}>
          <Form.Item label="主机记录" name="host" rules={[{ required: true }]}>
            <Input placeholder="@ / www / api.internal" disabled={!!editing && !spec?.can_rename_records} />
          </Form.Item>
          <Form.Item label="记录类型" name="record_type" rules={[{ required: true }]}>
            <Select options={(spec?.record_types ?? []).map((value) => ({ value, label: value }))} disabled={!!editing && !spec?.can_rename_records} />
          </Form.Item>
          <Form.Item label={spec?.supports_multi_values ? '记录值（每行一个值）' : '记录值'} name="record_value" rules={[{ required: true, whitespace: true, message: '请输入解析值' }]}>
            <Input.TextArea rows={spec?.supports_multi_values ? 5 : 3} />
          </Form.Item>
          {spec?.supports_priority && recordType === 'MX' && <Form.Item label="MX优先级" name="priority"><InputNumber min={0} max={65535} style={{ width: '100%' }} /></Form.Item>}
          {spec?.supports_proxy && ['A', 'AAAA', 'CNAME'].includes(recordType) && (
            <Form.Item label="Cloudflare代理" name="proxied" valuePropName="checked"><Switch /></Form.Item>
          )}
          <Form.Item label="TTL（秒）" name="ttl" rules={[{ required: true }]} extra={spec?.supports_proxy ? '1 表示自动；启用代理时云端自动使用自动 TTL' : undefined}>
            <InputNumber min={spec?.supports_proxy ? 1 : 60} max={86400} disabled={!!spec?.supports_proxy && !!proxied && ['A', 'AAAA', 'CNAME'].includes(recordType)} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item label="备注" name="remark"><Input.TextArea rows={2} maxLength={255} /></Form.Item>
        </Form>
      </Drawer>
    </Drawer>
  )
}
