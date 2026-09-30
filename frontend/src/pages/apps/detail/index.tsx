import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, useEffect } from 'react'
import {
  Tabs, Badge, Tag, Flex, Button,
  Skeleton, Alert, Form, Space,
} from 'antd'
import { App } from 'antd'
import {
  AppstoreOutlined, LinkOutlined,
  ArrowLeftOutlined, EditOutlined,
} from '@ant-design/icons'
import { applicationsApi } from '@/api'
import client from '@/api/client'
import { extractAxiosDetail } from '@/hooks/usePermission'
import ComponentDetailDrawer from '@/components/ComponentDetailDrawer'
import DeploymentProgressModal from '@/components/DeploymentProgressModal'
import type {
  ApplicationComponentResponse,
  ApplicationPayload,
} from '@/types/api'
import {
  ENV_COLOR, ENV_LABEL, STATUS_COLOR, STATUS_LABEL, HEALTH_COLOR,
  RESOURCE_TYPE_LABEL, controlActions,
} from './constants'
import type { EditAppFormValues, AppGroupItem, AppGroupMembership } from './types'
import OverviewPanel from './OverviewPanel'
import ResourcesPanel from './ResourcesPanel'
import ComponentsPanel from './ComponentsPanel'
import DependenciesPanel from './DependenciesPanel'
import LinksPanel from './LinksPanel'
import ActionOutputModal from './ActionOutputModal'
import BindResourceDrawer from './BindResourceDrawer'
import EditAppDrawer from './EditAppDrawer'

