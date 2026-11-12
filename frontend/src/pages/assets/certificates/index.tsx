import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, InputNumber, Tag, Drawer, Form, Select, App, Tooltip,
  Badge, Flex, Card, Statistic, Row, Col, Switch, Tabs, Radio, Dropdown, Spin,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, SafetyCertificateOutlined,
  CloudUploadOutlined, SyncOutlined, StopOutlined, SendOutlined,
  CheckCircleOutlined, ClockCircleOutlined, DownOutlined,
  HddOutlined, CloudOutlined, ClusterOutlined, DeploymentUnitOutlined,
  FileTextOutlined, HistoryOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { certificatesApi, assetsApi, credentialsApi, clustersApi, domainsApi } from '@/api'
import { riskTicketConfig } from '@/api/client'
import { useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import { downloadBlob } from '@/utils/download'
import GroupSidebar from '@/components/GroupSidebar'
import type {
  CertificateResponse, CertificateLogsResponse, CertificateIssueRequest, CertificateUploadRequest,
  CertificateDeployRequest, CertificateDeployResponse, CertificateDeployment, CertificateAccountItem, AssetResponse, CredentialResponse, ClusterResponse,
  DomainResponse,
} from '@/types/api'
import { filterCertAccountsByDomain } from './certAccountFilter'
import {
  CERT_DEPLOY_POLL_INTERVAL_MS, deployStatusTagColor, deployStatusText,
  deploySuccessCount, deployTargetTypeText, hasRunningDeployment,
} from './certDeployHistory'
import {
  CERT_LIST_POLL_INTERVAL_MS, CERT_LOG_POLL_INTERVAL_MS,
  isCertLogErrorLine, isTransientCertStatus, shouldPollCertList,
} from './certIssueLog'
import {
  CERT_PROVIDER_FILTER_OPTIONS, CERT_STATUS_FILTER_OPTIONS, CERT_STATUS_BADGE_PULSE,
  certStatusBadgeBg, expiryDisplay, EXPIRY_TONE_COLOR, formatIssuer, formatKeyType,
  matchProviderFilter, matchStatusFilter,
} from './certTableMeta'
import type { CertProviderFilter } from './certTableMeta'

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
  // 搜索框草稿：点「搜索」或回车后才应用到 search，避免每次 keystroke 都重算列表
  const [searchInput, setSearchInput] = useState('')
  const [providerFilter, setProviderFilter] = useState<CertProviderFilter>('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [groupFilter, setGroupFilter] = useState<number | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [mode, setMode] = useState<'issue' | 'upload' | 'edit'>('issue')

  // 部署任务大抽屉控制
  const [deployDrawerOpen, setDeployDrawerOpen] = useState(false)
  const [deployCert, setDeployCert] = useState<CertificateResponse | null>(null)
  const [deployTab, setDeployTab] = useState<'host' | 'cloud' | 'k8s'>('host')

  // 签发日志抽屉：打开的证书（null 表示关闭）
  const [logCert, setLogCert] = useState<CertificateResponse | null>(null)
  const logContainerRef = useRef<HTMLDivElement | null>(null)

  // 部署历史抽屉：打开的证书（null 表示关闭）
  const [historyCert, setHistoryCert] = useState<CertificateResponse | null>(null)

  // 编辑证书：复用申请抽屉的编辑态，只改可编辑字段（名称、SAN、自动续期、提前续期天数、备注），不动签发参数
  const [editingCert, setEditingCert] = useState<CertificateResponse | null>(null)
  const [editForm] = Form.useForm()

  // 抽屉表单里的 san_domains 是逗号/换行分隔的文本，提交时再转成 string[]
  type IssueFormValues = Omit<CertificateIssueRequest, 'san_domains'> & { san_domains?: string }
  const [issueForm] = Form.useForm<IssueFormValues>()
  const [uploadForm] = Form.useForm<CertificateUploadRequest>()
  const [deployForm] = Form.useForm<CertificateDeployRequest>()

  const { confirm } = useRiskConfirm()
  const { message, modal } = App.useApp()

  // 依赖数据
  const { data: hosts = [] } = useQuery<AssetResponse[]>({ queryKey: ['hosts'], queryFn: () => assetsApi.list() })
  const { data: credentials = [] } = useQuery<CredentialResponse[]>({ queryKey: ['credentials'], queryFn: () => credentialsApi.list() })
  const { data: clusters = [] } = useQuery<ClusterResponse[]>({ queryKey: ['clusters'], queryFn: () => clustersApi.list() })
  // 可用于 ACME 签发的 DNS 账号（后端 GET /certificates/accounts，仅返回已实现签发插件的厂商）
  const { data: acmeAccounts = [] } = useQuery<CertificateAccountItem[]>({
    queryKey: ['certificate-accounts'],
    queryFn: () => certificatesApi.accounts(),
  })
  // 域名资产（用于按主域名所属厂商过滤 DNS 校验凭证）
  const { data: domains = [], isLoading: isDomainsLoading } = useQuery<DomainResponse[]>({
    queryKey: ['domains'],
    queryFn: () => domainsApi.list(),
  })

  // 按主域名所属厂商过滤 DNS 校验凭证；主域名为空或未在域名资产中登记时不过滤
  const watchedPrimaryDomain = Form.useWatch('primary_domain', issueForm)
  const filteredCertAccounts = useMemo(
    () => filterCertAccountsByDomain(watchedPrimaryDomain ?? '', domains, acmeAccounts),
    [watchedPrimaryDomain, domains, acmeAccounts],
  )
  const showDomainUnknownHint =
    !!watchedPrimaryDomain?.trim() && !filteredCertAccounts.domainKnown && !isDomainsLoading
  // 主域名变化导致已选凭证不在过滤结果中时清空，避免提交错厂商的凭证
  useEffect(() => {
    const selected = issueForm.getFieldValue('account_id')
    if (selected && !filteredCertAccounts.accounts.some((a) => a.account_id === selected)) {
      issueForm.setFieldsValue({ account_id: undefined })
    }
  }, [filteredCertAccounts, issueForm])

  const { data: certs = [], isLoading } = useQuery<CertificateResponse[]>({
    queryKey: ['certificates', groupFilter],
    queryFn: () => certificatesApi.list({ group_id: groupFilter ?? undefined }),
    // 有证书处于签发中/续期中时轮询，状态自己翻过去；无进行中任务时不轮询
    refetchInterval: (query) =>
      (shouldPollCertList((query.state.data ?? []).map((c) => c.status)) ? CERT_LIST_POLL_INTERVAL_MS : false),
  })

  // 签发日志：抽屉打开时拉取；证书处于进行中状态时轮询，终态停止
  const logCertId = logCert ? (logCert.cert_id || logCert.id || '') : ''
  // 取列表里的最新状态（列表轮询会刷新它），列表里还没有时用打开时的快照
  const logCertLive = (logCertId ? certs.find((c) => (c.cert_id || c.id) === logCertId) : undefined) ?? logCert
  const logCertStatus = logCertLive?.status ?? ''
  // 部署历史：历史抽屉或日志抽屉打开时拉取；有进行中记录时轮询，终态停止
  const historyCertId = historyCert ? (historyCert.cert_id || historyCert.id || '') : ''
  const activeDeployCertId = historyCertId || logCertId
  const { data: deployments = [] } = useQuery<CertificateDeployment[]>({
    queryKey: ['certificate-deployments', activeDeployCertId],
    queryFn: () => certificatesApi.deployments(activeDeployCertId),
    enabled: !!activeDeployCertId,
    refetchInterval: (query) =>
      (hasRunningDeployment(query.state.data ?? []) ? CERT_DEPLOY_POLL_INTERVAL_MS : false),
  })
  // 日志抽屉轮询条件：证书签发进行中，或该证书有部署进行中（部署日志写进同一文件）
  const logDeployRunning = !!logCertId && activeDeployCertId === logCertId && hasRunningDeployment(deployments)
  const { data: logData, isLoading: isLogLoading } = useQuery<CertificateLogsResponse>({
    queryKey: ['certificate-logs', logCertId],
    queryFn: () => certificatesApi.logs(logCertId),
    enabled: !!logCertId,
    refetchInterval: (isTransientCertStatus(logCertStatus) || logDeployRunning) ? CERT_LOG_POLL_INTERVAL_MS : false,
  })
  const logLines = useMemo(() => logData?.lines ?? [], [logData])
  // 有新日志行时滚到底
  useEffect(() => {
    const el = logContainerRef.current
    if (el && logLines.length > 0) el.scrollTop = el.scrollHeight
  }, [logLines])

  const { saveMut, deleteMut } = useCrudResource<CertificateIssueRequest, string>({
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
    // 保存成功后关闭抽屉（saveMut 在本页只用于编辑；新增走 issueMut）
    onSaved: () => { setDrawerOpen(false); setEditingCert(null) },
  })

  const batchDeleteMut = useMutation({
    mutationFn: (ticket: string) => certificatesApi.batchDelete(selectedKeys, riskTicketConfig(ticket)),
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
    mutationFn: ({ id, ticket }: { id: string; ticket: string }) =>
      certificatesApi.revoke(id, undefined, riskTicketConfig(ticket)),
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
    confirm('certificates:download', async (ticket) => {
      try {
        const res = await certificatesApi.downloadFile(targetId, riskTicketConfig(ticket))
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
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      setDrawerOpen(false)
      message.success('已提交签发请求')
      // 提交后直接打开该证书的日志抽屉看进度
      if (data && (data.cert_id || data.id)) setLogCert(data)
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '签发失败，请检查域名和 DNS 校验账号'),
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
    onSuccess: (res: CertificateDeployResponse, variables: { id: string; data: CertificateDeployRequest }) => {
      queryClient.invalidateQueries({ queryKey: ['certificates'] })
      queryClient.invalidateQueries({ queryKey: ['certificate-deployments'] })
      setDeployDrawerOpen(false)
      message.success(res?.message || '部署任务已提交，后台执行中')
      // 提交后直接打开该证书的日志抽屉看进度（与签发提交后自动打开同一模式）
      const target = certs.find((c) => (c.cert_id || c.id) === variables.id) ?? deployCert
      if (target) setLogCert(target)
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

  const filtered = certs.filter(
    (c) =>
      (!search || (c.name && c.name.includes(search)) || (c.primary_domain && c.primary_domain.includes(search))) &&
      matchProviderFilter(c.provider, providerFilter) &&
      matchStatusFilter(c.status, statusFilter),
  )

  const openDrawer = () => {
    setMode('issue')
    setEditingCert(null)
    issueForm.resetFields()
    uploadForm.resetFields()
    setDrawerOpen(true)
  }

  const openEditDrawer = (cert: CertificateResponse) => {
    setEditingCert(cert)
    setMode('edit')
    editForm.resetFields()
    editForm.setFieldsValue({
      name: cert.name,
      san_domains: (cert.san_domains ?? []).join(', '),
      auto_renew: cert.auto_renew,
      renew_before_days: cert.renew_before_days,
      remark: cert.remark,
    })
    setDrawerOpen(true)
  }

  const handleEditSubmit = () => {
    if (!editingCert) return
    const targetId = editingCert.cert_id || editingCert.id || ''
    editForm.validateFields().then((v) => {
      const san_domains = (v.san_domains ?? '')
        .split(/[,，\n]+/)
        .map((item: string) => item.trim())
        .filter(Boolean)
      // primary_domain / account_id 只是满足 CertificateIssueRequest 的必填类型；
      // updateFn 只取其中可编辑字段，不动签发参数
      saveMut.mutate({
        id: targetId,
        data: {
          name: v.name,
          primary_domain: editingCert.primary_domain,
          san_domains,
          account_id: editingCert.account_id ?? '',
          auto_renew: v.auto_renew,
          renew_before_days: v.renew_before_days,
          remark: v.remark,
        },
      })
    })
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
      issueForm.validateFields().then((v) => {
        // 表单字段与后端 CertificateIssueRequest 对齐：主域名必填、SAN 文本转数组、账号取 cred_id
        const san_domains = (v.san_domains ?? '')
          .split(/[,，\n]+/)
          .map((item) => item.trim())
          .filter(Boolean)
        issueMut.mutate({
          name: v.name,
          primary_domain: v.primary_domain.trim(),
          san_domains,
          account_id: v.account_id,
          auto_renew: v.auto_renew ?? true,
        })
      })
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
      title: '证书名称', dataIndex: 'name', width: 140, fixed: 'left',
      render: (v: string, r) => v || r.primary_domain || '-',
    },
    {
      title: '绑定域名', dataIndex: 'primary_domain', width: 180,
      render: (v: string, r) => (
        <div>
          <div>{v || '-'}</div>
          {r.san_domains?.length ? (
            <div style={{ color: '#8c8c8c', fontSize: 12 }}>{r.san_domains.join('，')}</div>
          ) : null}
        </div>
      ),
    },
    {
      title: '证书信息', width: 170,
      render: (_: unknown, r) => (
        <div>
          <div>签名算法：{formatKeyType(r.key_type)}</div>
          <div style={{ color: '#8c8c8c', fontSize: 12 }}>颁发机构：{formatIssuer(r.acme_server)}</div>
        </div>
      ),
    },
    {
      title: '证书账户', dataIndex: 'account_name', width: 120,
      render: (v: string, r) => (
        <span>
          <SafetyCertificateOutlined style={{ color: '#8c8c8c', marginRight: 6 }} />
          {v || r.provider_label || '-'}
        </span>
      ),
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string, r: CertificateResponse) => {
        // 未知状态回落原文，不能默认 revoked（会把签发中/失败误显示成已吊销）
        const text = STATUS_MAP[s]?.text ?? s ?? '-'
        const bg = certStatusBadgeBg(s)
        const badge = (
          <Tag
            style={{ backgroundColor: bg, borderColor: bg, color: '#fff', marginInlineEnd: 0, cursor: ['pending', 'issuing', 'renewing', 'failed', 'issued'].includes(s) ? 'pointer' : 'default' }}
          >
            {CERT_STATUS_BADGE_PULSE.includes(s) && (
              <span
                style={{
                  display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
                  background: '#fff', marginRight: 6, animation: 'cert-status-pulse 1.2s infinite',
                }}
              />
            )}
            {text}
          </Tag>
        )
        // 进行中/失败/已签发可点开签发日志；pending 是新建记录的初始状态，同样可点
        if (['pending', 'issuing', 'renewing', 'failed', 'issued'].includes(s)) {
          return (
            <Tooltip title="查看签发日志">
              <span style={{ cursor: 'pointer' }} onClick={() => setLogCert(r)}>{badge}</span>
            </Tooltip>
          )
        }
        return badge
      },
    },
    {
      title: '签发时间', dataIndex: 'not_before', width: 100,
      render: (v: string) => {
        if (!v || !dayjs(v).isValid()) return '-'
        return dayjs(v).format('YYYY-MM-DD')
      },
    },
    {
      title: '到期时间', dataIndex: 'not_after', width: 100,
      render: (v: string) => {
        const { text, tone } = expiryDisplay(daysLeft(v))
        return <span style={{ color: EXPIRY_TONE_COLOR[tone], fontWeight: 600 }}>{text}</span>
      },
    },
    {
      title: '自动续期', dataIndex: 'auto_renew', width: 80,
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
      title: '操作', key: 'action', width: 260, fixed: 'right',
      render: (_: unknown, r) => {
        const targetId = r.cert_id || r.id || ''
        const isTransient = ['pending', 'issuing', 'renewing'].includes(r.status)
        // 手动上传（custom）证书无 ACME 账号，续期必然失败——续签禁用
        const isCustom = r.provider === 'custom' || r.acme_server === 'custom'
        const moreItems: MenuProps['items'] = [
          { key: 'logs', icon: <FileTextOutlined />, label: '查看签发日志' },
          { key: 'deploy', icon: <DeploymentUnitOutlined />, label: '部署任务' },
          { key: 'deploy-history', icon: <HistoryOutlined />, label: '部署历史' },
          { key: 'revoke', icon: <StopOutlined />, label: '吊销证书', danger: true },
          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'logs') {
            setLogCert(r)
          } else if (key === 'deploy') {
            openDeployDrawer(r)
          } else if (key === 'deploy-history') {
            setHistoryCert(r)
          } else if (key === 'revoke') {
            confirm('certificates:revoke', (ticket) => revokeMut.mutate({ id: targetId, ticket }))
          } else if (key === 'delete') {
            modal.confirm({
              title: `删除证书 ${r.name || r.primary_domain}`,
              content: '此操作不可恢复，确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('certificates:delete', (ticket) => deleteMut.mutate({ id: targetId, riskTicket: ticket })),
            })
          }
        }
        return (
          <Space size={6}>
            <Tooltip title="下载证书文件包（cert/key/fullchain）">
              <Button size="small" color="green" variant="solid" disabled={isTransient} onClick={() => handleDownload(r)}>
                下载
              </Button>
            </Tooltip>
            <Tooltip title={isCustom ? '手动上传的证书没有 ACME 账号，无法自动续期' : '手动触发续期'}>
              <Button
                size="small" color="orange" variant="solid" disabled={isTransient || isCustom}
                onClick={() => renewMut.mutate(targetId)} loading={renewMut.isPending}
              >
                续签
              </Button>
            </Tooltip>
            <Button size="small" color="blue" variant="solid" onClick={() => openEditDrawer(r)}>
              修改
            </Button>
            <Dropdown menu={{ items: moreItems, onClick: onMore }} trigger={['click']}>
              <Button size="small" color="blue" variant="solid">
                更多 <DownOutlined />
              </Button>
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  return (
    <Flex gap={16} style={{ height: '100%' }}>
      {/* 仅本页用：斑马纹行底色 + 进行中状态徽章呼吸点 */}
      <style>{`.cert-row-alt{background:#fafafa;}\n@keyframes cert-status-pulse{0%,100%{opacity:1}50%{opacity:.25}}`}</style>
      <GroupSidebar
        resourceType="cert"
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
              value={searchInput}
              onChange={(e) => { setSearchInput(e.target.value); if (!e.target.value) setSearch('') }}
              onPressEnter={() => setSearch(searchInput)}
              allowClear
              style={{ width: 240 }}
            />
            <Select
              value={providerFilter}
              onChange={(v) => setProviderFilter(v)}
              options={CERT_PROVIDER_FILTER_OPTIONS}
              style={{ width: 120 }}
            />
            <Select
              value={statusFilter}
              onChange={(v) => setStatusFilter(v)}
              options={CERT_STATUS_FILTER_OPTIONS}
              style={{ width: 130 }}
            />
            <Button type="primary" onClick={() => setSearch(searchInput)}>搜索</Button>
            <Button onClick={() => queryClient.invalidateQueries({ queryKey: ['certificates'] })}>刷新</Button>
          </Space>
          <Space>
            <Dropdown
              menu={{
                items: [{ key: 'batchDelete', icon: <DeleteOutlined />, label: `批量删除 (${selectedKeys.length})`, danger: true, disabled: selectedKeys.length === 0 }],
                onClick: ({ key }) => {
                  if (key === 'batchDelete') confirm('certificates:delete', (ticket) => batchDeleteMut.mutate(ticket))
                },
              }}
              trigger={['click']}
            >
              <Button disabled={selectedKeys.length === 0}>
                批量操作 <DownOutlined />
              </Button>
            </Dropdown>
            <Button type="primary" icon={<PlusOutlined />} onClick={openDrawer}>申请 / 上传证书</Button>
          </Space>
        </Flex>

        <Table<CertificateResponse>
          rowKey={(r) => r.cert_id || r.id || r.primary_domain}
          rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys as string[]) }}
          columns={columns}
          dataSource={filtered}
          loading={isLoading}
          size="middle"
          scroll={{ x: 1300 }}
          rowClassName={(_, index) => (index % 2 === 1 ? 'cert-row-alt' : '')}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (total, range) => `显示 ${range[0]} 到第 ${range[1]} 条，总共 ${total} 条` }}
        />
      </Flex>

      {/* ── 申请 / 上传 / 编辑 Drawer ── */}
      <Drawer
        title={mode === 'edit' ? `编辑证书 · ${editingCert?.primary_domain || ''}` : '申请 / 上传证书'}
        open={drawerOpen}
        onClose={() => { setDrawerOpen(false); setEditingCert(null) }}
        width={500}
        extra={
          mode === 'edit' ? (
            <Button type="primary" onClick={handleEditSubmit} loading={saveMut.isPending}>
              保存修改
            </Button>
          ) : (
            <Button type="primary" onClick={handleSubmit} loading={issueMut.isPending || uploadMut.isPending}>
              {mode === 'issue' ? '申请签发' : '确认上传'}
            </Button>
          )
        }
      >
        {mode === 'edit' ? (
          <Form form={editForm} layout="vertical">
            <Form.Item name="name" label="证书备注名称" rules={[{ required: true }]}>
              <Input placeholder="例如 生产环境 *.opspilot.com 证书" />
            </Form.Item>
            <Form.Item name="san_domains" label="SAN 备用域名（逗号或换行分隔）">
              <Input.TextArea rows={3} placeholder="www.example.com, api.example.com" />
            </Form.Item>
            <Form.Item name="auto_renew" label="自动续期" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="renew_before_days" label="提前续期天数">
              <InputNumber min={1} max={90} style={{ width: '100%' }} placeholder="例如 30" />
            </Form.Item>
            <Form.Item name="remark" label="备注">
              <Input.TextArea rows={2} />
            </Form.Item>
          </Form>
        ) : (
          <>
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
            <Form.Item name="primary_domain" label="主域名" rules={[{ required: true }]}>
              <Input placeholder="例如 *.example.com" />
            </Form.Item>
            <Form.Item name="san_domains" label="SAN 备用域名（逗号或换行分隔）">
              <Input.TextArea rows={3} placeholder="www.example.com, api.example.com" />
            </Form.Item>
            <Form.Item
              name="account_id"
              label="DNS 校验账号凭证"
              rules={[{ required: true, message: '请选择用于 DNS 校验的账号凭证' }]}
              extra={
                <>
                  用于 DNS-01 验证；ACME 账号由系统自动管理。
                  {showDomainUnknownHint && (
                    <>
                      <br />
                      未在域名资产中找到该域名，显示全部可用凭证。
                    </>
                  )}
                </>
              }
            >
              <Select
                placeholder="选择用于 DNS 校验的云 API 凭证"
                notFoundContent="暂无可用 DNS 校验账号"
                options={filteredCertAccounts.accounts.map((a) => ({ value: a.account_id, label: `${a.account_name}（${a.provider_label}）` }))}
              />
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
          </>
        )}
      </Drawer>

      {/* ── 签发日志抽屉 ── */}
      <Drawer
        title={`签发日志 · ${logCertLive?.primary_domain || ''}`}
        open={!!logCert}
        onClose={() => setLogCert(null)}
        width={640}
      >
        {logCertLive && (
          <Flex align="center" gap={8} style={{ marginBottom: 12 }}>
            <Badge
              status={(STATUS_MAP[logCertStatus]?.color ?? 'default') as 'success' | 'processing' | 'warning' | 'error' | 'default'}
              text={STATUS_MAP[logCertStatus]?.text ?? logCertStatus}
            />
            {logCertStatus === 'failed' && logCertLive.last_error && (
              <span style={{ color: '#ff4d4f', fontSize: 12 }}>{logCertLive.last_error}</span>
            )}
          </Flex>
        )}
        {isLogLoading ? (
          <Spin />
        ) : logLines.length === 0 ? (
          <div style={{ color: '#8c8c8c' }}>暂无日志</div>
        ) : (
          <div
            ref={logContainerRef}
            style={{ maxHeight: 'calc(100vh - 240px)', overflow: 'auto', fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}
          >
            {logLines.map((line, i) => (
              <div key={i} style={isCertLogErrorLine(line) ? { color: '#ff4d4f' } : undefined}>{line}</div>
            ))}
          </div>
        )}
      </Drawer>

      {/* ── 部署历史抽屉 ── */}
      <Drawer
        title={`部署历史 · ${historyCert?.primary_domain || ''}`}
        open={!!historyCert}
        onClose={() => setHistoryCert(null)}
        width={720}
      >
        {deployments.length === 0 ? (
          <div style={{ color: '#8c8c8c' }}>暂无部署记录</div>
        ) : (
          <Table<CertificateDeployment>
            rowKey={(r) => r.deploy_id}
            dataSource={deployments}
            size="middle"
            pagination={{ defaultPageSize: 10, showTotal: (total, range) => `显示 ${range[0]} 到第 ${range[1]} 条，总共 ${total} 条` }}
            expandable={{
              expandedRowRender: (r) => (
                <div>
                  <div style={{ color: '#8c8c8c', fontSize: 12, marginBottom: 8 }}>
                    触发人：{r.triggered_by || '-'} ｜ 开始：{r.started_at || '-'} ｜ 结束：{r.finished_at || '-'}
                  </div>
                  {r.results.length === 0 ? (
                    <div style={{ color: '#8c8c8c' }}>暂无逐目标结果</div>
                  ) : (
                    r.results.map((item, i) => (
                      <Flex key={i} align="center" gap={8} style={{ padding: '4px 0' }}>
                        <Tag color={item.success ? 'success' : 'error'}>{item.success ? '成功' : '失败'}</Tag>
                        <span style={{ minWidth: 120 }}>{item.target || item.asset_id || '-'}</span>
                        <span style={{ color: '#595959', fontSize: 12 }}>{item.message}</span>
                      </Flex>
                    ))
                  )}
                </div>
              ),
            }}
            columns={[
              {
                title: '时间', dataIndex: 'started_at', width: 150,
                render: (v: string) => (v && dayjs(v).isValid() ? dayjs(v).format('YYYY-MM-DD HH:mm') : v || '-'),
              },
              {
                title: '目标', width: 240,
                render: (_: unknown, r) => (
                  <div>
                    <div>{deployTargetTypeText(r.target_type)}：{r.targets || '-'}</div>
                  </div>
                ),
              },
              {
                title: '状态', dataIndex: 'status', width: 90,
                render: (s: string) => <Tag color={deployStatusTagColor(s)}>{deployStatusText(s)}</Tag>,
              },
              {
                title: '成功', width: 80,
                render: (_: unknown, r) => {
                  const { success, total } = deploySuccessCount(r.results)
                  return total > 0 ? `${success}/${total}` : '-'
                },
              },
            ]}
          />
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
