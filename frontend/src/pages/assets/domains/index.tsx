import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, App, Tooltip,
  Badge, Flex, Typography, Card, Statistic, Row, Col, Switch, InputNumber, Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined,
  GlobalOutlined, CheckCircleOutlined, ExclamationCircleOutlined,
  ClockCircleOutlined, ProfileOutlined, SafetyCertificateOutlined, MoreOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { domainsApi, credentialsApi } from '@/api'
import { usePermission, useRiskConfirm } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import GroupSidebar from '@/components/GroupSidebar'
import type { DomainResponse, DomainCreate, DomainUpdate } from '@/types/api'

import CloudDomainImportDrawer from './CloudDomainImportDrawer'
import DNSRecordsDrawer from './DNSRecordsDrawer'

const { Text, Paragraph } = Typography

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  active: { text: '正常', color: 'success' },
  expiring: { text: '即将到期', color: 'warning' },
  expired: { text: '已到期', color: 'error' },
  offline: { text: '停用', color: 'default' },
  // 历史遗留值（后端只接受 active/offline），仅用于旧数据展示兜底
  inactive: { text: '停用', color: 'default' },
}

function daysToExpiry(expiresAt?: string): number | null {
  if (!expiresAt) return null
  const exp = dayjs(expiresAt)
  if (!exp.isValid()) return null
  return exp.diff(dayjs(), 'day')
}