export default function AppDetailPage() {
  const { appId } = useParams<{ appId: string }>()
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  const [detailDrawerOpen, setDetailDrawerOpen]       = useState(false)
  const [selectedComponent, setSelectedComponent]     = useState<ApplicationComponentResponse | null>(null)
  const [deploymentTaskId, setDeploymentTaskId]       = useState<string | null>(null)
  const [deployProgressOpen, setDeployProgressOpen]   = useState(false)
  const [editAppDrawer, setEditAppDrawer]             = useState(false)
  const [editAppForm]                                 = Form.useForm<EditAppFormValues>()
  const [actionOutput, setActionOutput]             = useState<{ title: string; response: import('@/types/api').ApplicationRuntimeActionResponse } | null>(null)
  const [autoRefresh, setAutoRefresh]               = useState(false)
  const [bindResourceDrawer, setBindResourceDrawer] = useState(false)
  const [bindForm]                                  = Form.useForm<{ selectedValues: string[] }>()
  const [activeTab, setActiveTab]                   = useState('overview')

  const { data: detail, isLoading: detailLoading, error: detailError } = useQuery({
    queryKey: ['app-detail', appId],
    queryFn: () => applicationsApi.getDetail(appId!),
    enabled: !!appId,
    refetchInterval: autoRefresh ? 30000 : false,
  })
  const { data: overview, isLoading: overviewLoading } = useQuery({
    queryKey: ['app-overview', appId],
    queryFn: () => applicationsApi.getOverview(appId!),
    enabled: !!appId,
    refetchInterval: autoRefresh ? 30000 : false,
  })
  const { data: dependencies } = useQuery({
    queryKey: ['app-dependencies', appId],
    queryFn: () => applicationsApi.getDependencies(appId!),
    enabled: !!appId,
  })

  const { data: allHosts = [] } = useQuery({
    queryKey: ['assets-all'],
    queryFn: () => import('@/api').then(m => m.assetsApi.list()),
  })

  const { data: allVMs = [] } = useQuery({
    queryKey: ['vms-all'],
    queryFn: () => import('@/api').then(m => m.vmsApi.list()),
  })

  const { data: allDomains = [] } = useQuery({
    queryKey: ['domains-all'],
    queryFn: () => import('@/api').then(m => m.domainsApi.list()),
  })

  const { data: allCertificates = [] } = useQuery({
    queryKey: ['certificates-all'],
    queryFn: () => import('@/api').then(m => m.certificatesApi.list()),
  })

  const { data: groups = [] } = useQuery({
    queryKey: ['resource-groups', 'application'],
    queryFn: () =>
      client.get<AppGroupItem[]>('/groups', { params: { resource_type: 'application' } })
        .then((res) => res.data)
        .catch(() => [] as AppGroupItem[]),
  })

  const { data: appGroupMembership = [] } = useQuery({
    queryKey: ['app-group-membership', appId],
    queryFn: () =>
      client.get<AppGroupMembership[]>('/groups/members', {
        params: { resource_type: 'application', resource_id: appId },
      })
        .then((res) => res.data)
        .catch(() => [] as AppGroupMembership[]),
    enabled: !!appId,
  })

  useEffect(() => {
    if (autoRefresh) {
      message.info('已开启自动刷新，每 30 秒更新组件状态')
    }
  }, [autoRefresh, message])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['app-detail', appId] })
    queryClient.invalidateQueries({ queryKey: ['app-overview', appId] })
  }

  const controlMut = useMutation({
    mutationFn: ({ componentId, action }: { componentId: string; action: string }) =>
      applicationsApi.controlComponent(appId!, componentId, action),
    onSuccess: (res, vars) => {
      invalidate()
      setActionOutput({ title: `${vars.action === 'status' ? '刷新状态' : '执行操作'}：${vars.action}`, response: res })
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '控制操作失败'),
  })

  const deleteMut = useMutation({
    mutationFn: (componentId: string) => applicationsApi.deleteComponent(appId!, componentId),
    onSuccess: () => { invalidate(); message.success('部署组件已移除') },
    onError: () => message.error('删除失败'),
  })

  const bindResourcesMut = useMutation({
    mutationFn: (resources: Array<{ resource_type: string; resource_id: string }>) =>
      applicationsApi.bindResources(appId!, resources),
    onSuccess: () => {
      invalidate()
      setBindResourceDrawer(false)
      bindForm.resetFields()
      message.success('资源绑定已更新')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '资源绑定失败'),
  })

  const updateAppMut = useMutation({
    mutationFn: (data: ApplicationPayload) => applicationsApi.update(appId!, data),
    onSuccess: () => {
      invalidate()
      setEditAppDrawer(false)
      editAppForm.resetFields()
      message.success('应用信息已更新')
    },
    onError: (err: unknown) => message.error(extractAxiosDetail(err) ?? '更新失败'),
  })

  if (detailLoading) return <Skeleton active paragraph={{ rows: 8 }} style={{ padding: 24 }} />
  if (detailError || !detail) {
    return <Alert type="error" message="应用详情加载失败" description="请检查应用 ID 是否正确或刷新重试" showIcon />
  }

  const { app, resources, components, links } = detail

  // ── 资源绑定 ──
  const openBindResourceDrawer = () => {
    const existingResources = resources.map(r => 
      JSON.stringify({ resource_type: r.resource_type, resource_id: r.resource_id })
    )
    bindForm.setFieldsValue({ selectedValues: existingResources })
    setBindResourceDrawer(true)
  }

  const handleBindResources = async () => {
    const values = await bindForm.validateFields()
    const resources = (values.selectedValues || []).map((v: string) => JSON.parse(v))
    bindResourcesMut.mutate(resources)
  }

  const handleUnbindResource = (resourceType: string, resourceId: string) => {
    modal.confirm({
      title: '解绑资源',
      content: `确认解绑 ${RESOURCE_TYPE_LABEL[resourceType]?.label || resourceType} "${resourceId}"？`,
      okText: '确认解绑',
      okButtonProps: { danger: true },
      onOk: () => {
        const updatedResources = resources
          .filter(r => !(r.resource_type === resourceType && r.resource_id === resourceId))
          .map(r => ({ resource_type: r.resource_type, resource_id: r.resource_id }))
        bindResourcesMut.mutate(updatedResources)
      },
    })
  }

  const availableResourceOptions = [
    ...allHosts.map(h => ({ type: 'host', id: h.id, name: h.hostname || h.ip })),
    ...allVMs.map(v => ({ type: 'vm', id: v.id, name: v.name })),
    ...allDomains.map(d => ({ type: 'domain', id: String(d.id), name: d.domain })),
    ...allCertificates.map(c => ({ type: 'certificate', id: c.cert_id, name: c.primary_domain })),
  ]

  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      <Flex align="center" gap={12}>
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/apps')}
          style={{ marginRight: 8 }}
        >
          返回
        </Button>
        <AppstoreOutlined style={{ fontSize: 22, color: '#1677ff' }} />
        <Flex vertical gap={2}>
          <span style={{ fontSize: 18, fontWeight: 600 }}>{app.name}</span>
          <Space size={8}>
            <Tag color={ENV_COLOR[app.environment] || 'default'}>{ENV_LABEL[app.environment] || app.environment}</Tag>
            <Badge status={STATUS_COLOR[app.status]} text={STATUS_LABEL[app.status] || app.status} />
            <Badge status={HEALTH_COLOR[app.health_status]} text={app.health_label} />
          </Space>
        </Flex>
        <Space style={{ marginLeft: 'auto' }}>
          <Button
            icon={<EditOutlined />}
            onClick={() => {
              const currentGroupId = appGroupMembership.length > 0 ? appGroupMembership[0].group_id : null
              editAppForm.setFieldsValue({
                name: app.name,
                category: app.category,
                environment: app.environment,
                status: app.status,
                owner: app.owner,
                description: app.description,
                entry_url: app.entry_url,
                group_id: currentGroupId,
              })
              setEditAppDrawer(true)
            }}
          >
            编辑
          </Button>
          {app.entry_url && <Button icon={<LinkOutlined />} href={app.entry_url} target="_blank">打开应用</Button>}
        </Space>
      </Flex>

      <Tabs activeKey={activeTab} onChange={setActiveTab} style={{ flex: 1 }} items={[
        {
          key: 'overview', label: '总览',
          children: (
            <OverviewPanel
              app={app}
              overview={overview}
              overviewLoading={overviewLoading}
              componentCount={components.length}
              setActiveTab={setActiveTab}
            />
          ),
        },
        {
          key: 'resources', label: `资源绑定 (${resources.length})`,
          children: (
            <ResourcesPanel
              resources={resources}
              onOpenBindDrawer={openBindResourceDrawer}
              onUnbindResource={handleUnbindResource}
            />
          ),
        },
        {
          key: 'components', label: `部署组件 (${components.length})`,
          children: (
            <ComponentsPanel
              appId={appId!}
              resources={resources}
              components={components}
              autoRefresh={autoRefresh}
              setAutoRefresh={setAutoRefresh}
              onControl={(componentId, action) => controlMut.mutate({ componentId, action })}
              controlPendingComponentId={controlMut.isPending ? controlMut.variables?.componentId : undefined}
              onDelete={(componentId) => deleteMut.mutate(componentId)}
              onOpenDetail={(component) => { setSelectedComponent(component); setDetailDrawerOpen(true) }}
              onDeployStarted={(taskId) => { setDeploymentTaskId(taskId); setDeployProgressOpen(true) }}
              invalidate={invalidate}
            />
          ),
        },
        {
          key: 'dependencies', label: '依赖关系',
          children: <DependenciesPanel dependencies={dependencies} />,
        },
        {
          key: 'links', label: `快捷链接 (${links.length})`,
          children: <LinksPanel links={links} />,
        },
      ]} />

      <ActionOutputModal actionOutput={actionOutput} onClose={() => setActionOutput(null)} />

      <BindResourceDrawer
        open={bindResourceDrawer}
        onClose={() => setBindResourceDrawer(false)}
        bindForm={bindForm}
        availableResourceOptions={availableResourceOptions}
        onSave={handleBindResources}
        saving={bindResourcesMut.isPending}
      />

      {/* 部署组件详情抽屉 */}
      <ComponentDetailDrawer
        open={detailDrawerOpen}
        onClose={() => setDetailDrawerOpen(false)}
        component={selectedComponent}
        appId={appId!}
        onAction={(componentId, action) => {
          modal.confirm({
            title: `对「${selectedComponent?.name}」执行 ${controlActions.find(a => a.key === action)?.label}？`,
            okText: '执行',
            onOk: () => {
              controlMut.mutate({ componentId, action })
              setDetailDrawerOpen(false)
            },
          })
        }}
        onDelete={(componentId) => {
          modal.confirm({
            title: '移除该部署组件？',
            content: '仅移除登记记录，不会停止实际进程',
            okText: '移除',
            cancelText: '取消',
            onOk: () => {
              deleteMut.mutate(componentId)
              setDetailDrawerOpen(false)
            },
          })
        }}
      />

      {/* 编辑应用信息抽屉 */}
      <EditAppDrawer
        open={editAppDrawer}
        onClose={() => setEditAppDrawer(false)}
        editAppForm={editAppForm}
        groups={groups}
        saving={updateAppMut.isPending}
        onFinish={(values) => {
          const { group_id, ...appData } = values
          // 先更新应用信息
          updateAppMut.mutate(appData)
          // 如果分组发生变化，更新分组关系
          if (group_id !== undefined) {
            const currentGroupId = appGroupMembership.length > 0 ? appGroupMembership[0].group_id : null
            if (group_id !== currentGroupId) {
              // 分组发生了变化，需要更新
              if (group_id === null) {
                // 移出分组
                void client.post('/groups/members', {
                  action: 'remove',
                  resource_type: 'application',
                  resource_ids: [appId],
                })
              } else {
                // 分配到新分组
                void client.post(`/groups/${group_id}/assign`, {
                  resource_type: 'application',
                  resource_ids: [appId],
                }).then(() => {
                  queryClient.invalidateQueries({ queryKey: ['app-group-membership', appId] })
                })
              }
            }
          }
        }}
      />

      {/* 部署进度模态框 */}
      <DeploymentProgressModal
        taskId={deploymentTaskId}
        open={deployProgressOpen}
        onClose={() => {
          setDeployProgressOpen(false)
          setDeploymentTaskId(null)
        }}
        onSuccess={() => {
          invalidate()
          message.success('部署完成！')
        }}
      />
    </Flex>
  )
}
