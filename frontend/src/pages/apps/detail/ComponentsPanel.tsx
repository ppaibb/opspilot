import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  Flex, Card, Table, Button, Tag, Space, Alert, Tooltip, Form, Input, Select,
  Dropdown, Popconfirm, Checkbox, Collapse, Progress, Steps, Empty, Switch, Segmented, Badge,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { App } from 'antd'
import {
  DesktopOutlined, AppstoreOutlined,
  ReloadOutlined, SearchOutlined,
  PlusOutlined, DeleteOutlined, MoreOutlined, QuestionCircleOutlined, SafetyCertificateOutlined,
} from '@ant-design/icons'
import { applicationsApi } from '@/api'
import client from '@/api/client'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type {
  ApplicationResourceItem, ApplicationComponentResponse,
  ApplicationRuntimeDiscoveryCandidate, ApplicationComponentPayload,
  ApplicationComposePreflightResponse, ApplicationComposePreflightRequest,
} from '@/types/api'
import {
  ENV_COLOR, ENV_LABEL, COMPONENT_STATUS_COLOR, COMPONENT_STATUS_LABEL,
  DEPLOY_TYPES, DISCOVERY_TYPES, getSkippedEnvironment, controlActions,
} from './constants'
import type { ComponentFormValues, ScanHostStatus, ScanStage } from './types'

