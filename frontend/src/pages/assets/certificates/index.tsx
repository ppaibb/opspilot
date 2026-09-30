import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, App, Tooltip,
  Badge, Progress, Flex, Card, Statistic, Row, Col, Switch, Tabs, Radio, Alert, Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, SafetyCertificateOutlined,
  CloudUploadOutlined, SyncOutlined, StopOutlined, SendOutlined,
  CheckCircleOutlined, ClockCircleOutlined, DownloadOutlined,
  HddOutlined, CloudOutlined, ClusterOutlined, DeploymentUnitOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { certificatesApi, assetsApi, credentialsApi, clustersApi } from '@/api'
import { useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import { downloadBlob } from '@/utils/download'
import GroupSidebar from '@/components/GroupSidebar'
import type {
  CertificateResponse, CertificateIssueRequest, CertificateUploadRequest,
  CertificateDeployRequest, AssetResponse, CredentialResponse, ClusterResponse,
} from '@/types/api'

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  valid:    { text: '有效',     color: 'success' },
  issued:   { text: '已签发',   color: 'success' },
  issuing:  { text: '签发中',   color: 'processing' },
  renewing: { text: '续期中',   color: 'processing' },
  pending:  { text: '申请中',   color: 'processing' },
  expiring: { text: '即将到期', color: 'warning' },
  expired:  { text: '已过期',   color: 'error' },
  failed:   { text: '失败',     color: 'error' },
  revoked:  { text: '已吊销',   color: 'default' },
}

function daysLeft(notAfter?: string): number | null {
  if (!notAfter) return null
  const exp = dayjs(notAfter)
  if (!exp.isValid()) return null
  return exp.diff(dayjs(), 'day')
}

