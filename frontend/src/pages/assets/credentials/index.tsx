import { useState, useCallback, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, App, Tooltip,
  Modal, Radio, Flex, Typography, DatePicker, Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined,
  KeyOutlined, ExportOutlined, DownloadOutlined,
  SafetyCertificateOutlined, MoreOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { credentialsApi } from '@/api'
import { useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import { downloadBlob } from '@/utils/download'
import type { CredentialResponse, CredentialCreate, CredentialUpdate, CredentialType } from '@/types/api'

const { Text, Paragraph } = Typography

/**
 * 凭证类型取值与后端 _build_export_payload / is_long_lived_key_type 的判断对齐：
 * AccessKey → content 存 JSON；SSH Key → content 存 PEM；Password/Token → content 存明文。
 */
const TYPE_MAP: Record<string, { text: string; color: string }> = {
  Password: { text: '密码凭证', color: 'blue' },
  'SSH Key': { text: 'SSH 密钥', color: 'purple' },
  Token: { text: 'API Token', color: 'orange' },
  AccessKey: { text: '云厂商 AK/SK', color: 'green' },
}

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  active: { text: '有效', color: 'green' },
  expiring: { text: '即将过期', color: 'orange' },
  expired: { text: '已过期', color: 'red' },
}

/** 按凭证类型把表单字段组装成后端要求的单个 content 字符串 */
function buildContent(type: string, values: Record<string, unknown>): string {
  if (type === 'AccessKey') {
    const ak = String(values.access_key || '').trim()
    const sk = String(values.secret_key || '').trim()
    // 编辑态下两个都留空 = 不改动；不能返回 "{}" 否则会把已存 AK/SK 覆盖成空
    if (!ak && !sk) return ''
    return JSON.stringify({ access_key: ak, secret_key: sk })
  }
  if (type === 'SSH Key') return String(values.private_key || '')
  if (type === 'Token') return String(values.token || '')
  return String(values.password || '')
}

/** 抽屉表单的值结构：密文字段按类型二选一，expires_at 是日期控件，提交前转成 YYYY-MM-DD */
interface CredentialFormValues {
  name: string
  type: CredentialType
  expires_at?: dayjs.Dayjs
  scope?: string
  allowed_roles?: string[]
  password?: string
  private_key?: string
  token?: string
  access_key?: string
  secret_key?: string
}

export default function CredentialsPage() {
  const queryClient = useQueryClient()
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState<string | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  // 导出 Modal 控制
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const [exportMode, setExportMode] = useState<'audit' | 'encrypted'>('audit')
  const [exporting, setExporting] = useState(false)

  const [form] = Form.useForm<CredentialFormValues>()
  const credType = Form.useWatch('type', form)

  const { confirm } = useRiskConfirm()
  const { message, modal } = App.useApp()

  const { data: credentials = [], isLoading } = useQuery<CredentialResponse[]>({
    queryKey: ['credentials'],
    queryFn: credentialsApi.list,
  })

  const { data: accessRoles = [] } = useQuery({
    queryKey: ['credential-access-options'],
    queryFn: credentialsApi.accessOptions,
  })

  const { saveMut, deleteMut } = useCrudResource<CredentialCreate, string, CredentialUpdate>({
    queryKey: ['credentials'],
    entityName: '凭证',
    createFn: credentialsApi.create,
    updateFn: credentialsApi.update,
    deleteFn: credentialsApi.delete,
    onSaved: () => setDrawerOpen(false),
  })

  const batchDeleteMut = useMutation({
    mutationFn: () => credentialsApi.batchDelete(selectedKeys),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['credentials'] })
      setSelectedKeys([])
      message.success(`已删除 ${selectedKeys.length} 项凭证`)
    },
    onError: () => message.error('批量删除失败'),
  })

  // 筛选
  const filtered = useMemo(() => credentials.filter((c) => {
    const matchSearch = !search || c.name.includes(search) || (c.scope || '').includes(search)
    const matchType = !typeFilter || c.type === typeFilter
    return matchSearch && matchType
  }), [credentials, search, typeFilter])

  const openDrawer = useCallback((cred?: CredentialResponse) => {
    if (cred) {
      setEditingId(cred.cred_id)
      // 密文后端永不回传，编辑时敏感字段一律留空，留空即表示不改动
      form.setFieldsValue({
        name: cred.name,
        type: cred.type,
        // DatePicker 需要 Dayjs；直接塞字符串会导致回显空白
        expires_at: cred.expires_at ? dayjs(cred.expires_at) : undefined,
        scope: cred.scope,
        allowed_roles: cred.allowed_roles || [],
      })
    } else {
      setEditingId(null)
      form.resetFields()
    }
    setDrawerOpen(true)
  }, [form])

  const handleSave = () => {
    form.validateFields().then((values) => {
      // 过期时间留空 = 长期有效（后端空串即长期语义，status 恒 active）
      const expiresAt = values.expires_at ? values.expires_at.format('YYYY-MM-DD') : ''
      const content = buildContent(values.type, values as unknown as Record<string, unknown>)

      // SSH Key 类型即时校验：内容必须含 PEM 头尾（此前密码误存为 SSH Key，
      // 到测试 SSH 时才报「私钥文书解析损坏」，用户无法定位——输入端当场拦下）
      if (values.type === 'SSH Key' && content) {
        const lowered = content.toLowerCase()
        if (!lowered.includes('-----begin') || !lowered.includes('private key-----')) {
          message.error('内容不是有效的 SSH 私钥（应包含 -----BEGIN … PRIVATE KEY----- 头尾）。若要存密码请把类型改为「密码凭证」')
          return
        }
      }

      // SSH Key 编辑态换成内容重填时同样即时校验（上面已拦），这里只补创建端提示
      // AK/SK 必须成对填写：半填会把 content 里另一半覆盖成空串（账单同步/云区域拉取会突然鉴权失败）
      if (values.type === 'AccessKey') {
        const ak = String(values.access_key || '').trim()
        const sk = String(values.secret_key || '').trim()
        if ((ak && !sk) || (!ak && sk)) {
          message.error('Access Key ID 与 Access Key Secret 需要成对填写，留空表示不改动')
          return
        }
      }

      if (editingId) {
        // 换类型但没填新密文：旧密文（如明文密码）会挂在错误类型（如 SSH Key）下，拦截并提示
        const original = credentials.find((c) => c.cred_id === editingId)
        if (original && original.type !== values.type && !content) {
          message.error('凭证类型已变更，请填写新类型对应的密文内容')
          return
        }
        // 编辑：密文字段留空即不改动；填了才替换
        const payload: CredentialUpdate = {
          name: values.name,
          type: values.type,
          expires_at: expiresAt,
          scope: values.scope,
          allowed_roles: values.allowed_roles || [],
          replace_content: !!content,
          content: content || undefined,
        }
        saveMut.mutate({ id: editingId, data: payload })
      } else {
        const payload: CredentialCreate = {
          name: values.name,
          type: values.type,
          expires_at: expiresAt,
          scope: values.scope,
          allowed_roles: values.allowed_roles || [],
          content,
        }
        saveMut.mutate({ data: payload })
      }
    })
  }

  // ── 导出执行逻辑 ──
  // audit：本地生成的脱敏审计表（只含元数据，不含密文），无需取票；
  // encrypted：调后端批量导出接口（ZIP 附件 + manifest 审计清单），走 credentials:export 票据。
  const handleExecuteExport = () => {
    if (exportMode === 'audit') {
      const headers = ['凭证ID', '凭证名称', '类型', '适用范围', '授权权限组', '创建时间', '过期时间', '状态', '敏感内容']
      const rows = filtered.map((c) => [
        c.cred_id,
        `"${c.name}"`,
        TYPE_MAP[c.type]?.text || c.type,
        `"${c.scope || '-'}"`,
        `"${(c.allowed_roles || []).join('/') || '全部'}"`,
        c.created_at || '-',
        c.expires_at || '长期有效',
        STATUS_MAP[c.status || '']?.text || c.status || '-',
        '******(已脱敏隐藏)',
      ])
      const csvContent = '\uFEFF' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
      const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `OpsPilot_Credentials_Audit_${dayjs().format('YYYYMMDD')}.csv`
      link.click()
      URL.revokeObjectURL(link.href)
      message.success('合规审计脱敏表已成功导出')
      setExportModalOpen(false)
      return
    }

    const ids = filtered.map((c) => c.cred_id)
    if (!ids.length) {
      message.warning('没有可导出的凭证')
      return
    }
    confirm('credentials:export', async () => {
      setExporting(true)
      try {
        const res = await credentialsApi.batchExportFile(ids)
        downloadBlob(
          res.data,
          `OpsPilot_Credentials_Export_${dayjs().format('YYYYMMDD')}.zip`,
          res.headers?.['content-disposition'],
        )
        message.success('凭证备份包已导出（含 manifest 审计清单）')
        setExportModalOpen(false)
      } catch (err) {
        message.error(extractAxiosDetail(err) ?? '导出失败（缺少导出权限或目标凭证不可见）')
      } finally {
        setExporting(false)
      }
    })
  }

  // ── 单条下载密钥文件：后端真实明文导出（credentials:export 票据 + blob 附件） ──
  const handleDownloadSingleKey = (cred: CredentialResponse) => {
    confirm('credentials:export', async () => {
      try {
        const res = await credentialsApi.exportFile(cred.cred_id)
        downloadBlob(
          res.data,
          `${cred.name.replace(/\s+/g, '_')}_key.pem`,
          res.headers?.['content-disposition'],
        )
        message.success(`已导出密钥文件（${cred.name}）`)
      } catch (err) {
        message.error(extractAxiosDetail(err) ?? '导出失败（缺少导出权限或凭证不可见）')
      }
    })
  }

  const columns: ColumnsType<CredentialResponse> = [
    { title: '名称', dataIndex: 'name', width: 200, fixed: 'left', render: (v: string, r) => <a onClick={() => openDrawer(r)}>{v}</a> },
    {
      title: '类型', dataIndex: 'type', width: 120,
      render: (t: string) => {
        const item = TYPE_MAP[t] || { text: t, color: 'default' }
        return <Tag color={item.color}>{item.text}</Tag>
      },
    },
    { title: '适用范围', dataIndex: 'scope', width: 140, render: (v: string) => v || '-' },
    {
      title: '被引用', dataIndex: 'used_by', width: 90,
      render: (v: number) => (v > 0 ? <Tag color="blue">{v} 个资产</Tag> : <Tag style={{ opacity: 0.6 }}>未使用</Tag>),
    },
    {
      title: '授权权限组', dataIndex: 'allowed_roles', width: 180,
      render: (roles: string[]) => roles?.length
        ? <Space size={4} wrap>{roles.map((r) => <Tag key={r}>{r}</Tag>)}</Space>
        : <Tag>全部</Tag>,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 120, render: (v: string) => v || '-' },
    { title: '过期时间', dataIndex: 'expires_at', width: 120, render: (v: string) => v
      ? v
      : <Tag color="blue">长期有效</Tag> },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (s: string) => {
        const item = STATUS_MAP[s] || { text: s, color: 'default' }
        return <Tag color={item.color}>{item.text}</Tag>
      },
    },
    {
      title: '操作', key: 'action', width: 110, fixed: 'right',
      render: (_: unknown, r) => {
        const isSshKey = r.type === 'SSH Key'
        const moreItems: MenuProps['items'] = [
          ...(isSshKey ? [{ key: 'export', icon: <DownloadOutlined />, label: '导出 .pem 密钥' }] : []),
          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'export') handleDownloadSingleKey(r)
          if (key === 'delete') {
            modal.confirm({
              title: `删除凭证 ${r.name}`,
              content: (r.used_by ?? 0) > 0
                ? `该凭证仍被 ${r.used_by} 个资产引用，删除将被拒绝。确定继续？`
                : '此操作不可恢复，确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('credentials:delete', () => deleteMut.mutate(r.cred_id)),
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
    <Flex vertical gap={12} style={{ height: '100%' }}>
      <Flex justify="space-between" wrap gap={8}>
        <Space>
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索名称或用户名"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
            style={{ width: 240 }}
          />
          <Select
            placeholder="筛选类型"
            allowClear
            style={{ width: 140 }}
            options={Object.entries(TYPE_MAP).map(([k, v]) => ({ value: k, label: v.text }))}
            value={typeFilter}
            onChange={(v) => setTypeFilter(v ?? null)}
          />
          {selectedKeys.length > 0 && (
            <Button danger icon={<DeleteOutlined />}
              onClick={() => confirm('credentials:delete', () => batchDeleteMut.mutate())}
            >
              批量删除 ({selectedKeys.length})
            </Button>
          )}
        </Space>

        <Space>
          <Button icon={<ExportOutlined />} onClick={() => setExportModalOpen(true)}>
            导出凭证
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新增凭证</Button>
        </Space>
      </Flex>

      <Table<CredentialResponse>
        rowKey="cred_id"
        rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as string[]) }}
        columns={columns}
        dataSource={filtered}
        loading={isLoading}
        size="middle"
        scroll={{ x: 1400 }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条凭证` }}
      />

      {/* ── 新增/编辑抽屉 ── */}
      <Drawer
        title={editingId ? '编辑凭证' : '新增凭证'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={480}
        extra={<Button type="primary" onClick={handleSave} loading={saveMut.isPending}>保存</Button>}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="凭证名称" rules={[{ required: true, message: '请输入凭证名称' }]}>
            <Input prefix={<KeyOutlined />} placeholder="例如 阿里云生产账号 AK/SK" />
          </Form.Item>
          <Form.Item name="type" label="凭证类型" rules={[{ required: true }]} initialValue="Password">
            <Select options={Object.entries(TYPE_MAP).map(([k, v]) => ({ value: k, label: v.text }))} />
          </Form.Item>

          <Form.Item
            name="expires_at" label="过期时间（可选）"
            extra="留空默认长期有效；AK/SK 与 SSH 密钥无硬性过期语义，填写后仅用于到期前 30 天预警"
          >
            <DatePicker
              style={{ width: '100%' }}
              placeholder="不填默认长期有效"
              // 编辑已过期凭证时要能保留原日期，只禁止「新选」过去的日期
              disabledDate={(d) => {
                if (editingId) {
                  const current = form.getFieldValue('expires_at')
                  return d.isBefore(dayjs(), 'day') && !current?.isSame?.(d, 'day')
                }
                return d.isBefore(dayjs(), 'day')
              }}
            />
          </Form.Item>

          <Form.Item name="scope" label="适用范围（可选）">
            <Input placeholder="例如 阿里云华东1 / 生产集群" />
          </Form.Item>

          <Form.Item
            name="allowed_roles" label="授权权限组"
            extra="不选择表示所有权限组均可使用该凭证"
          >
            <Select
              mode="multiple" allowClear placeholder="默认全部权限组可用"
              options={accessRoles.map((r) => ({ value: r.role, label: `${r.title}${r.description ? ` — ${r.description}` : ''}` }))}
            />
          </Form.Item>

          {credType === 'Password' && (
            <Form.Item name="password" label="密码" rules={editingId ? [] : [{ required: true, message: '请输入密码' }]}>
              <Input.Password placeholder={editingId ? '留空则不修改' : '请输入密码'} />
            </Form.Item>
          )}

          {credType === 'SSH Key' && (
            <Form.Item name="private_key" label="私钥" rules={editingId ? [] : [{ required: true, message: '请粘贴私钥内容' }]}>
              <Input.TextArea rows={6} placeholder={editingId ? '留空则不修改' : '-----BEGIN OPENSSH PRIVATE KEY-----'} />
            </Form.Item>
          )}

          {credType === 'Token' && (
            <Form.Item name="token" label="Token" rules={editingId ? [] : [{ required: true, message: '请输入 Token' }]}>
              <Input.Password placeholder={editingId ? '留空则不修改' : '请输入 API Token'} />
            </Form.Item>
          )}

          {credType === 'AccessKey' && (
            <>
              <Form.Item name="access_key" label="Access Key ID" rules={editingId ? [] : [{ required: true, message: '请输入 Access Key ID' }]}>
                <Input placeholder={editingId ? '留空则不修改' : '例如 LTAIxxxxxxxxxxxxx'} />
              </Form.Item>
              <Form.Item name="secret_key" label="Access Key Secret" rules={editingId ? [] : [{ required: true, message: '请输入 Access Key Secret' }]}>
                <Input.Password placeholder={editingId ? '留空则不修改' : '请输入 Access Key Secret'} />
              </Form.Item>
              <Text type="secondary" style={{ fontSize: 12 }}>
                保存后 AK/SK 将以加密形式入库，页面与列表永不回显；阿里云账单、证书自动签发等模块可直接引用。
              </Text>
            </>
          )}
        </Form>
      </Drawer>

      {/* ── 凭证合规导出 Modal ── */}
      <Modal
        title={
          <Flex align="center" gap={8}>
            <SafetyCertificateOutlined style={{ color: '#1677ff' }} />
            <span>凭证合规导出与安全备份</span>
          </Flex>
        }
        open={exportModalOpen}
        onCancel={() => setExportModalOpen(false)}
        onOk={handleExecuteExport}
        confirmLoading={exporting}
        okText="立即导出"
        cancelText="取消"
        width={520}
      >
        <Flex vertical gap={16} style={{ paddingTop: 12 }}>
          <Radio.Group value={exportMode} onChange={(e) => setExportMode(e.target.value)}>
            <Flex vertical gap={12}>
              <Radio value="audit">
                <div>
                  <Text style={{ fontWeight: 600 }}>1. 合规审计脱敏表 (CSV)</Text>
                  <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
                    适合存档与审计。导出账号名称、类型、用户名、更新时间等审计字段，所有密码与私钥自动打掩码脱敏。
                  </Paragraph>
                </div>
              </Radio>

              <Radio value="encrypted">
                <div>
                  <Text style={{ fontWeight: 600 }}>2. 凭证备份包 (.zip)</Text>
                  <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
                    适合运维迁移与应急恢复。从服务端导出当前筛选凭证的明文内容并打包为 ZIP（附 manifest.json 审计清单），需二次确认并记录审计。
                  </Paragraph>
                </div>
              </Radio>
            </Flex>
          </Radio.Group>
        </Flex>
      </Modal>
    </Flex>
  )
}