export default function DomainsPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [groupFilter, setGroupFilter] = useState<number | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [recordsDomain, setRecordsDomain] = useState<DomainResponse | null>(null)

  const [form] = Form.useForm<DomainCreate>()
  const acmeEnabled = Form.useWatch('acme_enabled', form)

  const { confirm } = useRiskConfirm()
  const { has } = usePermission()
  const canManage = has('action.assets.domains.manage')
  const [importOpen, setImportOpen] = useState(false)
  const { data: providers = [] } = useQuery({ queryKey: ['domain-providers'], queryFn: domainsApi.providers })
  const { modal, message } = App.useApp()

  const { data: credentials = [] } = useQuery({
    queryKey: ['credentials'],
    queryFn: () => credentialsApi.list(),
  })

  const { data: domains = [], isLoading } = useQuery<DomainResponse[]>({
    queryKey: ['domains'],
    queryFn: () => domainsApi.list(),
  })

  const { saveMut, deleteMut } = useCrudResource<DomainCreate | DomainUpdate, number>({
    queryKey: ['domains'],
    entityName: '域名',
    createFn: (data) => domainsApi.create(data as DomainCreate),
    updateFn: (id, data) => domainsApi.update(id, data as DomainUpdate),
    deleteFn: domainsApi.delete,
    onSaved: () => setDrawerOpen(false),
  })

  const batchDeleteMut = useMutation({
    mutationFn: () => domainsApi.batchDelete(selectedKeys.map(Number)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['domains'] })
      setSelectedKeys([])
      message.success(`已删除 ${selectedKeys.length} 个域名`)
    },
    onError: () => message.error('批量删除失败'),
  })

  // 统计指标
  const totalCount = domains.length
  const activeCount = domains.filter((d) => d.status === 'active').length
  const expiringCount = domains.filter((d) => d.status === 'expiring' || (d.expires_at && daysToExpiry(d.expires_at)! <= 30)).length
  const expiredCount = domains.filter((d) => d.status === 'expired' || (d.expires_at && daysToExpiry(d.expires_at)! < 0)).length

  // 筛选
  const filtered = domains.filter((d) => {
    const matchSearch = !search || d.domain.includes(search) || d.root_domain?.includes(search)
    return matchSearch
  })

  const openDrawer = useCallback((dom?: DomainResponse) => {
    if (dom) {
      setEditingId(dom.id)
      form.setFieldsValue({
        domain: dom.domain,
        root_domain: dom.root_domain,
        provider: dom.provider,
        status: dom.status,
        expires_at: dom.expires_at,
        expire_reminder_days: dom.expire_reminder_days,
        remark: dom.remark,
        acme_enabled: dom.acme_enabled === 'yes',
      })
    } else {
      setEditingId(null)
      form.resetFields()
      form.setFieldsValue({ provider: 'custom', status: 'active', expire_reminder_days: 30, acme_enabled: false })
    }
    setDrawerOpen(true)
  }, [form])

  const handleSave = () => {
    form.validateFields().then((values) => {
      saveMut.mutate({ id: editingId ?? undefined, data: values })
      if (values.acme_enabled) {
        modal.info({
          title: 'ACME 自动证书申请已触发',
          icon: <SafetyCertificateOutlined style={{ color: '#1677ff' }} />,
          content: (
            <div>
              <Paragraph style={{ margin: 0 }}>
                系统正通过选定的 DNS 凭证为域名 <Text style={{ fontWeight: 600 }}>{values.domain}</Text> 自动向 Let's Encrypt 提交泛域名证书申请。
              </Paragraph>
              <Paragraph type="secondary" style={{ marginTop: 8, fontSize: 12 }}>
                您可以在“证书管理”中实时查看该域名的签发进度、到期提醒以及自动化分发任务。
              </Paragraph>
            </div>
          ),
          okText: '前往证书管理查看',
          onOk: () => navigate('/assets/certificates'),
        })
      }
    })
  }

  const credOptions = credentials.map((c) => ({ value: c.name, label: `${c.name} (${c.type})` }))

  const columns: ColumnsType<DomainResponse> = [
    {
      title: '域名', dataIndex: 'domain', width: 200, fixed: 'left',
      render: (v: string, r) => canManage ? <a onClick={() => openDrawer(r)}>{r.display_domain || v || '-'}</a> : (r.display_domain || v || '-'),
    },
    { title: '根域名', dataIndex: 'root_domain', width: 160, render: (v: string) => v || '-' },
    {
      title: '服务商', dataIndex: 'provider_label', width: 130,
      render: (v: string, r) => v || r.provider || '-',
    },
    {
      title: '状态', dataIndex: 'status', width: 110,
      render: (status: string) => {
        const s = STATUS_MAP[status] ?? STATUS_MAP.inactive
        return <Badge status={s.color as 'success' | 'warning' | 'error' | 'default'} text={s.text} />
      },
    },
    {
      title: '到期时间', dataIndex: 'expires_at', width: 170,
      render: (v: string) => {
        // 空 = 未查询到/未录入（后端不再伪造 +365 天），引导补录真实到期日
        if (!v) return <Tooltip title="未查询到真实到期时间，请点击域名补录"><Tag style={{ opacity: 0.65 }}>未录入</Tag></Tooltip>
        const days = daysToExpiry(v)
        const dateStr = dayjs(v).format('YYYY-MM-DD')
        if (days === null) return dateStr
        if (days < 0) return <Tag color="error">{dateStr}（已过期）</Tag>
        if (days <= 30) return <Tag color="warning">{dateStr}（{days}天）</Tag>
        return <span>{dateStr}</span>
      },
    },
    {
      title: 'ACME 状态', dataIndex: 'acme_enabled', width: 120,
      render: (v: string) => v === 'yes'
        ? <Tag color="blue" icon={<SafetyCertificateOutlined />}>ACME 自动签发</Tag>
        : <Tag style={{ opacity: 0.6 }}>未开启</Tag>,
    },
    {
      title: 'DNS 记录', dataIndex: 'records_count', width: 110,
      render: (v: number, r) => (
        <Button type="link" size="small" icon={<ProfileOutlined />} onClick={() => setRecordsDomain(r)}>
          {v ?? 0} 条解析
        </Button>
      ),
    },
    { title: '备注', dataIndex: 'remark', ellipsis: true, render: (v: string) => v || '-' },
    {
      title: '操作', key: 'action', width: 100, fixed: 'right',
      render: (_: unknown, r) => {
        if (!canManage) return null
        const moreItems: MenuProps['items'] = [
          { key: 'delete', icon: <DeleteOutlined />, label: '删除域名', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'delete') {
            modal.confirm({
              title: `删除域名 ${r.domain}`,
              content: 'DNS 记录列表将一并移除（不改动外部 DNS 服务商配置）。确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('domains:delete', () => deleteMut.mutate(r.id)),
            })
          }
        }
        return (
          <Space size={4}>
            <Tooltip title="编辑">
              <Button size="small" type="primary" icon={<EditOutlined />} onClick={() => openDrawer(r)} />
            </Tooltip>
            <Dropdown menu={{ items: moreItems, onClick: onMore }} trigger={['click']}>
              <Button size="small" type="text" icon={<MoreOutlined />} />
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  return (
    <Flex gap={16} style={{ height: '100%' }}>
      <GroupSidebar
        resourceType="domain"
        selectedGroupId={groupFilter}
        onSelect={setGroupFilter}
        selectedIds={selectedKeys}
      />

      <Flex vertical gap={16} style={{ flex: 1, minWidth: 0 }}>
        {/* 顶部概览统计卡片 */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>域名总数</span>}
                value={totalCount}
                prefix={<GlobalOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20 }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>正常运行</span>}
                value={activeCount}
                prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>即将到期</span>}
                value={expiringCount}
                prefix={<ClockCircleOutlined style={{ color: '#faad14', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: expiringCount > 0 ? '#faad14' : 'inherit' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>已到期</span>}
                value={expiredCount}
                prefix={<ExclamationCircleOutlined style={{ color: '#ff4d4f', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: expiredCount > 0 ? '#ff4d4f' : 'inherit' }}
              />
            </Card>
          </Col>
        </Row>

        {/* 检索与操作栏 */}
        <Flex justify="space-between" wrap gap={8}>
          <Space>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索域名或根域名"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              allowClear
              style={{ width: 240 }}
            />
            {selectedKeys.length > 0 && (
              <Button danger icon={<DeleteOutlined />} onClick={() => confirm('domains:delete', () => batchDeleteMut.mutate())}>
                批量删除 ({selectedKeys.length})
              </Button>
            )}
          </Space>
          {canManage && <Space>
            <Button onClick={() => setImportOpen(true)} disabled={!providers.some((item) => item.supports_import)}>导入云域名</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新增本地域名</Button>
          </Space>}
        </Flex>

        <Table<DomainResponse>
          rowKey="id"
          rowSelection={canManage ? { selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as string[]) } : undefined}
          columns={columns}
          dataSource={filtered}
          loading={isLoading}
          size="middle"
          scroll={{ x: 1200 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个域名` }}
        />
      </Flex>

      {/* ── 新增/编辑抽屉 ── */}
      <Drawer
        title={editingId ? '编辑域名' : '新增本地域名'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={480}
        extra={<Button type="primary" onClick={handleSave} loading={saveMut.isPending}>保存</Button>}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="domain" label="域名" rules={[{ required: true, message: '请输入域名' }]}>
            <Input prefix={<GlobalOutlined />} placeholder="例如 app.example.com" disabled={!!editingId} />
          </Form.Item>
          <Form.Item name="root_domain" label="根域名（可选）">
            <Input placeholder="例如 example.com" disabled={!!editingId} />
          </Form.Item>
          <Form.Item name="provider" label="服务商" rules={[{ required: true }]}>
            <Select disabled={!!editingId} options={(editingId ? providers : providers.filter((item) => item.key === 'custom'))
              .map((item) => ({ value: item.key, label: item.label }))} />
          </Form.Item>

          <Form.Item name="acme_enabled" label="启用 ACME 自动证书申请" valuePropName="checked">
            <Switch disabled={!providers.find((item) => item.key === form.getFieldValue('provider'))?.supports_acme} />
          </Form.Item>

          {acmeEnabled && (
            <div style={{ background: '#f0f9ff', padding: 12, borderRadius: 6, border: '1px solid #bae0ff', marginBottom: 16 }}>
              <Form.Item name="dns_credential" label="用于 ACME DNS 校验的凭证">
                <Select placeholder="选择关联的 DNS API 凭证" allowClear options={credOptions} />
              </Form.Item>
              <Text type="secondary" style={{ fontSize: 12 }}>
                开启后，OpsPilot 将通过 ACME 协议向 Let's Encrypt 自动签发和续期泛域名证书，并实时推送存入“证书管理”。
              </Text>
            </div>
          )}

          <Form.Item
            name="status" label="状态" initialValue="active"
            extra="「即将到期 / 已到期」按到期时间自动判定，不需要手工选择"
          >
            <Select options={[
              { value: 'active', label: STATUS_MAP.active.text },
              { value: 'offline', label: STATUS_MAP.offline.text },
            ]} />
          </Form.Item>
          <Form.Item name="expires_at" label="到期时间">
            <Input type="date" />
          </Form.Item>
          <Form.Item name="expire_reminder_days" label="提前提醒天数" initialValue={30}>
            <InputNumber min={1} max={180} addonAfter="天" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="备注说明" />
          </Form.Item>
        </Form>
      </Drawer>

      {/* ── DNS 记录列表 Drawer (带快速模糊搜索) ── */}
      {recordsDomain && <DNSRecordsDrawer key={recordsDomain.id} domain={recordsDomain} providers={providers} onClose={() => setRecordsDomain(null)} />}
      {importOpen && <CloudDomainImportDrawer providers={providers} onClose={() => setImportOpen(false)} />}
    </Flex>
  )
}