export default function ComponentsPanel({
  appId,
  resources,
  components,
  autoRefresh,
  setAutoRefresh,
  onControl,
  controlPendingComponentId,
  onDelete,
  onOpenDetail,
  onDeployStarted,
  invalidate,
}: {
  appId: string
  resources: ApplicationResourceItem[]
  components: ApplicationComponentResponse[]
  autoRefresh: boolean
  setAutoRefresh: (v: boolean) => void
  onControl: (componentId: string, action: string) => void
  controlPendingComponentId?: string
  onDelete: (componentId: string) => void
  onOpenDetail: (component: ApplicationComponentResponse) => void
  onDeployStarted: (taskId: string) => void
  invalidate: () => void
}) {
  const { message, modal } = App.useApp()

  const [addMode, setAddMode]                       = useState<'scan' | 'manual' | null>(null)
  const [scanCandidates, setScanCandidates]         = useState<ApplicationRuntimeDiscoveryCandidate[]>([])
  const [scanErrors, setScanErrors]                 = useState<string[]>([])
  const [scanSkipped, setScanSkipped]               = useState<Record<string, number>>({})
  const [scanStage, setScanStage]                   = useState<ScanStage>('config')
  const [scanAssetIds, setScanAssetIds]             = useState<string[]>([])
  const [scanTypes, setScanTypes]                   = useState<string[]>(['docker', 'docker_compose', 'supervisor'])
  const [includeSystemServices, setIncludeSystemServices] = useState(false)
  const [scanFilteredCount, setScanFilteredCount]   = useState(0)
  const [scanLoading, setScanLoading]               = useState(false)
  const [scanProgress, setScanProgress]             = useState<{ current: number; total: number; currentHost: string }>({ current: 0, total: 0, currentHost: '' })
  const [scanHostProgress, setScanHostProgress]     = useState<Record<string, { name: string; status: ScanHostStatus; count?: number; errorDetail?: string }>>({})
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([])
  const [importLoading, setImportLoading]           = useState(false)
  const [typeFilter, setTypeFilter]                 = useState<string>('all')
  const [componentTypeFilter, setComponentTypeFilter] = useState<string>('all')
  const [componentSearch, setComponentSearch]         = useState('')
  const [componentView, setComponentView]             = useState<'flat' | 'host' | 'project'>('flat')
  const [selectedComponentIds, setSelectedComponentIds] = useState<string[]>([])
  const [manualForm]                                = Form.useForm<ComponentFormValues>()
  const [composePreflight, setComposePreflight]     = useState<ApplicationComposePreflightResponse | null>(null)

  const batchDeleteMut = useMutation({
    mutationFn: (componentIds: string[]) => applicationsApi.batchDeleteComponents(appId, componentIds),
    onSuccess: (result) => {
      invalidate()
      setSelectedComponentIds([])
      message.success(`已移除 ${result.deleted_count} 个部署组件`)
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '批量移除失败'),
  })

  const createMut = useMutation({
    mutationFn: (data: ApplicationComponentPayload) => {
      // 如果是部署模式，启用异步部署
      if (data.create_mode === 'deploy' && data.deployment_type === 'docker_compose') {
        return client
          .post(`/applications/${appId}/components`, data, { params: { async_deploy: true } })
          .then(r => r.data)
      }
      // 否则使用原有的同步接口
      return applicationsApi.createComponent(appId, data)
    },
    onSuccess: (result, variables) => {
      // 如果返回了 task_id，说明是异步部署
      if (result.task_id) {
        onDeployStarted(result.task_id)
        setAddMode(null)
        manualForm.resetFields()
        return
      }
      
      // 同步部署或纳管模式
      invalidate()
      setAddMode(null)
      manualForm.resetFields()
      message.success(variables.create_mode === 'deploy' ? '部署完成，项目服务已自动发现并登记' : '部署组件已添加')
    },
    onError: (err: unknown, variables) => {
      const detail = extractAxiosDetail(err) ?? '添加失败'
      if (variables.create_mode === 'deploy') {
        modal.error({
          title: '部署失败',
          content: <pre style={{ margin: 0, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{detail}</pre>,
          okText: '关闭',
          width: 640,
        })
      } else {
        message.error(detail)
      }
    },
  })

  const composePreflightMut = useMutation({
    mutationFn: (data: ApplicationComposePreflightRequest) => applicationsApi.preflightCompose(appId, data),
    onSuccess: (result) => {
      setComposePreflight(result)
      if (result.ok) message.success('部署前检查通过')
      else message.warning('部署前检查未通过，请查看检查结果')
    },
    onError: (err: unknown) => {
      setComposePreflight(null)
      message.error(extractAxiosDetail(err) ?? '部署前检查失败')
    },
  })

  const getComposePreflightPayload = async () => {
    const values = await manualForm.validateFields([
      'target_resource_id', 'compose_project_name', 'compose_deploy_dir', 'compose_filename',
      'compose_content', 'compose_env_filename', 'compose_env_content',
    ])
    return {
      resource_id: values.target_resource_id!,
      project_name: values.compose_project_name!,
      deploy_dir: values.compose_deploy_dir!,
      compose_filename: values.compose_filename || 'docker-compose.yml',
      compose_content: values.compose_content || '',
      env_filename: values.compose_env_filename || '.env',
      env_content: values.compose_env_content || '',
    }
  }

  const handleComposePreflight = async () => {
    setComposePreflight(null)
    composePreflightMut.mutate(await getComposePreflightPayload())
  }

  const handleComponentSubmit = async () => {
    const values = await manualForm.validateFields()
    const payload: ApplicationComponentPayload = { ...values }
    delete (payload as ComponentFormValues).target_resource_id
    delete (payload as ComponentFormValues).compose_project_name
    delete (payload as ComponentFormValues).compose_deploy_dir
    delete (payload as ComponentFormValues).compose_filename

    if (values.create_mode === 'deploy') {
      payload.resource_refs = [`host:${values.target_resource_id}`]
      if (values.deployment_type === 'docker_compose') {
        if (!composePreflight?.ok) { message.warning('请先完成并通过部署前检查'); return }
        const deployDir = values.compose_deploy_dir!.replace(/\/$/, '')
        const composeFilename = values.compose_filename || 'docker-compose.yml'
        payload.name = values.compose_project_name!
        payload.control_ref = `compose=${deployDir}/${composeFilename}`
        payload.deploy_config = { compose_content: values.compose_content }
      }
    }
    createMut.mutate(payload)
  }

  const handleScan = async () => {
    const hostResources = resources.filter((r) => r.resource_type === 'host' && scanAssetIds.includes(r.resource_id))
    if (hostResources.length === 0) { message.warning('当前应用未绑定主机，无法扫描'); return }
    if (scanTypes.length === 0) { message.warning('请至少选择一种运行环境'); return }

    setScanLoading(true)
    setScanStage('scanning')
    setScanCandidates([])
    setScanErrors([])
    setScanSkipped({})
    setSelectedCandidates([])
    setTypeFilter('all')
    setScanFilteredCount(0)
    setScanProgress({ current: 0, total: hostResources.length, currentHost: '' })
    setScanHostProgress(Object.fromEntries(hostResources.map((host) => [host.resource_id, {
      name: host.resource_name || host.resource_id,
      status: 'pending' as ScanHostStatus,
    }])))

    const allCandidates: ApplicationRuntimeDiscoveryCandidate[] = []
    const realErrors: string[] = []
    const skippedEnvironments: Record<string, number> = {}

    for (let i = 0; i < hostResources.length; i++) {
      const h = hostResources[i]
      setScanProgress({ current: i + 1, total: hostResources.length, currentHost: h.resource_name || h.resource_id })
      setScanHostProgress(prev => ({ ...prev, [h.resource_id]: { ...prev[h.resource_id], status: 'scanning' } }))
      try {
        const res = await applicationsApi.discoverComponents(appId, [h.resource_id], scanTypes, includeSystemServices)
        allCandidates.push(...res.candidates)
        setScanFilteredCount(prev => prev + res.filtered_count)
        setScanCandidates([...allCandidates])
        const meaningful = res.errors.filter((error) => {
          const environment = getSkippedEnvironment(error)
          if (!environment) return true
          skippedEnvironments[environment] = (skippedEnvironments[environment] || 0) + 1
          return false
        })
        realErrors.push(...meaningful)
        setScanErrors([...realErrors])
        setScanSkipped({ ...skippedEnvironments })
        setScanHostProgress(prev => ({
          ...prev,
          [h.resource_id]: { 
            ...prev[h.resource_id], 
            status: meaningful.length > 0 ? 'failed' : 'success', 
            count: res.candidates.length,
            errorDetail: meaningful.length > 0 ? meaningful[0] : undefined
          },
        }))
      } catch (err: unknown) {
        const msg = extractAxiosDetail(err) ?? `${h.resource_name || h.resource_id} 扫描失败`
        realErrors.push(msg)
        setScanErrors([...realErrors])
        setScanHostProgress(prev => ({ 
          ...prev, 
          [h.resource_id]: { ...prev[h.resource_id], status: 'failed', errorDetail: msg } 
        }))
      }
    }

    setScanLoading(false)
    setSelectedCandidates([])
    setScanStage('preview')
    setScanProgress(prev => ({ ...prev, current: prev.total }))
    if (allCandidates.length > 0) {
      message.success(`扫描完成，共发现 ${allCandidates.length} 个实例`)
    } else {
      message.info('扫描完成，未发现可纳管的部署组件')
    }
  }

  const handleImportSelected = async () => {
    const toAdd = scanCandidates.filter((c) => selectedCandidates.includes(`${c.asset_id}:${c.control_ref}`))
    if (toAdd.length === 0) { message.warning('请先选择要纳管的部署组件'); return }
    const runImport = async () => {
      setImportLoading(true)
      let done = 0
      try {
        for (const c of toAdd) {
          try {
            await applicationsApi.createComponent(appId, {
              name: c.name,
              deployment_type: c.deployment_type,
              control_ref: c.control_ref,
              resource_refs: [`host:${c.asset_id}`],
              create_mode: 'manage',
              description: c.description,
            })
          done++
          } catch { /* 单条失败不中断 */ }
        }
        invalidate(); setAddMode(null); setScanCandidates([]); setSelectedCandidates([])
        message.success(`已处理 ${done} 个部署组件，重复项已自动跳过`)
      } finally {
        setImportLoading(false)
      }
    }
    modal.confirm({
      title: `确认纳管 ${toAdd.length} 个新实例？`,
      content: toAdd.length > 100 ? '本次数量较大，请确认筛选范围和运行环境无误。已纳管项不会重复创建。' : '纳管后可在部署组件列表中查看和控制。',
      okText: '确认纳管',
      cancelText: '返回检查',
      onOk: runImport,
    })
  }

  const canControl = (type: string) => ['systemd', 'supervisor', 'docker', 'docker_compose'].includes(type)
  const hostResourceMap = new Map(
    resources
      .filter(resource => resource.resource_type === 'host')
      .map(resource => [resource.resource_id, resource.resource_name || resource.resource_id]),
  )
  const componentHostIds = (component: ApplicationComponentResponse) => component.resource_refs
    .filter(ref => ref.startsWith('host:'))
    .map(ref => ref.slice(5))
  const componentHostLabel = (component: ApplicationComponentResponse) => {
    const hostIds = componentHostIds(component)
    if (hostIds.length === 0) return '未绑定主机'
    return hostIds.map(hostId => hostResourceMap.get(hostId) || hostId).join('、')
  }
  const componentProjectLabel = (component: ApplicationComponentResponse) => {
    if (component.deployment_type !== 'docker_compose') return ''
    const name = component.name || ''
    return name.includes(' / ') ? name.split(' / ', 1)[0] : 'Compose 项目'
  }

  const componentColumns: ColumnsType<ApplicationComponentResponse> = [
    { 
      title: '组件名称', 
      dataIndex: 'name', 
      width: 200, 
      render: (v, r) => (
        <Space direction="vertical" size={2}>
          <span style={{ fontWeight: 500 }}>{v}</span>
          <Tag color="blue" style={{ fontSize: 11 }}>{r.deployment_type}</Tag>
        </Space>
      )
    },
    { 
      title: '所属主机', 
      key: 'host', 
      width: 160, 
      render: (_: unknown, component) => (
        <Tooltip title={componentHostIds(component).join('、') || undefined}>
          {componentHostLabel(component)}
        </Tooltip>
      )
    },
    { 
      title: '状态', 
      dataIndex: 'status', 
      width: 100, 
      render: (v) => <Badge status={COMPONENT_STATUS_COLOR[v] || 'default'} text={COMPONENT_STATUS_LABEL[v] || v} /> 
    },
    { 
      title: '环境', 
      dataIndex: 'env', 
      width: 80, 
      render: (v) => <Tag color={ENV_COLOR[v] || 'default'}>{ENV_LABEL[v] || v}</Tag> 
    },
    {
      title: '最近操作', 
      key: 'last_control', 
      width: 160,
      render: (_: unknown, r) => r.last_control_at ? (
        <Tooltip title={`${r.last_control_action} → ${r.last_control_result || '无结果'}`}>
          <span style={{ color: r.last_control_result === 'failed' ? '#ff4d4f' : '#64748b', fontSize: 13 }}>
            {r.last_control_at.slice(0, 16)}
          </span>
        </Tooltip>
      ) : '-',
    },
    {
      title: '操作', key: 'action', width: 130, fixed: 'right', align: 'right',
      render: (_: unknown, r) => (
        <Space size={4}>
          {canControl(r.deployment_type) ? (
            <Dropdown trigger={['click']} menu={{
              items: controlActions.map((a) => ({ key: a.key, icon: a.icon, label: a.label })),
              onClick: ({ key }) => modal.confirm({
                title: `对「${r.name}」执行 ${controlActions.find(a => a.key === key)?.label}？`,
                okText: '执行',
                onOk: () => onControl(r.component_id, key),
              }),
            }}>
              <Button size="small" loading={controlPendingComponentId === r.component_id}>
                组件操作 <MoreOutlined />
              </Button>
            </Dropdown>
          ) : (
            <Tooltip title="此部署类型暂不支持远程控制"><Button size="small" disabled>控制</Button></Tooltip>
          )}
          <Popconfirm title="移除该部署组件？" description="仅移除登记记录，不会停止实际进程" okText="移除" cancelText="取消" onConfirm={() => onDelete(r.component_id)}>
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const scanCandidateColumns: ColumnsType<ApplicationRuntimeDiscoveryCandidate> = [
    {
      title: '', key: 'check', width: 40,
      render: (_: unknown, r) => {
        const key = `${r.asset_id}:${r.control_ref}`
        return <Checkbox disabled={r.management_status === 'managed'} checked={selectedCandidates.includes(key)} onChange={(e) =>
          setSelectedCandidates(prev => e.target.checked ? [...prev, key] : prev.filter(k => k !== key))
        } />
      },
    },
    { title: '主机', dataIndex: 'asset_name', width: 140 },
    { title: '类型', dataIndex: 'deployment_type', width: 110, render: (v) => <Tag color="blue">{v}</Tag> },
    { title: 'Compose 项目', dataIndex: 'project_name', width: 140, render: (v, r) => r.deployment_type === 'docker_compose' ? (v || '-') : '-' },
    { title: '名称', dataIndex: 'name', render: (v) => <code style={{ fontSize: 12 }}>{v}</code> },
    { title: '状态', dataIndex: 'status', width: 80, render: (v) => <Badge status={COMPONENT_STATUS_COLOR[v] || 'default'} text={v} /> },
    { title: '纳管状态', dataIndex: 'management_status', width: 90, render: (v) => v === 'managed' ? <Tag color="default">已纳管</Tag> : <Tag color="green">新发现</Tag> },
  ]

  const candidateTypes = Array.from(new Set(scanCandidates.map(candidate => candidate.deployment_type)))
  const visibleCandidateTypes = typeFilter === 'all' ? candidateTypes : candidateTypes.filter(type => type === typeFilter)
  const scanHostItems = Object.entries(scanHostProgress)
  const scanPercent = scanProgress.total > 0 ? Math.round((scanProgress.current / scanProgress.total) * 100) : 0
  const hostResources = resources.filter(resource => resource.resource_type === 'host')
  const newCandidateCount = scanCandidates.filter(candidate => candidate.management_status !== 'managed').length
  const managedCandidateCount = scanCandidates.length - newCandidateCount

  const openScan = () => {
    const hostIds = hostResources.map(resource => resource.resource_id)
    setAddMode('scan')
    setScanStage('config')
    setScanAssetIds(hostIds)
    setScanTypes(['docker', 'docker_compose', 'supervisor'])
    setIncludeSystemServices(false)
    setScanCandidates([])
    setSelectedCandidates([])
    setScanErrors([])
  }

  const quickScan = async () => {
    const hostIds = hostResources.map(resource => resource.resource_id)
    if (hostIds.length === 0) {
      message.warning('请先绑定主机资源')
      return
    }
    setAddMode('scan')
    setScanStage('scanning')
    setScanAssetIds(hostIds)
    setScanTypes(['docker', 'docker_compose', 'supervisor'])
    setIncludeSystemServices(false)
    setScanCandidates([])
    setSelectedCandidates([])
    setScanErrors([])
    setScanSkipped({})
    setScanLoading(true)
    setScanProgress({ current: 0, total: hostIds.length, currentHost: '' })
    setScanHostProgress(Object.fromEntries(hostIds.map(id => {
      const hostName = hostResourceMap.get(id) || id
      return [id, { name: hostName, status: 'pending' as ScanHostStatus }]
    })))

    const allCandidates: ApplicationRuntimeDiscoveryCandidate[] = []
    const allErrors: string[] = []
    const skippedEnvs: Record<string, number> = {}

    for (let i = 0; i < hostIds.length; i++) {
      const hostId = hostIds[i]
      const hostName = hostResourceMap.get(hostId) || hostId
      setScanProgress({ current: i + 1, total: hostIds.length, currentHost: hostName })
      setScanHostProgress(prev => ({ ...prev, [hostId]: { name: hostName, status: 'scanning' } }))

      try {
        const result = await applicationsApi.discoverComponents(
          appId,
          [hostId],
          ['docker', 'docker_compose', 'supervisor'],
          false
        )
        allCandidates.push(...result.candidates)
        setScanHostProgress(prev => ({
          ...prev,
          [hostId]: { name: hostName, status: 'success', count: result.candidates.length },
        }))
      } catch (err: unknown) {
        // 环境不支持等场景按"跳过"计数，其余进入失败清单
        const errorMsg = extractAxiosDetail(err) ?? (err instanceof Error ? err.message : undefined) ?? '扫描失败'
        const skippedEnv = getSkippedEnvironment(errorMsg)
        if (skippedEnv) {
          skippedEnvs[skippedEnv] = (skippedEnvs[skippedEnv] || 0) + 1
          setScanHostProgress(prev => ({ ...prev, [hostId]: { name: hostName, status: 'success', count: 0 } }))
        } else {
          allErrors.push(`${hostName}: ${errorMsg}`)
          setScanHostProgress(prev => ({ ...prev, [hostId]: { name: hostName, status: 'failed', errorDetail: errorMsg } }))
        }
      }
    }

    setScanCandidates(allCandidates)
    setScanErrors(allErrors)
    setScanSkipped(skippedEnvs)
    setScanFilteredCount(0)
    setScanLoading(false)
    setScanStage('preview')

    if (allCandidates.length > 0) {
      message.success(`扫描完成，发现 ${allCandidates.filter(c => c.management_status !== 'managed').length} 个新实例`)
    } else if (Object.keys(skippedEnvs).length > 0) {
      message.info('扫描完成，部分主机未安装运行环境')
    } else {
      message.info('扫描完成，未发现新的运行实例')
    }
  }
  const componentTypes = Array.from(new Set(components.map(component => component.deployment_type)))
  const typeFilteredComponents = componentTypeFilter === 'all'
    ? components
    : components.filter(component => component.deployment_type === componentTypeFilter)
  const normalizedComponentSearch = componentSearch.trim().toLowerCase()
  const visibleComponents = typeFilteredComponents.filter(component => {
    if (!normalizedComponentSearch) return true
    const searchable = [
      component.name,
      component.control_ref,
      component.description,
      componentHostLabel(component),
      ...componentHostIds(component),
    ].join(' ').toLowerCase()
    return searchable.includes(normalizedComponentSearch)
  })
  const componentsByHost = Array.from(visibleComponents.reduce((groups, component) => {
    const hostIds = componentHostIds(component)
    const groupKey = hostIds.length === 1 ? hostIds[0] : hostIds.length > 1 ? `multi:${hostIds.join(',')}` : 'unbound'
    const groupName = hostIds.length === 1
      ? hostResourceMap.get(hostIds[0]) || hostIds[0]
      : hostIds.length > 1 ? `多主机 (${hostIds.length})` : '未绑定主机'
    const current = groups.get(groupKey) || { key: groupKey, name: groupName, hostIds, components: [] as ApplicationComponentResponse[] }
    current.components.push(component)
    groups.set(groupKey, current)
    return groups
  }, new Map<string, { key: string; name: string; hostIds: string[]; components: ApplicationComponentResponse[] }>()).values())
    .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))
  const componentsByProject = Array.from(visibleComponents.reduce((groups, component) => {
    const projectName = componentProjectLabel(component)
    if (!projectName) return groups
    const current = groups.get(projectName) || { key: projectName, name: projectName, components: [] as ApplicationComponentResponse[] }
    current.components.push(component)
    groups.set(projectName, current)
    return groups
  }, new Map<string, { key: string; name: string; components: ApplicationComponentResponse[] }>()).values())
    .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'))

  const confirmBatchDelete = (componentIds: string[]) => {
    if (componentIds.length === 0) { message.warning('请先选择要移除的部署组件'); return }
    modal.confirm({
      title: `移除选中的 ${componentIds.length} 个部署组件？`,
      content: '仅删除 OpsPilot 中的登记记录，不会停止主机上的实际进程。此操作不可撤销。',
      okText: '确认移除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => batchDeleteMut.mutateAsync(componentIds),
    })
  }

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between" align="center" wrap gap={8}>
        <Space wrap>
          <span style={{ color: '#64748b', fontSize: 13 }}>共 {components.length} 个部署组件</span>
          <Input
            allowClear
            size="small"
            prefix={<SearchOutlined />}
            value={componentSearch}
            onChange={event => { setComponentSearch(event.target.value); setSelectedComponentIds([]) }}
            placeholder="搜索实例、标识或主机"
            style={{ width: 220 }}
          />
          <Select
            size="small"
            value={componentTypeFilter}
            style={{ width: 160 }}
            onChange={(value) => { setComponentTypeFilter(value); setSelectedComponentIds([]) }}
            options={[{ value: 'all', label: `全部类型 (${components.length})` }, ...componentTypes.map(type => ({
              value: type,
              label: `${DEPLOY_TYPES.find(item => item.value === type)?.label || type} (${components.filter(component => component.deployment_type === type).length})`,
            }))]}
          />
          <Segmented
            size="small"
            value={componentView}
            onChange={value => setComponentView(value as 'flat' | 'host' | 'project')}
            options={[{ value: 'flat', label: '平铺' }, { value: 'host', label: '按主机' }, { value: 'project', label: '按 Compose 项目' }]}
          />
          {selectedComponentIds.length > 0 && (
            <Button danger size="small" icon={<DeleteOutlined />} loading={batchDeleteMut.isPending} onClick={() => confirmBatchDelete(selectedComponentIds)}>
              批量移除 ({selectedComponentIds.length})
            </Button>
          )}
          {visibleComponents.length > 0 && selectedComponentIds.length !== visibleComponents.length && (
            <Button size="small" type="link" onClick={() => setSelectedComponentIds(visibleComponents.map(component => component.component_id))}>
              选择当前类型全部 {visibleComponents.length} 个
            </Button>
          )}
          {selectedComponentIds.length > 0 && (
            <Button size="small" type="link" onClick={() => setSelectedComponentIds([])}>取消选择</Button>
          )}
        </Space>
        <Space>
          <Tooltip title={autoRefresh ? '每 30 秒自动刷新组件状态' : '开启后自动刷新组件状态'}>
            <Space size={4}>
              <span style={{ fontSize: 13, color: '#8c8c8c' }}>自动刷新</span>
              <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
            </Space>
          </Tooltip>
          <Button icon={<ReloadOutlined />} loading={scanLoading} disabled={scanLoading} onClick={quickScan}>
            快速扫描
          </Button>
          <Button icon={<SearchOutlined />} loading={scanLoading} disabled={scanLoading} onClick={openScan}>自定义扫描</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setAddMode('manual'); setComposePreflight(null); manualForm.resetFields() }}>添加组件</Button>
        </Space>
      </Flex>

      {addMode === 'scan' && (
        <Card size="small" title="扫描主机存量实例" extra={<Button size="small" onClick={() => setAddMode(null)}>关闭</Button>}>
          <Flex vertical gap={16}>
            <Steps size="small" current={scanStage === 'config' ? 0 : scanStage === 'scanning' ? 1 : 2} items={[{ title: '配置范围' }, { title: '执行扫描' }, { title: '预览并纳管' }]} />
            {scanStage === 'config' ? (
              <Flex vertical gap={16}>
                <Form layout="vertical">
                  <Form.Item label="扫描主机" required>
                    <Select
                      mode="multiple"
                      value={scanAssetIds}
                      onChange={setScanAssetIds}
                      placeholder="请选择应用已绑定的主机"
                      options={hostResources.map(host => ({ value: host.resource_id, label: host.resource_name || host.resource_id }))}
                    />
                  </Form.Item>
                  <Form.Item label="运行环境" required>
                    <Checkbox.Group value={scanTypes} onChange={values => setScanTypes(values as string[])} options={DISCOVERY_TYPES} />
                  </Form.Item>
                  {scanTypes.includes('systemd') && (
                    <Alert
                      type="warning"
                      showIcon
                      message="Systemd 可能发现大量系统服务"
                      description={<Flex justify="space-between" align="center"><span>默认过滤基础系统服务；仅在确有需要时显示。</span><Switch size="small" checked={includeSystemServices} onChange={setIncludeSystemServices} checkedChildren="显示" unCheckedChildren="过滤" /></Flex>}
                    />
                  )}
                </Form>
                <Flex justify="flex-end"><Button type="primary" disabled={scanAssetIds.length === 0 || scanTypes.length === 0} onClick={handleScan}>开始扫描</Button></Flex>
              </Flex>
            ) : scanStage === 'scanning' ? (
              <Flex vertical gap={12}>
                <div>
                  <Flex justify="space-between">
                    <span>正在扫描 {scanProgress.currentHost || '绑定主机'}</span>
                    <span style={{ color: '#8c8c8c' }}>{scanProgress.current}/{scanProgress.total}</span>
                  </Flex>
                  <Progress percent={scanPercent} showInfo={false} size="small" />
                </div>
                <div style={{ maxHeight: 280, overflowY: 'auto' }}>
                  {scanHostItems.map(([hostId, host]) => (
                    <Flex key={hostId} justify="space-between" align="center" style={{ minHeight: 32 }}>
                      <Space>
                        <Badge status={host.status === 'success' ? 'success' : host.status === 'failed' ? 'error' : host.status === 'scanning' ? 'processing' : 'default'} />
                        <span>{host.name}</span>
                      </Space>
                      {host.status === 'failed' && host.errorDetail ? (
                        <Tooltip title={host.errorDetail}>
                          <span style={{ color: '#ff4d4f', fontSize: 12, cursor: 'help' }}>
                            未完成 <QuestionCircleOutlined />
                          </span>
                        </Tooltip>
                      ) : (
                        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                          {host.status === 'success' ? `发现 ${host.count || 0} 个` : host.status === 'failed' ? '未完成' : host.status === 'scanning' ? '扫描中' : '等待中'}
                        </span>
                      )}
                    </Flex>
                  ))}
                </div>
              </Flex>
            ) : (
              <Flex vertical gap={12}>
                {Object.keys(scanSkipped).length > 0 && (
                  <Alert type="info" showIcon message="已自动跳过未安装的运行环境" description={Object.entries(scanSkipped).map(([name, count]) => `${name} ${count} 项`).join('，')} />
                )}
                <Alert type="info" showIcon message={`发现 ${newCandidateCount} 个新实例，${managedCandidateCount} 个已纳管`} description={scanFilteredCount > 0 ? `已过滤 ${scanFilteredCount} 个基础系统服务` : undefined} />
                {scanErrors.length > 0 && (
                  <Collapse size="small" items={[{
                    key: 'errors',
                    label: `${scanErrors.length} 项扫描未完成`,
                    children: <ul style={{ margin: 0, paddingLeft: 20 }}>{scanErrors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul>,
                  }]} />
                )}
                {scanCandidates.length > 0 ? (
                  <>
                    <Flex justify="space-between" align="center" wrap gap={8}>
                      <Space wrap>
                        <Select
                          size="small"
                          value={typeFilter}
                          style={{ width: 160 }}
                          onChange={setTypeFilter}
                          options={[{ value: 'all', label: `全部类型 (${scanCandidates.length})` }, ...candidateTypes.map(type => ({
                            value: type,
                            label: `${DEPLOY_TYPES.find(item => item.value === type)?.label || type} (${scanCandidates.filter(candidate => candidate.deployment_type === type).length})`,
                          }))]}
                        />
                        <Button size="small" type="link" onClick={() => setSelectedCandidates(prev => Array.from(new Set([
                          ...prev,
                          ...scanCandidates.filter(candidate => visibleCandidateTypes.includes(candidate.deployment_type) && candidate.management_status !== 'managed').map(candidate => `${candidate.asset_id}:${candidate.control_ref}`),
                        ])))}>全选当前结果</Button>
                      </Space>
                      <Button type="primary" size="small" loading={importLoading} disabled={selectedCandidates.length === 0 || importLoading} onClick={handleImportSelected}>
                        纳管选中 {selectedCandidates.length > 0 ? `(${selectedCandidates.length})` : ''}
                      </Button>
                    </Flex>
                    <Collapse
                      defaultActiveKey={visibleCandidateTypes.slice(0, 1)}
                      items={visibleCandidateTypes.map(type => {
                        const candidates = scanCandidates.filter(candidate => candidate.deployment_type === type)
                        const label = DEPLOY_TYPES.find(item => item.value === type)?.label || type
                        const selectedCount = candidates.filter(candidate => selectedCandidates.includes(`${candidate.asset_id}:${candidate.control_ref}`)).length
                        return {
                          key: type,
                          label: <Space><span>{label}</span><Badge count={candidates.length} showZero color="#8c8c8c" /><span style={{ color: '#8c8c8c', fontSize: 12 }}>已选 {selectedCount}</span></Space>,
                          children: <Table size="small" rowKey={(r) => `${r.asset_id}:${r.control_ref}`} rowClassName={r => r.management_status === 'managed' ? 'ant-table-row-disabled' : ''} dataSource={candidates} columns={scanCandidateColumns} pagination={candidates.length > 5 ? { pageSize: 5, size: 'small', showSizeChanger: false } : false} />,
                        }
                      })}
                    />
                  </>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未发现可纳管的部署组件">
                    <Button size="small" onClick={() => setScanStage('config')}>调整范围</Button>
                  </Empty>
                )}
              </Flex>
            )}
          </Flex>
        </Card>
      )}

      {addMode === 'manual' && (
        <Card size="small" title="添加部署组件" extra={<Button size="small" onClick={() => setAddMode(null)}>关闭</Button>}>
          <Form form={manualForm} layout="vertical" style={{ maxWidth: 760 }} onValuesChange={() => setComposePreflight(null)}>
            <Flex gap={16}>
              <Form.Item name="name" label="实例名称" rules={[{ required: true }]} style={{ flex: 1 }}>
                <Input placeholder="例如 order-api" />
              </Form.Item>
              <Form.Item name="deployment_type" label="部署方式" rules={[{ required: true }]} extra="Systemd、Supervisor、External 目前只支持纳管已有服务" style={{ flex: 1 }} initialValue="systemd">
                <Select options={DEPLOY_TYPES} />
              </Form.Item>
            </Flex>
            <Form.Item noStyle shouldUpdate={(prev, cur) => prev.create_mode !== cur.create_mode || prev.deployment_type !== cur.deployment_type}>
              {({ getFieldValue }) => getFieldValue('create_mode') === 'deploy' && getFieldValue('deployment_type') === 'docker_compose' ? null : (
                <Form.Item name="control_ref" label="控制标识" rules={[{ required: true }]}
                  help="Systemd 填服务名；Docker 填容器名；Compose 纳管填 compose=文件路径;service=服务名">
                  <Input placeholder="服务名 / 容器名 / compose=..." />
                </Form.Item>
              )}
            </Form.Item>
            <Flex gap={16}>
              <Form.Item name="env" label="环境" style={{ flex: 1 }} initialValue="prod">
                <Select options={[{ value: 'prod', label: '生产' }, { value: 'test', label: '测试' }, { value: 'dev', label: '开发' }]} />
              </Form.Item>
              <Form.Item noStyle shouldUpdate={(prev, cur) => prev.deployment_type !== cur.deployment_type}>
                {({ getFieldValue }) => {
                  const type = getFieldValue('deployment_type')
                  if (['docker', 'docker_compose'].includes(type)) {
                    return (
                      <Form.Item name="create_mode" label="添加模式" style={{ flex: 1 }} initialValue="manage">
                        <Select options={[{ value: 'manage', label: '纳管已有服务' }, { value: 'deploy', label: '全新部署' }]} />
                      </Form.Item>
                    )
                  }
                  return null
                }}
              </Form.Item>
            </Flex>
            <Form.Item shouldUpdate={(prev, cur) => prev.create_mode !== cur.create_mode || prev.deployment_type !== cur.deployment_type}>
              {({ getFieldValue }) => {
                const mode = getFieldValue('create_mode')
                const type = getFieldValue('deployment_type')
                if (mode === 'deploy' && ['docker', 'docker_compose'].includes(type)) {
                  return (
                    <Flex vertical gap={12}>
                      <Form.Item name="target_resource_id" label="目标主机" rules={[{ required: true, message: '请选择目标主机' }]}>
                        <Select placeholder="请选择应用已绑定主机" options={hostResources.map(host => ({ value: host.resource_id, label: host.resource_name || host.resource_id }))} />
                      </Form.Item>
                      {type === 'docker' ? (
                        <>
                          <Form.Item name="control_ref" label="容器名称" rules={[{ required: true }]}><Input placeholder="例如 order-api" /></Form.Item>
                          <Form.Item name={['deploy_config', 'image_ref']} label="Docker 镜像" rules={[{ required: true }]}><Input placeholder="nginx:alpine" /></Form.Item>
                          <Form.Item name={['deploy_config', 'docker_run_args']} label="运行参数"><Input placeholder="例如 -p 18080:80 --restart unless-stopped" /></Form.Item>
                        </>
                      ) : (
                        <>
                          <Alert type="info" showIcon message="整个 Compose 项目将作为一次部署，成功后可通过组件发现纳管项目内服务。" />
                          <Flex gap={12}>
                            <Form.Item name="compose_project_name" label="Compose 项目名" rules={[{ required: true }]} style={{ flex: 1 }}><Input placeholder="opspilot-compose-demo" /></Form.Item>
                            <Form.Item
                              name="compose_deploy_dir"
                              label="部署目录"
                              rules={[{ required: true }]}
                              extra="全新部署会创建此目录；目录必须为空或尚不存在，已有文件时会拒绝部署，不会覆盖原内容。"
                              style={{ flex: 1 }}
                            >
                              <Input placeholder="/opt/opspilot/test/demo" />
                            </Form.Item>
                          </Flex>
                          <Flex gap={12}>
                            <Form.Item name="compose_filename" label="Compose 文件名" initialValue="docker-compose.yml" style={{ flex: 1 }}><Input /></Form.Item>
                            <Form.Item name="compose_env_filename" label="环境变量文件" initialValue=".env" style={{ flex: 1 }}><Input /></Form.Item>
                          </Flex>
                          <Form.Item name="compose_content" label="Compose 文件内容" rules={[{ required: true }]}>
                            <Input.TextArea rows={8} placeholder={'services:\n  web:\n    image: nginx:alpine\n    ports:\n      - "18080:80"'} />
                          </Form.Item>
                          <Form.Item name="compose_env_content" label=".env 内容（可选）"><Input.TextArea rows={3} placeholder="KEY=value" /></Form.Item>
                          <Form.Item name="pull_image" valuePropName="checked" initialValue><Checkbox>部署前拉取镜像</Checkbox></Form.Item>
                          <Flex justify="space-between" align="center">
                            <Button icon={<SafetyCertificateOutlined />} loading={composePreflightMut.isPending} onClick={handleComposePreflight}>部署前检查</Button>
                            {composePreflight && <Tag color={composePreflight.ok ? 'success' : 'error'}>{composePreflight.ok ? '检查通过' : '检查未通过'}</Tag>}
                          </Flex>
                          {composePreflight && <Collapse size="small" items={[{ key: 'preflight', label: '检查结果', children: <Flex vertical gap={8}>{composePreflight.checks.map(check => <Flex key={check.key} justify="space-between"><Badge status={check.status === 'success' ? 'success' : check.status === 'warning' ? 'warning' : 'error'} text={check.label} /><span style={{ color: '#64748b' }}>{check.message}</span></Flex>)}</Flex> }]} />}
                        </>
                      )}
                    </Flex>
                  )
                }
                if (mode === 'deploy' && type === 'docker') {
                  return (
                    <Form.Item name={['deploy_config', 'image_ref']} label="Docker 镜像" rules={[{ required: true }]}>
                      <Input placeholder="nginx:latest" />
                    </Form.Item>
                  )
                }
                return null
              }}
            </Form.Item>
            <Form.Item name="description" label="备注"><Input placeholder="可选" /></Form.Item>
            <Flex justify="flex-end" gap={8}>
              <Button onClick={() => setAddMode(null)}>取消</Button>
              <Button type="primary" loading={createMut.isPending}
                onClick={handleComponentSubmit}>
                {manualForm.getFieldValue('create_mode') === 'deploy' ? '开始部署' : '保存'}
              </Button>
            </Flex>
          </Form>
        </Card>
      )}

      {componentView === 'flat' ? (
        <Table<ApplicationComponentResponse>
          rowKey="component_id" size="middle"
          dataSource={visibleComponents} columns={componentColumns}
          rowSelection={{
            selectedRowKeys: selectedComponentIds,
            onChange: (keys) => setSelectedComponentIds(keys as string[]),
            preserveSelectedRowKeys: true,
          }}
          onRow={(record) => ({
            onClick: (e) => {
              // 不处理操作列的点击
              if ((e.target as HTMLElement).closest('.ant-btn, .ant-dropdown, .ant-popconfirm')) return
              onOpenDetail(record)
            },
            style: { cursor: 'pointer' },
          })}
          scroll={{ x: 1060 }}
          pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100], showTotal: total => `共 ${total} 条` }}
          locale={{ emptyText: normalizedComponentSearch ? '没有匹配的部署组件' : '暂无部署组件，点击「发现组件」或「添加组件」' }}
        />
      ) : componentView === 'host' && componentsByHost.length > 0 ? (
        <Collapse
          defaultActiveKey={componentsByHost.slice(0, 1).map(group => group.key)}
          items={componentsByHost.map(group => ({
            key: group.key,
            label: <Space><DesktopOutlined /><span>{group.name}</span><Badge count={group.components.length} showZero color="#8c8c8c" />{group.hostIds.length === 1 && <span style={{ color: '#8c8c8c', fontSize: 12 }}>{group.hostIds[0]}</span>}</Space>,
            children: <Table<ApplicationComponentResponse>
              rowKey="component_id"
              size="small"
              dataSource={group.components}
              columns={componentColumns.filter(column => column.key !== 'host')}
              rowSelection={{ selectedRowKeys: selectedComponentIds, onChange: keys => setSelectedComponentIds(keys as string[]), preserveSelectedRowKeys: true }}
              onRow={(record) => ({
                onClick: (e) => {
                  if ((e.target as HTMLElement).closest('.ant-btn, .ant-dropdown, .ant-popconfirm')) return
                  onOpenDetail(record)
                },
                style: { cursor: 'pointer' },
              })}
              scroll={{ x: 900 }}
              pagination={group.components.length > 10 ? { pageSize: 10, size: 'small', showSizeChanger: false } : false}
            />,
          }))}
        />
      ) : componentView === 'project' && componentsByProject.length > 0 ? (
        <Collapse
          defaultActiveKey={componentsByProject.slice(0, 1).map(group => group.key)}
          items={componentsByProject.map(group => ({
            key: group.key,
            label: <Space><AppstoreOutlined /><span>{group.name}</span><Badge count={group.components.length} showZero color="#8c8c8c" /></Space>,
            children: <Table<ApplicationComponentResponse>
              rowKey="component_id"
              size="small"
              dataSource={group.components}
              columns={componentColumns.filter(column => column.key !== 'host')}
              rowSelection={{ selectedRowKeys: selectedComponentIds, onChange: keys => setSelectedComponentIds(keys as string[]), preserveSelectedRowKeys: true }}
              onRow={(record) => ({
                onClick: (e) => {
                  if ((e.target as HTMLElement).closest('.ant-btn, .ant-dropdown, .ant-popconfirm')) return
                  onOpenDetail(record)
                },
                style: { cursor: 'pointer' },
              })}
              scroll={{ x: 900 }}
              pagination={group.components.length > 10 ? { pageSize: 10, size: 'small', showSizeChanger: false } : false}
            />,
          }))}
        />
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的部署组件" />
      )}
    </Flex>
  )
}