export default function CertificatesPage() {
  const queryClient = useQueryClient()
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [groupFilter, setGroupFilter] = useState<number | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [mode, setMode] = useState<'issue' | 'upload'>('issue')

  // 部署任务大抽屉控制
  const [deployDrawerOpen, setDeployDrawerOpen] = useState(false)
  const [deployCert, setDeployCert] = useState<CertificateResponse | null>(null)
  const [deployTab, setDeployTab] = useState<'host' | 'cloud' | 'k8s'>('host')

  const [issueForm] = Form.useForm<CertificateIssueRequest>()
  const [uploadForm] = Form.useForm<CertificateUploadRequest>()
  const [deployForm] = Form.useForm<CertificateDeployRequest>()

  const { confirm } = useRiskConfirm()
  const { message, modal } = App.useApp()

  // 依赖数据
  const { data: hosts = [] } = useQuery<AssetResponse[]>({ queryKey: ['hosts'], queryFn: () => assetsApi.list() })
  const { data: credentials = [] } = useQuery<CredentialResponse[]>({ queryKey: ['credentials'], queryFn: () => credentialsApi.list() })
  const { data: clusters = [] } = useQuery<ClusterResponse[]>({ queryKey: ['clusters'], queryFn: () => clustersApi.list() })

  const { data: certs = [], isLoading } = useQuery<CertificateResponse[]>({
    queryKey: ['certificates'],
    queryFn: () => certificatesApi.list(),
  })

  const { deleteMut } = useCrudResource<CertificateIssueRequest, string>({
    queryKey: ['certificates'],
    entityName: '证书',
    createFn: certificatesApi.issue,
    // 编辑走 PUT /certificates/{id} 原地更新；此前误指向 issue 会变成重新签发一张新证书
    updateFn: (id, data) =>
      certificatesApi.update(id, {
        name: data.name,
        san_domains: data.san_domains,
        key_type: data.key_type,
        auto_renew: data.auto_renew,
        renew_before_days: data.renew_before_days,
        remark: data.remark,
        deploy_targets: data.deploy_targets,
      }),
    deleteFn: certificatesApi.delete,
  })

  const batchDeleteMut = useMutation({
    mutationFn: () => certificatesApi.batchDelete(selectedKeys),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      setSelectedKeys([])
      message.success(`已删除 ${selectedKeys.length} 个证书`)
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '批量删除失败'),
  })

  const renewMut = useMutation({
    mutationFn: (id: string) => certificatesApi.renew(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      message.success('已触发自动续期任务')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '续期失败'),
  })

  const revokeMut = useMutation({
    mutationFn: (id: string) => certificatesApi.revoke(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      message.success('已吊销证书')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '吊销失败'),
  })

  const autoRenewMut = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => certificatesApi.autoRenew(id, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['certificates'] }),
  })

  /** 下载证书文件包（cert/key/fullchain/ca 打包 zip）：certificates:download 票据 + blob 附件 */
  const handleDownload = (cert: CertificateResponse) => {
    const targetId = cert.cert_id || cert.id || ''
    confirm('certificates:download', async () => {
      try {
        const res = await certificatesApi.downloadFile(targetId)
        downloadBlob(
          res.data,
          `${(cert.primary_domain || targetId).replace(/\*/g, 'wildcard').replace(/\//g, '-')}.zip`,
          res.headers?.['content-disposition'],
        )
        message.success('证书文件包已下载')
      } catch (err) {
        message.error(extractAxiosDetail(err) ?? '下载失败（证书文件缺失或无下载权限）')
      }
    })
  }

  const issueMut = useMutation({
    mutationFn: (data: CertificateIssueRequest) => certificatesApi.issue(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      setDrawerOpen(false)
      message.success('已提交签发请求')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '签发失败，请检查域名和 ACME 账号'),
  })

  const uploadMut = useMutation({
    mutationFn: (data: CertificateUploadRequest) => certificatesApi.upload(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      setDrawerOpen(false)
      message.success('上传成功')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '上传失败，请检查证书格式'),
  })

  const deployMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: CertificateDeployRequest }) => certificatesApi.deploy(id, data),
    onSuccess: (res: { message?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      setDeployDrawerOpen(false)
      message.success(res?.message || '部署任务已成功派发执行')
    },
    onError: (err: unknown) => {
      message.error((err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '部署任务下发失败')
    },
  })

  // ── 统计指标 ──
  const totalCount = certs.length
  const validCount = certs.filter((c) => c.status === 'valid' || c.status === 'issued').length
  const expiringCount = certs.filter((c) => {
    const days = daysLeft(c.not_after)
    return c.status === 'expiring' || (days !== null && days >= 0 && days <= 30)
  }).length
  const autoRenewCount = certs.filter((c) => c.auto_renew).length

  const filtered = certs.filter((c) =>
    !search || (c.name && c.name.includes(search)) || (c.primary_domain && c.primary_domain.includes(search)),
  )

  const openDrawer = () => {
    setMode('issue')
    issueForm.resetFields()
    uploadForm.resetFields()
    setDrawerOpen(true)
  }

  const openDeployDrawer = (cert: CertificateResponse) => {
    const targetId = cert.cert_id || cert.id || ''
    setDeployCert({ ...cert, id: targetId })
    setDeployTab('host')
    deployForm.resetFields()
    deployForm.setFieldsValue({
      target_type: 'host',
      cert_path: `/etc/nginx/ssl/${cert.primary_domain}.crt`,
      key_path: `/etc/nginx/ssl/${cert.primary_domain}.key`,
      reload_cmd: 'systemctl reload nginx',
    })
    setDeployDrawerOpen(true)
  }

  const handleSubmit = () => {
    if (mode === 'issue') {
      issueForm.validateFields().then((v) => issueMut.mutate(v))
    } else {
      uploadForm.validateFields().then((v) => uploadMut.mutate(v))
    }
  }

  const handleExecuteDeploy = () => {
    if (!deployCert) return
    const targetId = deployCert.cert_id || deployCert.id || ''
    deployForm.validateFields().then((values) => {
      // 表单可能携带旧字段别名（host_ids/cert_dest_path 等），提交前统一归一
      const vals = values as CertificateDeployRequest & {
        host_ids?: string[]
        cert_dest_path?: string
        key_dest_path?: string
        post_script?: string
      }
      const asset_ids = vals.host_ids || vals.asset_ids || []
      const cert_dest_path = vals.cert_path || vals.cert_dest_path || ''
      const key_dest_path = vals.key_path || vals.key_dest_path || ''
      const post_script = vals.reload_cmd || vals.post_script || ''

      deployMut.mutate({
        id: targetId,
        data: {
          ...values,
          target_type: deployTab,
          asset_ids,
          host_ids: asset_ids,
          cert_dest_path,
          key_dest_path,
          post_script,
          cert_path: cert_dest_path,
          key_path: key_dest_path,
          reload_cmd: post_script,
        },
      })
    })
  }

  const columns: ColumnsType<CertificateResponse> = [
    {
      title: '证书名称', dataIndex: 'name', width: 180, fixed: 'left',
      render: (v: string, r) => v || r.primary_domain || '-',
    },
    { title: '主域名', dataIndex: 'primary_domain', width: 170, render: (v: string) => v || '-' },
    {
      title: 'SAN 备用域名', dataIndex: 'san_domains', width: 140,
      render: (v: string[]) => v?.length ? <Tooltip title={v.join(', ')}><Tag>{v.length} 个</Tag></Tooltip> : '-',
    },
    {
      title: '部署任务', dataIndex: 'deploy_targets', width: 220,
      render: (v: string) => {
        if (!v) return <Tag style={{ opacity: 0.6 }}>未配置部署任务</Tag>
        return (
          <Tooltip title={`已绑定部署目标: ${v}`}>
            <Space size={4} wrap>
              {v.includes('ALIYUN') || v.includes('TENCENT') ? (
                <Tag color="orange" icon={<CloudOutlined />}>云服务 CDN/ALB</Tag>
              ) : v.includes('k8s') || v.includes('secret') ? (
                <Tag color="purple" icon={<ClusterOutlined />}>K8s Secret</Tag>
              ) : (
                <Tag color="blue" icon={<HddOutlined />}>{v.length > 18 ? `${v.slice(0, 16)}...` : v}</Tag>
              )}
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (s: string) => {
        // 未知状态回落原文，不能默认 revoked（会把签发中/失败误显示成已吊销）
        const item = STATUS_MAP[s] ?? { text: s || '-', color: 'default' }
        return <Badge status={item.color as 'success' | 'processing' | 'warning' | 'error' | 'default'} text={item.text} />
      },
    },
    {
      title: '剩余有效时间', dataIndex: 'not_after', width: 200,
      render: (v: string) => {
        if (!v) return '-'
        const days = daysLeft(v)
        if (days === null) return '-'
        let status: 'normal' | 'exception' | 'active' = 'normal'
        if (days < 0) status = 'exception'
        else if (days <= 30) status = 'exception'

        const percent = Math.max(0, Math.min(100, Math.round((days / 90) * 100)))
        return (
          <div>
            <Flex justify="space-between" style={{ fontSize: 12 }}>
              <span>{dayjs(v).format('YYYY-MM-DD')}</span>
              <span style={{ color: days < 0 ? '#ff4d4f' : days <= 30 ? '#faad14' : '#52c41a' }}>
                {days < 0 ? '已过期' : `${days} 天`}
              </span>
            </Flex>
            <Progress percent={percent} size="small" status={status} showInfo={false} />
          </div>
        )
      },
    },
    {
      title: '自动续期', dataIndex: 'auto_renew', width: 90,
      render: (v: boolean, r) => {
        const targetId = r.cert_id || r.id || ''
        // 手动上传（custom）证书无 ACME 账号，续期必然失败——禁用开关并说明
        const isCustom = r.provider === 'custom' || r.acme_server === 'custom'
        if (isCustom) {
          return (
            <Tooltip title="手动上传的证书没有 ACME 账号，无法自动续期；请在到期前手动更新">
              <Switch size="small" checked={false} disabled />
            </Tooltip>
          )
        }
        return (
          <Switch size="small" checked={!!v} onChange={(checked) => autoRenewMut.mutate({ id: targetId, enabled: checked })} />
        )
      },
    },
    {
      title: '操作', key: 'action', width: 130, fixed: 'right',
      render: (_: unknown, r) => {
        const targetId = r.cert_id || r.id || ''
        const moreItems: MenuProps['items'] = [
          { key: 'revoke', icon: <StopOutlined />, label: '吊销证书', danger: true },
          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'revoke') {
            confirm('certificates:revoke', () => revokeMut.mutate(targetId))
          } else if (key === 'delete') {
            modal.confirm({
              title: `删除证书 ${r.name || r.primary_domain}`,
              content: '此操作不可恢复，确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('certificates:delete', () => deleteMut.mutate(targetId)),
            })
          }
        }
        return (
          <Space size={4}>
            <Tooltip title="配置多模态部署任务">
              <Button size="small" type="primary" ghost icon={<DeploymentUnitOutlined />} onClick={() => openDeployDrawer(r)}>
                部署任务
              </Button>
            </Tooltip>
            <Tooltip title="手动触发续期">
              <Button size="small" type="text" icon={<SyncOutlined />} onClick={() => renewMut.mutate(targetId)} loading={renewMut.isPending} />
            </Tooltip>
            <Tooltip title="下载证书文件包（cert/key/fullchain）">
              <Button size="small" type="text" icon={<DownloadOutlined />} onClick={() => handleDownload(r)} />
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
        resourceType="certificate"
        selectedGroupId={groupFilter}
        onSelect={setGroupFilter}
        selectedIds={selectedKeys}
      />

      <Flex vertical gap={16} style={{ flex: 1, minWidth: 0 }}>
        {/* 顶部概览卡片 */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>证书总数</span>}
                value={totalCount}
                prefix={<SafetyCertificateOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20 }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>有效证书</span>}
                value={validCount}
                prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>即将到期（30天）</span>}
                value={expiringCount}
                prefix={<ClockCircleOutlined style={{ color: '#faad14', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: expiringCount > 0 ? '#faad14' : 'inherit' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>自动续期开启</span>}
                value={autoRenewCount}
                prefix={<SyncOutlined style={{ color: '#722ed1', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: '#722ed1' }}
              />
            </Card>
          </Col>
        </Row>

        {/* 工具栏 */}
        <Flex justify="space-between" wrap gap={8}>
          <Space>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索证书名称或主域名"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              allowClear
              style={{ width: 240 }}
            />
            {selectedKeys.length > 0 && (
              <Button danger icon={<DeleteOutlined />} onClick={() => confirm('certificates:delete', () => batchDeleteMut.mutate())}>
                批量删除 ({selectedKeys.length})
              </Button>
            )}
          </Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={openDrawer}>申请 / 上传证书</Button>
        </Flex>

        <Table<CertificateResponse>
          rowKey={(r) => r.cert_id || r.id || r.primary_domain}
          rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as string[]) }}
          columns={columns}
          dataSource={filtered}
          loading={isLoading}
          size="middle"
          scroll={{ x: 1300 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个证书` }}
        />
      </Flex>

      {/* ── 申请 / 上传 Drawer ── */}
      <Drawer
        title="申请 / 上传证书"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={500}
        extra={
          <Button type="primary" onClick={handleSubmit} loading={issueMut.isPending || uploadMut.isPending}>
            {mode === 'issue' ? '申请签发' : '确认上传'}
          </Button>
        }
      >
        <Radio.Group
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          buttonStyle="solid"
          style={{ marginBottom: 20, width: '100%' }}
        >
          <Radio.Button value="issue" style={{ width: '50%', textAlign: 'center' }}>
            <SafetyCertificateOutlined /> ACME 自动申请
          </Radio.Button>
          <Radio.Button value="upload" style={{ width: '50%', textAlign: 'center' }}>
            <CloudUploadOutlined /> 手动上传已有证书
          </Radio.Button>
        </Radio.Group>

        {mode === 'issue' ? (
          <Form form={issueForm} layout="vertical">
            <Form.Item name="name" label="证书备注名称" rules={[{ required: true }]}>
              <Input placeholder="例如 生产环境 *.opspilot.com 证书" />
            </Form.Item>
            <Form.Item name="domains" label="域名列表（逗号分隔或多行）" rules={[{ required: true }]}>
              <Input.TextArea rows={3} placeholder="*.example.com, example.com" />
            </Form.Item>
            <Form.Item name="acme_account_id" label="ACME 账号凭证">
              <Select placeholder="使用 Let's Encrypt 默认账号" allowClear />
            </Form.Item>
            <Form.Item name="auto_renew" label="自动续期" valuePropName="checked" initialValue={true}>
              <Switch />
            </Form.Item>
          </Form>
        ) : (
          <Form form={uploadForm} layout="vertical">
            <Form.Item name="name" label="证书备注名称" rules={[{ required: true }]}>
              <Input placeholder="例如 通配符证书 2026" />
            </Form.Item>
            <Form.Item name="cert_content" label="证书内容 (CRT/PEM)" rules={[{ required: true }]}>
              <Input.TextArea rows={6} placeholder="-----BEGIN CERTIFICATE-----" />
            </Form.Item>
            <Form.Item name="key_content" label="私钥内容 (KEY)" rules={[{ required: true }]}>
              <Input.TextArea rows={6} placeholder="-----BEGIN RSA PRIVATE KEY-----" />
            </Form.Item>
          </Form>
        )}
      </Drawer>

      {/* ── 🚀 全功能「多模态证书部署任务」大抽屉 ── */}
      <Drawer
        title={
          <Flex align="center" gap={8}>
            <DeploymentUnitOutlined style={{ color: '#1677ff', fontSize: 18 }} />
            <span>证书自动化部署任务中心 · {deployCert?.primary_domain}</span>
          </Flex>
        }
        open={deployDrawerOpen}
        onClose={() => setDeployDrawerOpen(false)}
        width={600}
        extra={
          <Button type="primary" icon={<SendOutlined />} onClick={handleExecuteDeploy} loading={deployMut.isPending}>
            下发部署任务
          </Button>
        }
      >
        <Alert
          message="证书多模态分发机制"
          description="支持将 SSL 证书一键安全分发至 Nginx Linux 服务器主机、云服务商 CDN/ALB 域名节点，或 Kubernetes 集群 TLS Secret 中。"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />

        <Tabs
          activeKey={deployTab}
          onChange={(k) => setDeployTab(k as 'host' | 'cloud' | 'k8s')}
          items={[
            {
              key: 'host',
              label: (
                <span>
                  <HddOutlined style={{ marginRight: 6 }} />部署到 Linux 主机
                </span>
              ),
            },
            {
              key: 'cloud',
              label: (
                <span>
                  <CloudOutlined style={{ marginRight: 6 }} />部署到云服务 CDN/ALB
                </span>
              ),
            },
            {
              key: 'k8s',
              label: (
                <span>
                  <ClusterOutlined style={{ marginRight: 6 }} />部署到 K8s Secret
                </span>
              ),
            },
          ]}
        />

        <Form form={deployForm} layout="vertical" style={{ paddingTop: 12 }}>
          {deployTab === 'host' && (
            <>
              <Form.Item name="host_ids" label="目标分发主机" rules={[{ required: true, message: '请选择至少一台主机' }]}>
                <Select
                  mode="multiple"
                  placeholder="选择需要落盘部署证书的服务器"
                  options={hosts.map((h: AssetResponse) => ({ value: String(h.id), label: `${h.name} (${h.ip})` }))}
                />
              </Form.Item>
              <Form.Item name="cert_path" label="证书落盘绝对路径 (Certificate)" rules={[{ required: true }]}>
                <Input placeholder="例如 /etc/nginx/ssl/fullchain.pem" />
              </Form.Item>
              <Form.Item name="key_path" label="私钥落盘绝对路径 (Private Key)" rules={[{ required: true }]}>
                <Input placeholder="例如 /etc/nginx/ssl/privkey.pem" />
              </Form.Item>
              <Form.Item name="reload_cmd" label="证书更新后执行的服务重载脚本">
                <Input placeholder="例如 systemctl reload nginx 或 docker restart nginx" />
              </Form.Item>
            </>
          )}

          {deployTab === 'cloud' && (
            <>
              <Form.Item name="cloud_credential" label="云服务商 API 密钥凭证" rules={[{ required: true }]}>
                <Select
                  placeholder="选择阿里云 / 腾讯云 / AWS AccessKey 凭证"
                  options={credentials
                    .filter((c: CredentialResponse) => c.type === 'AccessKey')
                    .map((c: CredentialResponse) => ({ value: c.cred_id, label: `${c.name} (${c.type})` }))}
                />
              </Form.Item>
              <Form.Item name="cloud_service" label="云服务产品类型" initialValue="cdn">
                <Radio.Group buttonStyle="solid">
                  <Radio.Button value="cdn">阿里云 / 腾讯云 CDN</Radio.Button>
                  <Radio.Button value="alb">ALB / CLB 负载均衡</Radio.Button>
                  <Radio.Button value="waf">WAF 应用防火墙</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Form.Item name="domain_instance" label="云资源关联的实例域名 / ID" rules={[{ required: true }]}>
                <Input placeholder="例如 cdn.opspilot.com 或 alb-instance-889a" />
              </Form.Item>
            </>
          )}

          {deployTab === 'k8s' && (
            <>
              <Form.Item name="cluster_id" label="目标 Kubernetes 集群" rules={[{ required: true }]}>
                <Select
                  placeholder="选择需要更新 TLS Secret 的集群"
                  options={clusters.map((cl: ClusterResponse) => ({ value: cl.cluster_id || cl.name, label: `${cl.name} (${cl.provider})` }))}
                />
              </Form.Item>
              <Form.Item name="namespace" label="命名空间 (Namespace)" initialValue="default" rules={[{ required: true }]}>
                <Input placeholder="例如 default 或 prod" />
              </Form.Item>
              <Form.Item name="secret_name" label="TLS Secret 名称" rules={[{ required: true }]}>
                <Input placeholder={`例如 tls-${deployCert?.primary_domain?.replace(/\./g, '-') || 'opspilot'}`} />
              </Form.Item>
            </>
          )}
        </Form>
      </Drawer>
    </Flex>
  )
}
