import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Input, Tag, Drawer, Form, Select, InputNumber, App, Tooltip,
  Badge, Flex, Alert, Dropdown, Popover, Popconfirm, Empty, Segmented, Modal, Descriptions, Radio,
  Tabs, Card, Statistic, Row, Col,
  Typography,
} from 'antd'

const { Text } = Typography
import type { ColumnsType } from 'antd/es/table'
import type { TableRowSelection } from 'antd/es/table/interface'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, SearchOutlined, DeleteOutlined, EditOutlined,
  ApiOutlined, SyncOutlined, CodeOutlined, LoadingOutlined, MoreOutlined,
  LineChartOutlined, FileTextOutlined, FolderOutlined, DesktopOutlined, DownloadOutlined,
  CloudSyncOutlined, CloudOutlined, InfoCircleOutlined, KeyOutlined,
  CheckCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined,
  PayCircleOutlined, LinkOutlined,
} from '@ant-design/icons'
import { assetsApi, groupsApi, credentialsApi, monitoringApi } from '@/api'
import dayjs from 'dayjs'
import { useNotificationStore } from '@/store/notifications'
import { useRiskConfirm, usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import GroupSidebar from '@/components/GroupSidebar'
import TerminalModal from '@/components/TerminalModal'
import InstanceCostDrawer from '@/components/InstanceCostDrawer'
import RelationCard from '@/components/RelationCard'
import WgMembershipDrawer from '@/components/WgMembershipDrawer'
import HostMetricsDrawer from '@/components/HostMetricsDrawer'
import HostLogsDrawer from '@/components/HostLogsDrawer'
import type { AssetResponse, AssetCreate, CredentialCreate, MonitoringTargetItem, LogTargetItem, CloudInstanceItem } from '@/types/api'

// 云厂商 / 来源展示映射
const PROVIDER_META: Record<string, { label: string; sourceTag: string; tagColor: string }> = {
  aliyun:       { label: '阿里云',   sourceTag: 'ECS', tagColor: 'blue' },
  tencent:      { label: '腾讯云',   sourceTag: 'CVM', tagColor: 'blue' },
  aws:          { label: 'AWS',      sourceTag: 'EC2', tagColor: 'blue' },
  proxmox:      { label: 'Proxmox', sourceTag: 'PVE', tagColor: 'geekblue' },
  vmware:       { label: 'VMware',  sourceTag: 'VM',  tagColor: 'geekblue' },
  'on-premise': { label: '物理机',   sourceTag: '自建主机', tagColor: 'geekblue' },
  localhost:    { label: '本机',     sourceTag: '内置采集', tagColor: 'green' },
}
function providerMeta(provider: string) {
  return PROVIDER_META[provider] ?? { label: provider || '未知', sourceTag: '主机', tagColor: 'default' }
}
// 状态灯：off=未接入(灰) ok=正常(绿) warn=心跳异常(黄)
// 本机也读后端 target（source=builtin），不再写死绿灯——监控连接未配时显示未接入。
type LightState = 'off' | 'ok' | 'warn'
function lightState(target: { enabled: boolean; status: string } | undefined): LightState {
  if (!target || !target.enabled) return 'off'
  const up = target.status === 'up' || target.status === 'running' || target.status === 'ok'
  return up ? 'ok' : 'warn'
}
const LIGHT_COLOR: Record<LightState, string> = { off: '#94a3b8', ok: '#10b981', warn: '#f59e0b' }
const LIGHT_BG: Record<LightState, string> = { off: '#f1f5f9', ok: '#ecfdf5', warn: '#fffbeb' }


export default function HostsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { confirm } = useRiskConfirm()
  const { has } = usePermission()
  const { message, modal } = App.useApp()
  const pushLocal = useNotificationStore((s) => s.pushLocal)

  const [search, setSearch]                     = useState('')
  const [groupFilter, setGroupFilter]           = useState<number | null>(null)
  const [statusFilter, setStatusFilter]         = useState<'all' | 'online' | 'offline'>('all')
  const [sourceFilter, setSourceFilter]         = useState<string | null>(null)
  const [configFilter, setConfigFilter]         = useState<string | null>(null)
  const [moveOpen, setMoveOpen]                 = useState(false)
  const [moveTarget, setMoveTarget]             = useState<number | null>(null)
  const [drawerOpen, setDrawerOpen]             = useState(false)
  const [editingId, setEditingId]               = useState<string | null>(null)
  const [selectedKeys, setSelectedKeys]         = useState<string[]>([])
  const [form]                                  = Form.useForm<AssetCreate>()
  const [credentialRef, setCredentialRef]       = useState<string>('')
  const [pingResult, setPingResult]             = useState<{ ok: boolean; message?: string } | null>(null)
  const [pingLoading, setPingLoading]           = useState(false)
  const [termModalOpen, setTermModalOpen]       = useState(false)
  const [termAsset, setTermAsset]               = useState<AssetResponse | null>(null)
  const [collectingIds, setCollectingIds]       = useState<string[]>([])
  const collectStartRef                         = useRef<Record<string, number>>({})
  const [metricAsset, setMetricAsset]           = useState<AssetResponse | null>(null)
  const [logAsset, setLogAsset]                 = useState<AssetResponse | null>(null)
  const [costAsset, setCostAsset]             = useState<AssetResponse | null>(null)
  const [wgAsset, setWgAsset]                 = useState<AssetResponse | null>(null)
  const [metricOpen, setMetricOpen]             = useState(false)
  const [logOpen, setLogOpen]                   = useState(false)

  // ── 云同步状态 ──
  const [cloudSyncOpen, setCloudSyncOpen]       = useState(false)
  const [hostKeysOpen, setHostKeysOpen]         = useState(false)
  const [syncStep, setSyncStep]                 = useState<'form' | 'preview'>('form')
  const [syncProvider, setSyncProvider]         = useState<'aliyun' | 'tencent' | 'aws'>('aliyun')
  const [syncUseSaved, setSyncUseSaved]         = useState(true)
  const [syncCredId, setSyncCredId]             = useState<string>('')
  const [syncAK, setSyncAK]                     = useState('')
  const [syncSK, setSyncSK]                     = useState('')
  const [syncRegions, setSyncRegions]           = useState<string[]>([])
  const [syncScanning, setSyncScanning]         = useState(false)
  const [syncProgress, setSyncProgress]         = useState<Record<string, { status: 'pending' | 'scanning' | 'success' | 'failed'; count?: number }>>({})
  const [syncInstances, setSyncInstances]       = useState<CloudInstanceItem[]>([])
  const [syncSelectedIds, setSyncSelectedIds]   = useState<string[]>([])
  const [syncImporting, setSyncImporting]       = useState(false)

  // ── 新增凭证快捷弹窗 ──
  const [credModalOpen, setCredModalOpen]       = useState(false)
  const [credForm]                              = Form.useForm()
  const [credType, setCredType]                 = useState<'password' | 'ssh_key'>('password')
  // ── 主机详情抽屉 ──
  const [detailOpen, setDetailOpen]             = useState(false)
  const [detailAsset, setDetailAsset]           = useState<AssetResponse | null>(null)

  // ── Queries ──
  const { data: hostKeys = [], refetch: refetchHostKeys } = useQuery({
    queryKey: ['asset-host-keys'],
    queryFn: () => assetsApi.listHostKeys(),
    enabled: hostKeysOpen,
  })

  const resetHostKeyMut = useMutation({
    mutationFn: (assetId: string) => assetsApi.resetHostKey(assetId),
    onSuccess: () => { message.success('指纹已清除，下次连接将重新钉扎'); refetchHostKeys() },
    onError: () => message.error('清除指纹失败'),
  })

  const { data: assets = [], isLoading } = useQuery({
    queryKey: ['assets'],
    queryFn: assetsApi.list,
    // 本地触发的采集，或后端自动采集（首次添加）导致任一主机处于 collecting，都持续轮询
    refetchInterval: (q) => {
      const list = (q.state.data as AssetResponse[] | undefined) ?? []
      const anyCollecting = collectingIds.length > 0 || list.some((a) => a.config_task_status === 'collecting')
      return anyCollecting ? 2000 : false
    },
  })
  const { data: groups = [] }            = useQuery({ queryKey: ['groups'],  queryFn: groupsApi.list })
  const { data: credentials = [] }       = useQuery({ queryKey: ['credentials'], queryFn: credentialsApi.list })
  const { data: monTargets }             = useQuery({ queryKey: ['monitoring-targets'], queryFn: monitoringApi.targets, refetchInterval: 60000 })
  const { data: logTargets }             = useQuery({ queryKey: ['log-targets'], queryFn: monitoringApi.logTargets, refetchInterval: 60000 })

  // asset_id -> 监控 / 日志 target 状态映射
  const monMap = useMemo(() => {
    const m = new Map<string, MonitoringTargetItem>()
    monTargets?.targets?.forEach((t) => m.set(t.asset_id, t))
    return m
  }, [monTargets])
  const logMap = useMemo(() => {
    const m = new Map<string, LogTargetItem>()
    logTargets?.targets?.forEach((t) => m.set(t.asset_id, t))
    return m
  }, [logTargets])

  // ── 采集完成检测：轮询期间发现主机采集结束，移出集合并提示结果 ──
  useEffect(() => {
    if (collectingIds.length === 0) return
    // 宽限期：后端可能 1-2 秒就跑完，若刚触发时前端拿到的还是上次的 success，
    // 会被瞬间误判为「已完成」导致「采集中」一闪而过。触发后 2 秒内不判定完成。
    const GRACE_MS = 2000
    const now = Date.now()
    const finished = assets.filter((a) => {
      if (!collectingIds.includes(a.id) || a.config_task_status === 'collecting') return false
      const startedAt = collectStartRef.current[a.id] ?? 0
      return now - startedAt >= GRACE_MS
    })
    if (finished.length === 0) return
    finished.forEach((a) => {
      if (a.config_task_status === 'success') {
        message.success(`${a.name} 配置采集完成`)
      } else if (a.config_task_status === 'failed') {
        message.error(`${a.name} 配置采集失败：${a.config_task_error || '未知原因'}`)
      }
    })
    const finishedIds = finished.map((a) => a.id)
    finishedIds.forEach((id) => { delete collectStartRef.current[id] })
    setCollectingIds((prev) => prev.filter((id) => !finishedIds.includes(id)))
  }, [assets, collectingIds, message])

  // ── Mutations ──
  const { deleteMut } = useCrudResource<AssetCreate, string>({
    queryKey: ['assets'],
    entityName: '主机',
    createFn: assetsApi.create,
    updateFn: assetsApi.update,
    deleteFn: assetsApi.delete,
  })

  const saveMut = useMutation({
    mutationFn: ({ id, data }: { id?: string; data: AssetCreate }): Promise<AssetResponse | { message: string; config_collecting: boolean; asset: AssetResponse }> =>
      id ? assetsApi.update(id, data) : assetsApi.create(data),
    onSuccess: (result, variables) => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setDrawerOpen(false)
      if (variables.id) {
        const updated = (result as { asset?: AssetResponse })?.asset
        if (updated && updated.type !== 'healthy') {
          message.warning(`主机已更新，但 SSH 连接失败${updated.status?.[0] ? `：${updated.status[0]}` : ''}`)
        } else {
          message.success('主机更新成功')
        }
        return
      }
      const created = result as AssetResponse
      if (created.type === 'healthy') {
        message.success('主机已接入，正在采集配置')
      } else if (created.type === 'pending') {
        message.warning('主机已保存，待绑定 SSH 凭证后才能采集配置')
      } else {
        const probeMessage = created.status?.[0]
        message.warning(`主机已保存，但 SSH 连接失败${probeMessage ? `：${probeMessage}` : ''}`)
      }
    },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '主机保存失败，请重试'),
  })

  const credSaveMut = useMutation({
    mutationFn: (data: CredentialCreate) => credentialsApi.create(data),
    onSuccess: (newCred) => {
      queryClient.invalidateQueries({ queryKey: ['credentials'] })
      message.success('凭证已保存')
      setCredModalOpen(false)
      // 自动填充到主机表单
      setCredentialRef(newCred.cred_id)
      form.setFieldValue('credential_ref', newCred.cred_id)
    },
    onError: () => message.error('凭证保存失败'),
  })

  // ── 快速新增凭证 ──
  // 后端凭证模型只有单个加密 content 字段：密码存明文、SSH 密钥存 PEM，
  // 且 expires_at 必填（AK/SK 与密钥类按长期有效处理，默认给一年）。
  const handleCredSave = () => {
    credForm.validateFields().then((values) => {
      credSaveMut.mutate({
        name: values.name,
        type: credType === 'ssh_key' ? 'SSH Key' : 'Password',
        content: credType === 'ssh_key' ? values.private_key : values.password,
        expires_at: dayjs().add(1, 'year').format('YYYY-MM-DD'),
      })
    })
  }

  const batchDeleteMut = useMutation({
    mutationFn: () => assetsApi.batchDelete(selectedKeys),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      const count = selectedKeys.length
      setSelectedKeys([])
      message.success(`已删除 ${count} 台主机`)
      pushLocal({ type: 'success', category: 'host', title: '批量删除完成', content: `已成功删除 ${count} 台主机` })
    },
    onError: () => {
      message.error('批量删除失败')
      pushLocal({ type: 'error', category: 'host', title: '批量删除失败', content: '主机批量删除操作未完成，请重试' })
    },
  })

  const batchMoveMut = useMutation({
    mutationFn: () => groupsApi.assign(selectedKeys, moveTarget ?? null, 'host'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      const count = selectedKeys.length
      const label = moveTarget === null ? '未分组' : groups.find((g) => g.id === moveTarget)?.name || '目标分组'
      message.success(`已将 ${count} 台主机移动到「${label}」`)
      setSelectedKeys([])
      setMoveOpen(false)
      pushLocal({ type: 'success', category: 'host', title: '批量移动完成', content: `已将 ${count} 台主机移动到「${label}」` })
    },
    onError: (error) => message.error(extractAxiosDetail(error) ?? '批量移动失败'),
  })

  const batchRefreshMut = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(selectedKeys.map((id) => assetsApi.refreshConfig(id)))
      const failed = results.filter((r) => r.status === 'rejected').length
      return { total: selectedKeys.length, failed }
    },
    onSuccess: ({ total, failed }) => {
      const now = Date.now()
      selectedKeys.forEach((id) => { collectStartRef.current[id] = now })
      setCollectingIds((prev) => Array.from(new Set([...prev, ...selectedKeys])))
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      setSelectedKeys([])
      if (failed > 0) {
        message.warning(`已触发 ${total - failed}/${total} 台主机的配置采集，${failed} 台触发失败`)
        pushLocal({ type: 'warning', category: 'host', title: '批量配置采集部分失败', content: `${failed} 台主机触发采集失败，请单独重试`, link: '/assets/hosts' })
      } else {
        message.success(`已触发 ${total} 台主机的配置采集`)
        pushLocal({ type: 'info', category: 'host', title: '批量配置采集已触发', content: `已触发 ${total} 台主机的配置采集，结果将在采集完成后通知`, link: '/assets/hosts' })
      }
    },
    onError: () => message.error('批量刷新失败'),
  })

  // ── 统计指标 ──
  const totalCount   = assets.length
  const onlineCount  = assets.filter((a) => a.type === 'healthy').length
  const pendingCount = assets.filter((a) => a.type === 'pending').length
  const offlineCount = assets.filter((a) => a.type !== 'healthy' && a.type !== 'pending').length

  // ── 筛选 ──
  const filtered = assets.filter((a) => {
    const matchSearch = !search || a.name.includes(search) || a.ip.includes(search)
    const matchGroup  = groupFilter === null || a.group_id === groupFilter
    const matchStatus = statusFilter === 'all'
      || (statusFilter === 'online' && a.type === 'healthy')
      || (statusFilter === 'offline' && a.type !== 'healthy')
    const matchSource = !sourceFilter || a.provider === sourceFilter
    const matchConfig = !configFilter
      || (configFilter === 'collecting' && (collectingIds.includes(a.id) || a.config_task_status === 'collecting'))
      || (configFilter === 'failed' && a.config_task_status === 'failed')
    return matchSearch && matchGroup && matchStatus && matchSource && matchConfig
  })

  // 来源筛选候选（去重）
  const sourceOptions = useMemo(() => {
    const seen = new Set<string>()
    return assets.reduce<{ value: string; label: string }[]>((acc, a) => {
      if (!seen.has(a.provider)) {
        seen.add(a.provider)
        acc.push({ value: a.provider, label: providerMeta(a.provider).label })
      }
      return acc
    }, [])
  }, [assets])

  // ── 导出 CSV ──
  const handleExport = () => {
    const headers = ['名称', 'IP地址', '来源', '分组', 'CPU(核)', '内存(GB)', '系统盘(GB)', '状态', '操作系统']
    const rows = filtered.map((a) => [
      a.name,
      a.ip,
      providerMeta(a.provider).label,
      groups.find((g) => g.id === a.group_id)?.name || '未分组',
      a.cpu_cores || '',
      a.memory_gb || '',
      a.system_disk_gb || '',
      a.type === 'healthy' ? '在线' : '离线',
      a.os || '',
    ])
    const csv = [headers, ...rows].map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\r\n')
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `主机清单-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
    message.success(`已导出 ${filtered.length} 台主机`)
  }

  // 抽屉内展示的主机（随轮询自动更新，用于磁盘明细/采集状态）
  const editingAsset = editingId ? assets.find((a) => a.id === editingId) ?? null : null
  const editingCollecting = !!editingId && (collectingIds.includes(editingId) || editingAsset?.config_task_status === 'collecting')

  // ── 打开抽屉 ──
  const openDrawer = useCallback((asset?: AssetResponse) => {
    if (asset) {
      setEditingId(asset.id)
      setCredentialRef(asset.credential_ref || '')
      form.setFieldsValue({
        name: asset.name, ip: asset.ip, provider: asset.provider,
        region: asset.region, os: asset.os, ssh_port: asset.ssh_port,
        ssh_user: asset.ssh_user, ssh_password: '', credential_ref: asset.credential_ref,
        group_id: asset.group_id, cpu_cores: asset.cpu_cores,
        memory_gb: asset.memory_gb, system_disk_gb: asset.system_disk_gb,
        remark: asset.remark,
      })
    } else {
      setEditingId(null)
      form.resetFields()
      setCredentialRef('')
    }
    setDrawerOpen(true)
    setPingResult(null)
  }, [form])

  // ── 打开详情抽屉 ──
  const openDetailDrawer = useCallback((asset: AssetResponse) => {
    setDetailAsset(asset)
    setDetailOpen(true)
  }, [])

  // ── 云同步：打开对话框 ──
  const openCloudSync = () => {
    setSyncStep('form')
    setSyncProvider('aliyun')
    setSyncUseSaved(true)
    setSyncCredId('')
    setSyncAK('')
    setSyncSK('')
    setSyncRegions([])
    setSyncInstances([])
    setSyncSelectedIds([])
    setSyncProgress({})
    setCloudSyncOpen(true)
  }

  // ── 云同步：一键获取主机（内部自动拉区域 + 全区并发扫描 + 进预览） ──
  const handleSyncScan = async () => {
    if (syncUseSaved && !syncCredId) {
      message.warning('请选择凭证')
      return
    }
    if (!syncUseSaved && (!syncAK.trim() || !syncSK.trim())) {
      message.warning('请输入 Access Key 和 Secret Key')
      return
    }

    const credArgs = {
      credential_id: syncUseSaved ? syncCredId : undefined,
      access_key: syncUseSaved ? undefined : syncAK,
      secret_key: syncUseSaved ? undefined : syncSK,
    }

    setSyncScanning(true)
    setSyncInstances([])
    setSyncProgress({})

    // 1) 后台拉取全部区域（用户无需感知）
    let regions: string[]
    try {
      const res = await assetsApi.getCloudRegions({ provider: syncProvider, ...credArgs })
      if (!res.success || res.regions.length === 0) {
        message.error(res.message || '未获取到可用区域，请检查凭证是否有效')
        setSyncScanning(false)
        return
      }
      regions = res.regions
      setSyncRegions(regions)
    } catch (err: unknown) {
      message.error(extractAxiosDetail(err) ?? '凭证核验失败，无法获取区域')
      setSyncScanning(false)
      return
    }

    // 2) 受限并发扫描（每批 3 个区域，失败重试一次）
    const progress: Record<string, { status: 'pending' | 'scanning' | 'success' | 'failed'; count?: number }> = {}
    regions.forEach((r) => { progress[r] = { status: 'pending' } })
    setSyncProgress(progress)

    const allInstances: CloudInstanceItem[] = []
    const failedRegions: string[] = []
    const scanRegion = async (region: string, isRetry = false) => {
      setSyncProgress((prev) => ({ ...prev, [region]: { status: 'scanning' } }))
      try {
        const res = await assetsApi.sync({ provider: syncProvider, region, ...credArgs, instance_ids: null })
        if (res.success && res.preview && res.instances) {
          allInstances.push(...res.instances)
          setSyncProgress((prev) => ({ ...prev, [region]: { status: 'success', count: res.instances.length } }))
        } else {
          if (!isRetry) failedRegions.push(region)
          setSyncProgress((prev) => ({ ...prev, [region]: { status: 'failed' } }))
        }
      } catch {
        if (!isRetry) failedRegions.push(region)
        setSyncProgress((prev) => ({ ...prev, [region]: { status: 'failed' } }))
      }
    }

    const CONCURRENCY = 3
    for (let i = 0; i < regions.length; i += CONCURRENCY) {
      const batch = regions.slice(i, i + CONCURRENCY)
      await Promise.all(batch.map((r) => scanRegion(r)))
    }

    // 3) 失败区域自动重试一次
    if (failedRegions.length > 0) {
      const retryBatch = failedRegions.splice(0)
      for (const r of retryBatch) {
        await scanRegion(r, true)
      }
    }

    // 4) 去重后进预览
    const uniq = Array.from(new Map(allInstances.map((i) => [i.instance_id, i])).values())
    setSyncInstances(uniq)
    setSyncSelectedIds(uniq.filter((i) => !i.is_imported).map((i) => i.instance_id))
    setSyncScanning(false)
    setSyncStep('preview')
    const finalFailedCount = Object.values(progress).filter(p => p.status === 'failed').length
    if (finalFailedCount > 0) {
      message.warning(`扫描完成，共发现 ${uniq.length} 台实例（${finalFailedCount} 个区域失败）`)
    } else {
      message.success(`扫描完成，共发现 ${uniq.length} 台实例`)
    }
  }

  // ── 云同步：确认导入 ──
  const handleSyncImport = async () => {
    if (syncSelectedIds.length === 0) {
      message.warning('请至少选择一台实例')
      return
    }
    setSyncImporting(true)
    try {
      const res = await assetsApi.sync({
        provider: syncProvider,
        region: 'auto',
        credential_id: syncUseSaved ? syncCredId : undefined,
        access_key: syncUseSaved ? undefined : syncAK,
        secret_key: syncUseSaved ? undefined : syncSK,
        instance_ids: syncSelectedIds,
      })
      if (res.success) {
        const count = syncSelectedIds.length
        message.success(res.message || `成功导入 ${count} 台主机`)
        queryClient.invalidateQueries({ queryKey: ['assets'] })
        setCloudSyncOpen(false)
        // 导入成功引导：提示用户绑定 SSH 凭证
        modal.info({
          title: '主机导入成功',
          content: (
            <div>
              <p>已成功导入 {count} 台主机。</p>
              <p style={{ marginTop: 8, color: '#faad14' }}>
                <strong>下一步：</strong>这些主机需要绑定 SSH 凭证才能进行配置采集和远程操作。
                您可以在列表中找到这些主机（显示"待配置"标签），点击编辑为其绑定凭证。
              </p>
            </div>
          ),
          okText: '知道了',
        })
      } else {
        message.error(res.message || '导入失败')
      }
    } catch (err: unknown) {
      message.error(extractAxiosDetail(err) ?? '导入失败')
    } finally {
      setSyncImporting(false)
    }
  }

  // 可用的 AccessKey 凭证（后端 list 返回展示名 "AccessKey"，创建时用 "access_key"，做归一化兼容）
  const akCredentials = useMemo(() => {
    return credentials.filter((c) => {
      const t = String(c.type).toLowerCase().replace(/[\s_-]/g, '')
      return t === 'accesskey' || t === 'aksk'
    })
  }, [credentials])

  // ── SSH 测试 ──
  const handlePing = async () => {
    const ip = form.getFieldValue('ip')
    const port = form.getFieldValue('ssh_port') ?? 22
    const user = form.getFieldValue('ssh_user') || 'root'
    const password = form.getFieldValue('ssh_password') || ''
    const credentialId = credentialRef || form.getFieldValue('credential_ref') || ''
    setPingLoading(true); setPingResult(null)
    try {
      const res = await assetsApi.ping({
        // 编辑已有主机时传 asset_id：后端会用本次探测结果同步更新该主机的在线状态
        asset_id: editingId ?? '',
        ip,
        port,
        user,
        password,
        credential_id: credentialId,
      })
      if (res?.success) {
        setPingResult({ ok: true }); message.success(res.message || 'SSH 连通正常')
      } else {
        setPingResult({ ok: false, message: res?.message }); message.error(res?.message || 'SSH 连接失败')
      }
    } catch (error) {
      setPingResult({ ok: false }); message.error(extractAxiosDetail(error) ?? 'SSH 连接失败')
    } finally { setPingLoading(false) }
  }

  // ── 刷新配置 ──
  const handleRefreshConfig = async (id: string) => {
    try {
      const res = await assetsApi.refreshConfig(id)
      if (res?.success === false) {
        message.warning(res.message || '该主机已有采集任务在执行')
        return
      }
      message.loading({ content: '正在采集硬件配置，约 2-8 秒…', key: `collect-${id}`, duration: 8 })
      collectStartRef.current[id] = Date.now()
      setCollectingIds((prev) => (prev.includes(id) ? prev : [...prev, id]))
      queryClient.invalidateQueries({ queryKey: ['assets'] })
    } catch { message.error('触发失败') }
  }

  // ── 批量删除确认 ──
  const confirmBatchDelete = () => {
    modal.confirm({
      title: `删除 ${selectedKeys.length} 台主机`,
      content: '此操作不可恢复，确定继续？',
      okText: '删除', okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => confirm('hosts:batch_delete', () => batchDeleteMut.mutate()),
    })
  }

  const credOptions = credentials
    .filter((c) => {
      const t = String(c.type).toLowerCase().replace(/[\s_-]/g, '')
      return t !== 'accesskey' && t !== 'aksk' && t !== 'token'
    })
    .map((c) => ({ value: c.cred_id, label: `${c.name} (${c.type})` }))
  const groupOptions = groups.map((g) => ({ value: g.id, label: g.name }))

  // ── 状态灯渲染 ──
  const renderLight = (
    state: LightState,
    icon: React.ReactNode,
    _text: string,
    tip: string,
    onClick?: () => void,
  ) => (
    <Tooltip title={tip}>
      <span
        onClick={onClick}
        style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 24, height: 24, borderRadius: '50%', fontSize: 13,
          background: LIGHT_BG[state], color: LIGHT_COLOR[state],
          cursor: !onClick ? 'default' : 'pointer',
          transition: 'all .15s',
        }}
      >
        {icon}
      </span>
    </Tooltip>
  )

  // ── 表格列 ──
  const columns: ColumnsType<AssetResponse> = [
    {
      title: '名称', dataIndex: 'name', width: 180, fixed: 'left',
      render: (v: string, r) => <a onClick={() => openDetailDrawer(r)} style={{ fontWeight: 500 }}>{v}</a>,
    },
    { title: 'IP 地址', dataIndex: 'ip', width: 140 },
    {
      title: '来源', key: 'source', width: 120,
      render: (_: unknown, r) => {
        const meta = providerMeta(r.provider)
        return <Tag color={meta.tagColor} style={{ margin: 0 }}>{meta.label}</Tag>
      },
    },
    {
      title: '分组', dataIndex: 'group_id', width: 110,
      render: (gid: number | null) => {
        const g = groups.find((x) => x.id === gid)
        return g
          ? <Space size={5}><span style={{ width: 7, height: 7, borderRadius: '50%', background: g.color, display: 'inline-block' }} />{g.name}</Space>
          : <span style={{ color: '#94a3b8' }}>未分组</span>
      },
    },
    {
      title: '配置', key: 'resource', width: 190,
      render: (_: unknown, r) => {
        if (collectingIds.includes(r.id)) {
          return <Tag icon={<LoadingOutlined />} color="processing">采集中</Tag>
        }
        const cpu = r.cpu_cores ? `${r.cpu_cores}C` : '-'
        const mem = r.memory_gb ? `${r.memory_gb}G` : '-'
        const disk = r.system_disk_gb ? `${r.system_disk_gb}G` : '-'
        const dataDisks = (r.disks ?? []).slice(1)
        const summary = `${cpu} / ${mem} / ${disk}`
        const detail = dataDisks.length > 0
          ? `系统盘 ${disk}；数据盘 ${dataDisks.map((d) => `${d.label || d.key}:${d.size_gb}G`).join('、')}`
          : summary
        return (
          <Tooltip title={detail}>
            <span style={{ color: '#475569', fontSize: 13 }}>
              {summary}
              {dataDisks.length > 0 && (
                <span style={{ color: '#94a3b8', marginLeft: 6 }}>+{dataDisks.length}</span>
              )}
            </span>
          </Tooltip>
        )
      },
    },
    {
      title: '状态', dataIndex: 'type', width: 100,
      sorter: (a, b) => (a.type === 'healthy' ? 1 : 0) - (b.type === 'healthy' ? 1 : 0),
      render: (t: string, r) => {
        if (t === 'pending') {
          return (
            <Tooltip title="主机已导入，但尚未绑定 SSH 凭证，请编辑主机配置">
              <Badge status="warning" text="待配置" style={{ color: '#faad14' }} />
            </Tooltip>
          )
        }
        if (t === 'healthy') return <Badge status="success" text="在线" />
        return (
          <Popover
            trigger="click"
            title="主机离线"
            content={
              <div style={{ maxWidth: 260 }}>
                <div style={{ color: '#8c8c8c', fontSize: 13, marginBottom: 10, wordBreak: 'break-all' }}>
                  {r.status?.[0] || '连接异常，无法连通 SSH'}
                </div>
                <Space>
                  <Button size="small" icon={<ApiOutlined />} onClick={() => openDrawer(r)}>SSH 测试</Button>
                  <Button size="small" type="primary" icon={<SyncOutlined />} onClick={() => handleRefreshConfig(r.id)}>刷新重连</Button>
                </Space>
              </div>
            }
          >
            <span style={{ cursor: 'pointer' }}><Badge status="error" text="离线" /></span>
          </Popover>
        )
      },
    },
    {
      title: '监控状态', key: 'observability', width: 110, align: 'center',
      render: (_: unknown, r) => {
        const mon = monMap.get(r.id)
        const log = logMap.get(r.id)
        const monState = lightState(mon)
        const logState = lightState(log)
        const monTip = monState === 'off' ? '监控未接入，点击去安装 Alloy' : monState === 'warn' ? (mon?.last_error || '心跳异常') : '监控正常，点击查看指标'
        const logTip = logState === 'off' ? '日志未接入，点击去安装 Alloy' : logState === 'warn' ? (log?.last_error || '心跳异常') : '日志采集正常，点击查看日志'
        const openMon = monState === 'off'
          ? () => navigate('/monitoring/collectors')
          : () => { setMetricAsset(r); setMetricOpen(true) }
        const openLog = logState === 'off'
          ? () => navigate('/monitoring/collectors')
          : () => { setLogAsset(r); setLogOpen(true) }
        return (
          <Space size={10}>
            {renderLight(monState, <LineChartOutlined />, '监控', monTip, openMon)}
            {renderLight(logState, <FileTextOutlined />, '日志', logTip, openLog)}
          </Space>
        )
      },
    },
    {
      title: '操作', key: 'action', width: 110, fixed: 'right', align: 'right',
      render: (_: unknown, r) => {
        const moreItems: MenuProps['items'] = [
          // 云账单 × CMDB 联动：云实例（i-xxx）可查费用，自建主机打开后是空态提示。
          ...(has('module.finance.overview')
            ? [{ key: 'cost', icon: <PayCircleOutlined />, label: '实例费用' }]
            : []),
          ...(has('module.wireguard.manage')
            ? [{ key: 'wg', icon: <LinkOutlined />, label: '虚拟组网' }]
            : []),
          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key === 'cost') { setCostAsset(r); return }
          if (key === 'wg') { setWgAsset(r); return }
          if (key === 'delete') {
            modal.confirm({
              title: `删除主机 ${r.name}`,
              content: '此操作不可恢复，确定继续？',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: () => confirm('hosts:delete', () => deleteMut.mutate(r.id)),
            })
          }
        }
        const offline = r.type !== 'healthy'
        return (
          <Space size={4}>
            <Tooltip title={offline ? '主机离线，无法连接终端' : '终端'}>
              <Button size="small" type="text" icon={<CodeOutlined />} disabled={offline} onClick={() => { setTermAsset(r); setTermModalOpen(true) }} />
            </Tooltip>
            <Tooltip title="编辑">
              <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openDrawer(r)} />
            </Tooltip>
            <Dropdown menu={{ items: moreItems, onClick: onMore }} trigger={['click']}>
              <Button size="small" type="text" icon={<MoreOutlined />} />
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  const rowSelection: TableRowSelection<AssetResponse> = {
    selectedRowKeys: selectedKeys,
    onChange: (keys) => setSelectedKeys(keys as string[]),
  }

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      <Flex gap={16} style={{ flex: 1, minWidth: 0 }}>
        <GroupSidebar
          resourceType="host"
          selectedGroupId={groupFilter}
          onSelect={setGroupFilter}
          selectedIds={selectedKeys}
        />

        <Flex vertical gap={12} style={{ flex: 1, minWidth: 0 }}>
        {/* 顶部概览统计卡片 */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>主机总数</span>}
                value={totalCount}
                prefix={<DesktopOutlined style={{ color: '#1677ff', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20 }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>在线</span>}
                value={onlineCount}
                prefix={<CheckCircleOutlined style={{ color: '#52c41a', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: '#52c41a' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>离线</span>}
                value={offlineCount}
                prefix={<CloseCircleOutlined style={{ color: '#ff4d4f', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: offlineCount > 0 ? '#ff4d4f' : 'inherit' }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
              <Statistic
                title={<span style={{ fontSize: 13, color: '#8c8c8c' }}>待配置</span>}
                value={pendingCount}
                prefix={<ExclamationCircleOutlined style={{ color: '#faad14', marginRight: 6 }} />}
                valueStyle={{ fontWeight: 600, fontSize: 20, color: pendingCount > 0 ? '#faad14' : 'inherit' }}
              />
            </Card>
          </Col>
        </Row>

        <Flex justify="space-between" wrap gap={8}>
          <Space wrap>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索名称或 IP"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              allowClear style={{ width: 220 }}
            />
            <Segmented
              value={statusFilter}
              onChange={(v) => setStatusFilter(v as typeof statusFilter)}
              options={[
                { label: '全部', value: 'all' },
                { label: '在线', value: 'online' },
                { label: '离线', value: 'offline' },
              ]}
            />
            <Select
              allowClear
              placeholder="来源"
              value={sourceFilter ?? undefined}
              onChange={(v) => setSourceFilter(v ?? null)}
              options={sourceOptions}
              style={{ width: 120 }}
            />
            <Select
              allowClear
              placeholder="配置采集"
              value={configFilter ?? undefined}
              onChange={(v) => setConfigFilter(v ?? null)}
              options={[
                { value: 'collecting', label: '采集中' },
                { value: 'failed', label: '采集失败' },
              ]}
              style={{ width: 120 }}
            />
          </Space>
          <Space>
            {selectedKeys.length > 0 && (
              <>
                <Button icon={<FolderOutlined />} onClick={() => { setMoveTarget(null); setMoveOpen(true) }}>
                  移动分组 ({selectedKeys.length})
                </Button>
                <Button icon={<SyncOutlined />} loading={batchRefreshMut.isPending} onClick={() => batchRefreshMut.mutate()}>
                  刷新配置
                </Button>
                <Button danger icon={<DeleteOutlined />} onClick={confirmBatchDelete}>
                  删除 ({selectedKeys.length})
                </Button>
              </>
            )}
            <Button icon={<DownloadOutlined />} onClick={handleExport} disabled={filtered.length === 0}>
              导出
            </Button>
            <Button icon={<CloudSyncOutlined />} onClick={openCloudSync}>云同步</Button>
            <Button icon={<KeyOutlined />} onClick={() => setHostKeysOpen(true)}>SSH 指纹</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>新增主机</Button>
          </Space>
        </Flex>

        <Table<AssetResponse>
          rowKey="id"
          rowSelection={rowSelection}
          columns={columns}
          dataSource={filtered}
          loading={isLoading}
          size="middle"
          scroll={{ x: 1070 }}
          locale={{
            emptyText: (
              <Empty
                image={<DesktopOutlined style={{ fontSize: 48, color: '#cbd5e1' }} />}
                styles={{ image: { height: 60 } }}
                description={
                  <span style={{ color: '#8c8c8c' }}>
                    {assets.length === 0 ? '还没有纳管任何主机' : '没有符合条件的主机'}
                  </span>
                }
              >
                {assets.length === 0 && (
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => openDrawer()}>
                    新增第一台主机
                  </Button>
                )}
              </Empty>
            ),
          }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 台主机` }}
        />

        <Drawer
          title={editingId ? '编辑主机' : '新增主机'}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width={520}
          footer={
            <Flex justify="flex-end" gap={8}>
              <Button onClick={handlePing} loading={pingLoading} icon={<ApiOutlined />}>SSH 测试</Button>
              <Button type="primary" onClick={() => form.validateFields().then((raw) => {
                // getFieldsValue(true) 合并隐藏卸载字段的残留值（硬件字段编辑态仍隐藏）
                const v = { ...form.getFieldsValue(true), ...(raw as AssetCreate) } as AssetCreate
                const payload: AssetCreate = {
                  ...v,
                  provider: v.provider || 'on-premise',
                  region: v.region || 'local',
                  // credential_ref 以选择框状态为准：清空（allowClear）即解绑凭证，
                  // 不能回落到表单里预填的旧值，否则解绑会被静默还原
                  credential_ref: credentialRef || '',
                }
                saveMut.mutate({ id: editingId ?? undefined, data: payload })
              })} loading={saveMut.isPending}>保存</Button>
            </Flex>
          }
        >
          {pingResult !== null && (
            <Alert
              type={pingResult.ok ? 'success' : 'error'}
              showIcon
              style={{ marginBottom: 16 }}
              message={pingResult.ok ? 'SSH 连通正常' : (pingResult.message || 'SSH 连接失败')}
            />
          )}

          {editingId && editingAsset && (
            <div style={{ marginBottom: 24, padding: 16, background: '#fafafa', borderRadius: 8 }}>
              <div style={{ fontWeight: 500, marginBottom: 12, color: '#262626' }}>硬件采集结果（最近一次 SSH 采集）</div>
              <Descriptions size="small" column={3}>
                <Descriptions.Item label="CPU">{editingAsset.cpu_cores ? `${editingAsset.cpu_cores} 核` : '-'}</Descriptions.Item>
                <Descriptions.Item label="内存">{editingAsset.memory_gb ? `${editingAsset.memory_gb} GB` : '-'}</Descriptions.Item>
                <Descriptions.Item label="系统盘">{editingAsset.system_disk_gb ? `${editingAsset.system_disk_gb} GB` : '-'}</Descriptions.Item>
              </Descriptions>
              <Text type="secondary" style={{ fontSize: 12 }}>提供商 / 地域 / 操作系统可在下方表单手工纠正；云同步主机（阿里云等）这三项由云平台数据锁定，编辑不生效。</Text>
            </div>
          )}

          <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
            <Form.Item name="name" label="主机名称" rules={[{ required: true }]}>
              <Input placeholder="例如 web-server-01" />
            </Form.Item>
            <Form.Item name="ip" label="IP 地址" rules={[{ required: true }]}>
              <Input placeholder="192.168.1.1" />
            </Form.Item>
            {(
              <>
                <Flex gap={16}>
                  <Form.Item
                    name="provider"
                    label="提供商"
                    style={{ flex: 1 }}
                    extra="可选；本机请选「本机」启用内置采集，留空默认物理机"
                  >
                    <Select
                      allowClear
                      placeholder="可选，留空默认物理机"
                      options={[
                        {
                          label: '自建 / 本地',
                          options: [
                            { value: 'localhost', label: '本机（OpsPilot 主机）' },
                            { value: 'on-premise', label: '物理机' },
                            { value: 'proxmox', label: 'Proxmox 虚拟机' },
                            { value: 'vmware', label: 'VMware 虚拟机' },
                          ],
                        },
                        {
                          label: '云厂商',
                          options: [
                            { value: 'aliyun', label: '阿里云' },
                            { value: 'tencent', label: '腾讯云' },
                            { value: 'aws', label: 'AWS' },
                          ],
                        },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="region" label="地域" style={{ flex: 1 }} extra="本地/物理机可留空，默认 local">
                    <Input placeholder="本地机可留空" />
                  </Form.Item>
                </Flex>
                <Form.Item name="os" label="操作系统">
                  <Input placeholder="例如 Ubuntu 22.04" />
                </Form.Item>
                <Flex gap={16}>
                  <Form.Item name="cpu_cores" label="CPU(核)" style={{ flex: 1 }}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="memory_gb" label="内存(GB)" style={{ flex: 1 }}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="system_disk_gb" label="系统盘(GB)" style={{ flex: 1 }}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                </Flex>
              </>
            )}
            <Flex gap={16}>
              <Form.Item name="ssh_user" label="SSH 用户" style={{ flex: 1 }} initialValue="root">
                <Input />
              </Form.Item>
              <Form.Item name="ssh_port" label="SSH 端口" style={{ flex: 1 }} initialValue={22}>
                <InputNumber min={1} max={65535} style={{ width: '100%' }} />
              </Form.Item>
            </Flex>
            <Form.Item
              name="ssh_password"
              label="SSH 密码"
              extra={editingId ? '留空表示不修改已保存的密码' : '可直接填写密码，也可以选择凭证中心中的 Password 凭证'}
            >
              <Input.Password placeholder="可选" autoComplete="new-password" />
            </Form.Item>
            <Form.Item label="凭证">
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  placeholder="选择 SSH 凭证"
                  allowClear
                  options={credOptions}
                  style={{ width: 'calc(100% - 72px)' }}
                  value={credentialRef || undefined}
                  onChange={(v) => setCredentialRef(v || '')}
                />
                <Button icon={<PlusOutlined />} onClick={() => { setCredType('password'); credForm.resetFields(); setCredModalOpen(true) }}>
                  新增
                </Button>
              </Space.Compact>
            </Form.Item>

            {editingId && (
              <div style={{ marginBottom: 16 }}>
                <Flex justify="space-between" align="center" style={{ marginBottom: 8 }}>
                  <span style={{ fontWeight: 500 }}>硬件配置明细</span>
                  <Button
                    size="small"
                    icon={editingCollecting ? <LoadingOutlined /> : <SyncOutlined />}
                    loading={editingCollecting}
                    onClick={() => handleRefreshConfig(editingId)}
                  >
                    {editingCollecting ? '采集中…' : '同步硬件配置'}
                  </Button>
                </Flex>
                {editingCollecting ? (
                  <Alert type="info" showIcon message="正在通过 SSH 采集硬件配置，约 2-8 秒…" />
                ) : editingAsset?.config_task_status === 'failed' ? (
                  <Alert type="error" showIcon message={`上次采集失败：${editingAsset.config_task_error || '未知原因'}`} />
                ) : (editingAsset?.disks?.length ?? 0) > 0 ? (
                  <Table
                    size="small"
                    rowKey="key"
                    pagination={false}
                    dataSource={editingAsset!.disks}
                    columns={[
                      {
                        title: '磁盘', dataIndex: 'key', width: 120,
                        render: (v: string, _r, i: number) => (
                          <Space size={6}>
                            <span>{v}</span>
                            <Tag color={i === 0 ? 'blue' : 'default'}>{i === 0 ? '系统盘' : '数据盘'}</Tag>
                          </Space>
                        ),
                      },
                      { title: '容量', dataIndex: 'size_gb', render: (v: number) => `${v} GB` },
                    ]}
                  />
                ) : (
                  <Alert type="warning" showIcon message="暂无磁盘明细，点击“同步硬件配置”采集" />
                )}
              </div>
            )}

            <Form.Item name="group_id" label="所属分组">
              <Select placeholder="选择分组" allowClear options={groupOptions} />
            </Form.Item>
            <Form.Item name="remark" label="备注">
              <Input.TextArea rows={2} />
            </Form.Item>
          </Form>
        </Drawer>

        {/* ── 浮动 / 最大化全屏 macOS 终端 Modal ── */}
        <TerminalModal
          open={termModalOpen}
          assetId={termAsset?.id ?? ''}
          assetName={termAsset?.name}
          assetIp={termAsset?.ip}
          onClose={() => setTermModalOpen(false)}
        />

        {/* ── 虚拟组网抽屉（主机 × WireGuard 联动） ── */}
        <WgMembershipDrawer
          open={!!wgAsset}
          assetId={wgAsset?.id ?? ''}
          hostName={wgAsset?.name}
          onClose={() => setWgAsset(null)}
        />

        {/* ── 实例费用抽屉（云账单 × CMDB 联动） ── */}
        <InstanceCostDrawer
          open={!!costAsset}
          instanceId={costAsset?.id ?? ''}
          hostName={costAsset?.name}
          onClose={() => setCostAsset(null)}
        />

        {/* ── 主机监控指标抽屉 ── */}
        <HostMetricsDrawer
          open={metricOpen}
          assetName={metricAsset?.name}
          assetIp={metricAsset?.ip}
          onClose={() => setMetricOpen(false)}
        />

        {/* ── 主机日志抽屉（日志灯专用） ── */}
        <HostLogsDrawer
          open={logOpen}
          assetName={logAsset?.name}
          assetIp={logAsset?.ip}
          onClose={() => setLogOpen(false)}
        />

        {/* ── 批量移动分组 ── */}
        <Modal
          title={`移动 ${selectedKeys.length} 台主机到分组`}
          open={moveOpen}
          onCancel={() => setMoveOpen(false)}
          onOk={() => batchMoveMut.mutate()}
          okText="移动"
          confirmLoading={batchMoveMut.isPending}
        >
          <Select
            style={{ width: '100%' }}
            placeholder="选择目标分组"
            value={moveTarget ?? undefined}
            onChange={(v) => setMoveTarget(v ?? null)}
            allowClear
            options={[{ value: null as unknown as number, label: '未分组' }, ...groupOptions]}
          />
        </Modal>

        {/* ── 云同步 ── */}
        <Modal
          title={
            <Flex align="center" gap={8}>
              <CloudOutlined style={{ color: '#1677ff' }} />
              <span>云账号批量导入主机</span>
            </Flex>
          }
          open={cloudSyncOpen}
          onCancel={() => setCloudSyncOpen(false)}
          footer={null}
          width={syncStep === 'preview' ? 1000 : 600}
          destroyOnClose
        >
          {syncStep === 'form' && (
            <Flex vertical gap={16} style={{ paddingTop: 12 }}>
              <Form layout="vertical">
                <Form.Item label="云厂商" required>
                  <Radio.Group value={syncProvider} onChange={(e) => setSyncProvider(e.target.value)}>
                    <Radio.Button value="aliyun">阿里云</Radio.Button>
                    <Radio.Button value="tencent">腾讯云</Radio.Button>
                    <Radio.Button value="aws">AWS</Radio.Button>
                  </Radio.Group>
                </Form.Item>

                <Form.Item label="凭证来源" required>
                  <Radio.Group value={syncUseSaved} onChange={(e) => setSyncUseSaved(e.target.value)}>
                    <Radio value={true}>使用已保存凭证</Radio>
                    <Radio value={false}>临时输入 AK/SK</Radio>
                  </Radio.Group>
                </Form.Item>

                {syncUseSaved ? (
                  <Form.Item label="选择凭证" required>
                    <Select
                      placeholder="请选择 AccessKey 类型凭证"
                      value={syncCredId || undefined}
                      onChange={(v) => setSyncCredId(v)}
                      options={akCredentials.map((c) => ({ value: c.cred_id, label: c.name }))}
                      notFoundContent={<Empty description="暂无 AccessKey 凭证，请先在凭证管理中添加" />}
                    />
                  </Form.Item>
                ) : (
                  <>
                    <Form.Item label="Access Key" required>
                      <Input placeholder="请输入 Access Key ID" value={syncAK} onChange={(e) => setSyncAK(e.target.value)} />
                    </Form.Item>
                    <Form.Item label="Secret Key" required>
                      <Input.Password placeholder="请输入 Secret Access Key" value={syncSK} onChange={(e) => setSyncSK(e.target.value)} />
                    </Form.Item>
                  </>
                )}

                <Form.Item>
                  <Button
                    type="primary"
                    icon={<CloudSyncOutlined />}
                    onClick={handleSyncScan}
                    loading={syncScanning}
                    block
                  >
                    {syncScanning ? '正在获取主机…' : '获取主机'}
                  </Button>
                </Form.Item>

                {syncScanning && syncRegions.length > 0 && (
                  <div style={{ marginTop: 16, padding: 12, background: '#fafafa', borderRadius: 4, maxHeight: 320, overflowY: 'auto' }}>
                    <div style={{ marginBottom: 12, fontWeight: 500, color: '#333' }}>
                      正在扫描 {syncRegions.length} 个区域…
                    </div>
                    {syncRegions.map((r) => {
                      const prog = syncProgress[r] || { status: 'pending' }
                      const color =
                        prog.status === 'success' ? '#52c41a' :
                        prog.status === 'failed' ? '#ff4d4f' :
                        prog.status === 'scanning' ? '#1890ff' : '#d9d9d9'
                      const icon =
                        prog.status === 'success' ? '✓' :
                        prog.status === 'failed' ? '✗' :
                        prog.status === 'scanning' ? '⟳' : '·'
                      return (
                        <div key={r} style={{ display: 'flex', alignItems: 'center', marginBottom: 6, fontSize: 13 }}>
                          <span style={{ color, marginRight: 8, fontWeight: 'bold', fontSize: 15 }}>{icon}</span>
                          <span style={{ flex: 1, color: '#333' }}>{r}</span>
                          {prog.status === 'success' && prog.count !== undefined && (
                            <span style={{ color: '#52c41a', fontSize: 12 }}>发现 {prog.count} 台</span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </Form>
            </Flex>
          )}

          {syncStep === 'preview' && (
            <Flex vertical gap={16} style={{ paddingTop: 12 }}>
              <Alert
                message={`共扫描到 ${syncInstances.length} 台实例，已导入 ${syncInstances.filter((i) => i.is_imported).length} 台，可新增 ${syncInstances.filter((i) => !i.is_imported).length} 台`}
                type="info"
                showIcon
              />

              <Tabs
                items={[
                  {
                    key: 'ecs',
                    label: '云服务器 (ECS/CVM/EC2)',
                    children: (
                      <Table
                        rowKey="instance_id"
                        size="small"
                        dataSource={syncInstances.filter((i) => ['ECS', 'CVM', 'EC2'].includes(i.cloud_resource_type))}
                        rowSelection={{
                          selectedRowKeys: syncSelectedIds,
                          onChange: (keys) => setSyncSelectedIds(keys as string[]),
                          getCheckboxProps: (r) => ({ disabled: r.is_imported }),
                        }}
                        columns={[
                          { title: '实例名称', dataIndex: 'name', width: 180 },
                          { title: '实例ID', dataIndex: 'instance_id', width: 140 },
                          { title: 'IP', dataIndex: 'ip', width: 120 },
                          { title: '区域', dataIndex: 'region', width: 120 },
                          { title: 'CPU', dataIndex: 'cpu_cores', width: 60, render: (v) => `${v}核` },
                          { title: '内存', dataIndex: 'memory_gb', width: 70, render: (v) => `${v}GB` },
                          { title: '系统盘', dataIndex: 'system_disk_gb', width: 80, render: (v) => `${v}GB` },
                          { title: '状态', dataIndex: 'status', width: 80 },
                          {
                            title: '导入状态',
                            dataIndex: 'is_imported',
                            width: 100,
                            render: (v) => v ? <Tag color="default">已导入</Tag> : <Tag color="success">可导入</Tag>,
                          },
                        ]}
                        pagination={false}
                        scroll={{ y: 300 }}
                      />
                    ),
                  },
                  {
                    key: 'light',
                    label: '轻量服务器 (SWAS/Lighthouse/Lightsail)',
                    children: (
                      <Table
                        rowKey="instance_id"
                        size="small"
                        dataSource={syncInstances.filter((i) => ['SWAS', 'Lighthouse', 'Lightsail'].includes(i.cloud_resource_type))}
                        rowSelection={{
                          selectedRowKeys: syncSelectedIds,
                          onChange: (keys) => setSyncSelectedIds(keys as string[]),
                          getCheckboxProps: (r) => ({ disabled: r.is_imported }),
                        }}
                        columns={[
                          { title: '实例名称', dataIndex: 'name', width: 180 },
                          { title: '实例ID', dataIndex: 'instance_id', width: 140 },
                          { title: 'IP', dataIndex: 'ip', width: 120 },
                          { title: '区域', dataIndex: 'region', width: 120 },
                          { title: 'CPU', dataIndex: 'cpu_cores', width: 60, render: (v) => `${v}核` },
                          { title: '内存', dataIndex: 'memory_gb', width: 70, render: (v) => `${v}GB` },
                          { title: '系统盘', dataIndex: 'system_disk_gb', width: 80, render: (v) => `${v}GB` },
                          { title: '状态', dataIndex: 'status', width: 80 },
                          {
                            title: '导入状态',
                            dataIndex: 'is_imported',
                            width: 100,
                            render: (v) => v ? <Tag color="default">已导入</Tag> : <Tag color="success">可导入</Tag>,
                          },
                        ]}
                        pagination={false}
                        scroll={{ y: 300 }}
                      />
                    ),
                  },
                ]}
              />

              <Flex justify="space-between">
                <Button onClick={() => setSyncStep('form')}>返回</Button>
                <Button
                  type="primary"
                  icon={<CloudSyncOutlined />}
                  onClick={handleSyncImport}
                  loading={syncImporting}
                  disabled={syncSelectedIds.length === 0}
                >
                  导入 {syncSelectedIds.length} 台主机
                </Button>
              </Flex>
            </Flex>
          )}
        </Modal>
        {/* ── 快速新增凭证 ── */}
        <Modal
          title="新增 SSH 凭证"
          open={credModalOpen}
          onCancel={() => setCredModalOpen(false)}
          onOk={handleCredSave}
          okText="保存并使用"
          confirmLoading={credSaveMut.isPending}
          destroyOnClose
          width={480}
        >
          <Form form={credForm} layout="vertical" style={{ marginTop: 8 }}>
            <Form.Item name="name" label="凭证名称" rules={[{ required: true }]}>
              <Input placeholder="例如 prod-server-root" />
            </Form.Item>
            <Form.Item label="凭证类型" required>
              <Segmented
                value={credType}
                onChange={(v) => setCredType(v as 'password' | 'ssh_key')}
                options={[
                  { label: '账户密码', value: 'password' },
                  { label: 'SSH 密钥', value: 'ssh_key' },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="username" label="用户名"
              tooltip="仅用于本次填写提示，登录用户以主机表单的「SSH 用户」为准"
              initialValue="root"
            >
              <Input placeholder="root" />
            </Form.Item>
            {credType === 'password' && (
              <Form.Item name="password" label="密码" rules={[{ required: true }]}>
                <Input.Password placeholder="请输入 SSH 密码" />
              </Form.Item>
            )}
            {credType === 'ssh_key' && (
              <Form.Item name="private_key" label="私钥内容" rules={[{ required: true }]}>
                <Input.TextArea rows={5} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
              </Form.Item>
            )}
          </Form>
        </Modal>

        {/* ── 主机详情抽屉 ── */}
        <Drawer
          title={<Flex align="center" gap={8}><InfoCircleOutlined style={{ color: '#1677ff' }} /><span>主机详情</span></Flex>}
          open={detailOpen}
          onClose={() => setDetailOpen(false)}
          width={680}
          footer={
            <Flex justify="flex-end" gap={8}>
              <Button onClick={() => setDetailOpen(false)}>关闭</Button>
              <Button type="primary" icon={<EditOutlined />} onClick={() => { setDetailOpen(false); openDrawer(detailAsset ?? undefined) }}>
                编辑主机
              </Button>
            </Flex>
          }
        >
          {detailAsset && (
            <Flex vertical gap={24}>
              <RelationCard
                type="host"
                id={detailAsset.id}
                costInstanceId={detailAsset.id}
                title="关联应用 / 责任 / 费用"
              />
              {/* 基本信息 */}
              <div>
                <div style={{ fontWeight: 500, marginBottom: 12, fontSize: 15, color: '#262626' }}>基本信息</div>
                <Descriptions bordered size="small" column={2}>
                  <Descriptions.Item label="主机名称" span={2}>{detailAsset.name}</Descriptions.Item>
                  <Descriptions.Item label="IP 地址">{detailAsset.ip}</Descriptions.Item>
                  <Descriptions.Item label="提供商">
                    <Tag color={providerMeta(detailAsset.provider).tagColor}>{providerMeta(detailAsset.provider).label}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="地域">{detailAsset.region}</Descriptions.Item>
                  <Descriptions.Item label="所属分组">
                    {detailAsset.group_id ? groups.find((g) => g.id === detailAsset.group_id)?.name || '-' : '未分组'}
                  </Descriptions.Item>
                  {detailAsset.remark && <Descriptions.Item label="备注" span={2}>{detailAsset.remark}</Descriptions.Item>}
                </Descriptions>
              </div>

              {/* 配置信息 */}
              <div>
                <div style={{ fontWeight: 500, marginBottom: 12, fontSize: 15, color: '#262626' }}>配置信息</div>
                <Descriptions bordered size="small" column={2}>
                  <Descriptions.Item label="操作系统" span={2}>{detailAsset.os || '-'}</Descriptions.Item>
                  <Descriptions.Item label="CPU">{detailAsset.cpu_cores ? `${detailAsset.cpu_cores} 核` : '-'}</Descriptions.Item>
                  <Descriptions.Item label="内存">{detailAsset.memory_gb ? `${detailAsset.memory_gb} GB` : '-'}</Descriptions.Item>
                  <Descriptions.Item label="系统盘">{detailAsset.system_disk_gb ? `${detailAsset.system_disk_gb} GB` : '-'}</Descriptions.Item>
                  <Descriptions.Item label="总磁盘数">{detailAsset.disks?.length || 0} 块</Descriptions.Item>
                </Descriptions>
                {detailAsset.disks && detailAsset.disks.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <div style={{ fontSize: 13, color: '#8c8c8c', marginBottom: 8 }}>磁盘明细</div>
                    <Table
                      size="small"
                      rowKey="key"
                      pagination={false}
                      dataSource={detailAsset.disks}
                      columns={[
                        {
                          title: '磁盘', dataIndex: 'key', width: 140,
                          render: (v: string, _r, i: number) => (
                            <Space size={6}>
                              <span>{v}</span>
                              <Tag color={i === 0 ? 'blue' : 'default'}>{i === 0 ? '系统盘' : '数据盘'}</Tag>
                            </Space>
                          ),
                        },
                        { title: '容量', dataIndex: 'size_gb', render: (v: number) => `${v} GB` },
                      ]}
                    />
                  </div>
                )}
              </div>

              {/* SSH 连接信息 */}
              <div>
                <div style={{ fontWeight: 500, marginBottom: 12, fontSize: 15, color: '#262626' }}>SSH 连接信息</div>
                <Descriptions bordered size="small" column={2}>
                  <Descriptions.Item label="SSH 用户">{detailAsset.ssh_user}</Descriptions.Item>
                  <Descriptions.Item label="SSH 端口">{detailAsset.ssh_port}</Descriptions.Item>
                  <Descriptions.Item label="使用凭证" span={2}>
                    {detailAsset.credential_ref
                      ? credentials.find((c) => c.cred_id === detailAsset.credential_ref)?.name || detailAsset.credential_ref
                      : <span style={{ color: '#faad14' }}>未绑定凭证</span>}
                  </Descriptions.Item>
                </Descriptions>
              </div>

              {/* 状态信息 */}
              <div>
                <div style={{ fontWeight: 500, marginBottom: 12, fontSize: 15, color: '#262626' }}>状态信息</div>
                <Descriptions bordered size="small" column={2}>
                  <Descriptions.Item label="连接状态">
                    {detailAsset.type === 'healthy' ? <Badge status="success" text="在线" /> :
                     detailAsset.type === 'pending' ? <Badge status="warning" text="待配置" /> :
                     <Badge status="error" text="离线" />}
                  </Descriptions.Item>
                  <Descriptions.Item label="配置采集">
                    {detailAsset.config_task_status === 'collecting' && <Tag color="processing">采集中</Tag>}
                    {detailAsset.config_task_status === 'success' && <Tag color="success">已采集</Tag>}
                    {detailAsset.config_task_status === 'failed' && <Tag color="error">采集失败</Tag>}
                    {!detailAsset.config_task_status && <Tag>未采集</Tag>}
                  </Descriptions.Item>
                  {detailAsset.config_task_status === 'failed' && detailAsset.config_task_error && (
                    <Descriptions.Item label="采集错误" span={2}>
                      <span style={{ color: '#ff4d4f', fontSize: 12 }}>{detailAsset.config_task_error}</span>
                    </Descriptions.Item>
                  )}
                  {detailAsset.health_last_checked_at && (
                    <Descriptions.Item label="最近检查" span={2}>{detailAsset.health_last_checked_at}</Descriptions.Item>
                  )}
                  {detailAsset.config_task_updated_at && (
                    <Descriptions.Item label="采集时间" span={2}>{detailAsset.config_task_updated_at}</Descriptions.Item>
                  )}
                </Descriptions>
              </div>
            </Flex>
          )}
        </Drawer>

        <Drawer
          title="SSH 主机指纹（TOFU 钉扎）"
          open={hostKeysOpen}
          onClose={() => setHostKeysOpen(false)}
          width={720}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="指纹变化会拒绝连接（疑似中间人）。服务器重装/换钥匙后请管理员清除对应指纹。"
          />
          <Table
            rowKey={(r) => `${r.host}:${r.port}:${r.key_type}`}
            size="small"
            dataSource={hostKeys}
            pagination={{ pageSize: 10 }}
            columns={[
              { title: '主机', dataIndex: 'host', width: 140 },
              { title: '端口', dataIndex: 'port', width: 70 },
              { title: '类型', dataIndex: 'key_type', width: 120 },
              { title: '指纹', dataIndex: 'fingerprint', ellipsis: true },
              { title: '首连', dataIndex: 'first_seen_at', width: 150 },
              { title: '最近校验', dataIndex: 'last_seen_at', width: 150 },
              {
                title: '操作', width: 100,
                render: (_, r) => {
                  const asset = assets.find((a) => a.ip === r.host && (a.ssh_port || 22) === r.port)
                  return (
                    <Popconfirm
                      title="清除该主机全部指纹？"
                      description="下次连接将按首连重新钉扎"
                      okText="清除"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => {
                        if (!asset) { message.warning('未匹配到主机资产，请到主机详情里重置'); return }
                        resetHostKeyMut.mutate(asset.id)
                      }}
                    >
                      <Button size="small" danger disabled={!asset}>清除</Button>
                    </Popconfirm>
                  )
                },
              },
            ]}
          />
        </Drawer>
        </Flex>
      </Flex>

    </Flex>
  )
}
