/**
 * 脚本执行页（/tools/script）。
 *
 * 基于 Ansible 的批量运维执行台，四个 Tab：
 * 1. 执行：目标主机多选 + 三种模式（ad-hoc 模块 / Playbook / 从模板），
 *    执行走 useRiskConfirm 高危二次确认，结果展示 stdout/stderr/耗时
 * 2. 批量模板：模板 CRUD + 一键执行（模板绑定目标或运行时覆盖）
 * 3. 执行历史：全部人执行记录 + 详情抽屉（命令预览/输出）
 * 4. Ansible 配置：开关 / 可执行文件 / 并发 / 超时 / 主机密钥校验 + 可用性探测
 *
 * 目标列表自带 ansible_ready 与 auth_notice：未纳管凭据的主机会被后端拒绝，
 * 前端在选人阶段即置灰提示，避免执行后才失败。
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Tabs, Card, Table, Button, Space, Tag, Typography, Flex, Input, Select, Switch, InputNumber,
  Drawer, Form, App, Alert, Tooltip, Badge, Descriptions, Popconfirm, Checkbox, Empty,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlayCircleOutlined, ReloadOutlined, PlusOutlined, HistoryOutlined, SettingOutlined,
  CheckCircleOutlined, CloseCircleOutlined, CodeOutlined, FileTextOutlined,
} from '@ant-design/icons'
import { toolsApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import type {
  AnsibleHistoryEntry, AnsibleRunResult, AnsibleTarget, AnsibleTemplateItem, AnsibleTemplatePayload,
} from '@/types/api'

const { Text } = Typography
const { TextArea } = Input

type RunMode = 'module' | 'playbook' | 'template'

/** ad-hoc 模块速选（参数留空由用户补）。 */
const MODULE_PRESETS = ['ping', 'shell', 'command', 'copy', 'setup', 'service', 'systemd', 'reboot']

function ResultPanel({ result }: { result: AnsibleRunResult }) {
  return (
    <Card
      size="small"
      title={
        <Space>
          {result.ok
            ? <Tag color="green" icon={<CheckCircleOutlined />}>成功</Tag>
            : <Tag color="red" icon={<CloseCircleOutlined />}>失败（exit {result.code}）</Tag>}
          <Text type="secondary">
            目标 {result.target_count} 台 · 耗时 {(result.duration_ms / 1000).toFixed(1)}s · run_id {result.run_id.slice(0, 8)}
          </Text>
        </Space>
      }
      style={{ marginTop: 16 }}
    >
      {result.notice && <Alert type="info" showIcon message={result.notice} style={{ marginBottom: 8 }} />}
      {result.command_preview && (
        <Text code style={{ display: 'block', marginBottom: 8, fontSize: 12 }}>{result.command_preview}</Text>
      )}
      {result.stdout && (
        <details open>
          <summary><Text strong>stdout</Text></summary>
          <pre style={{
            background: '#fafafa', padding: 8, borderRadius: 4, maxHeight: 260,
            overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap',
          }}>{result.stdout}</pre>
        </details>
      )}
      {result.stderr && (
        <details open>
          <summary><Text type="danger" strong>stderr</Text></summary>
          <pre style={{
            background: '#fff1f0', padding: 8, borderRadius: 4, maxHeight: 200,
            overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap',
          }}>{result.stderr}</pre>
        </details>
      )}
    </Card>
  )
}

export default function ScriptExecutePage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.tools.scripts.manage')
  const canExecute = has('action.tools.scripts.execute')

  // ── 执行 Tab 状态 ──
  const [runMode, setRunMode] = useState<RunMode>('module')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [moduleName, setModuleName] = useState('shell')
  const [moduleArgs, setModuleArgs] = useState('uptime')
  const [become, setBecome] = useState(false)
  const [playbook, setPlaybook] = useState('- name: 查看磁盘\n  hosts: all\n  tasks:\n    - name: df -h\n      ansible.builtin.command: df -h\n')
  const [templateId, setTemplateId] = useState<string | null>(null)
  const [runResult, setRunResult] = useState<AnsibleRunResult | null>(null)

  // ── 模板抽屉 / 历史抽屉 ──
  const [templateDrawerOpen, setTemplateDrawerOpen] = useState(false)
  const [editingTemplate, setEditingTemplate] = useState<AnsibleTemplateItem | null>(null)
  const [historyDetail, setHistoryDetail] = useState<AnsibleHistoryEntry | null>(null)
  const [templateForm] = Form.useForm()

  const { data: config } = useQuery({ queryKey: ['ansible-config'], queryFn: toolsApi.getConfig })
  const { data: targets = [], isLoading: loadingTargets } = useQuery({
    queryKey: ['ansible-targets'], queryFn: toolsApi.listTargets,
  })
  const { data: history = [] } = useQuery({
    queryKey: ['ansible-history'], queryFn: toolsApi.listHistory,
  })
  const { data: templates = [] } = useQuery({
    queryKey: ['ansible-templates'], queryFn: toolsApi.listTemplates,
  })
  const { data: roleOptions = [] } = useQuery({
    queryKey: ['ansible-role-options'], queryFn: toolsApi.listRoleOptions, enabled: templateDrawerOpen,
  })

  const readyTargets = useMemo(() => targets.filter((t) => t.ansible_ready), [targets])

  const enabled = !!config?.enabled
  const available = !!config?.ansible_available

  const invalidateRuns = () => {
    queryClient.invalidateQueries({ queryKey: ['ansible-history'] })
    queryClient.invalidateQueries({ queryKey: ['ansible-templates'] })
  }

  /** 执行统一入口：高危确认 → 提交 → 展示结果 → 刷新历史。 */
  const execute = (payloadBuilder: () => Promise<AnsibleRunResult>) => {
    riskConfirm.confirm('scripts:execute', async () => {
      try {
        const result = await payloadBuilder()
        setRunResult(result)
        if (result.ok) message.success(`执行成功（${result.target_count} 台目标）`)
        else message.warning(`执行失败，exit ${result.code}，详见输出`)
        invalidateRuns()
      } catch (error: unknown) {
        message.error(extractAxiosDetail(error) ?? '执行请求失败')
      }
    })
  }

  const runMutation = useMutation({
    mutationFn: toolsApi.runTask,
  })
  const playbookMutation = useMutation({ mutationFn: toolsApi.runPlaybook })
  const templateRunMutation = useMutation({ mutationFn: toolsApi.runTemplate })

  const submitRun = () => {
    if (runMode !== 'template' && selectedIds.length === 0) {
      message.warning('请先选择目标主机')
      return
    }
    setRunResult(null)
    if (runMode === 'module') {
      execute(() => runMutation.mutateAsync({
        asset_ids: selectedIds,
        module: moduleName,
        module_args: moduleArgs,
        become,
        become_user: 'root',
      }))
    } else if (runMode === 'playbook') {
      execute(() => playbookMutation.mutateAsync({ asset_ids: selectedIds, playbook }))
    } else {
      if (!templateId) {
        message.warning('请选择要执行的模板')
        return
      }
      execute(() => templateRunMutation.mutateAsync({ template_id: templateId }))
    }
  }

  // ── 模板 CRUD ──
  const saveTemplate = useMutation({
    mutationFn: (values: AnsibleTemplatePayload) =>
      editingTemplate
        ? toolsApi.updateTemplate(editingTemplate.template_id, values)
        : toolsApi.createTemplate(values),
    onSuccess: () => {
      message.success(editingTemplate ? '模板已更新' : '模板已创建')
      setTemplateDrawerOpen(false)
      invalidateRuns()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const deleteTemplate = useMutation({
    mutationFn: (id: string) => toolsApi.deleteTemplate(id),
    onSuccess: () => { message.success('模板已删除'); invalidateRuns() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const configMutation = useMutation({
    mutationFn: toolsApi.updateConfig,
    onSuccess: () => { message.success('配置已保存'); queryClient.invalidateQueries({ queryKey: ['ansible-config'] }) },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const openTemplateCreate = () => {
    setEditingTemplate(null)
    templateForm.resetFields()
    setTemplateDrawerOpen(true)
  }

  const openTemplateEdit = (tpl: AnsibleTemplateItem) => {
    setEditingTemplate(tpl)
    templateForm.setFieldsValue({
      name: tpl.name, description: tpl.description, module: tpl.module, module_args: tpl.module_args,
      become: tpl.become, become_user: tpl.become_user, run_all: tpl.run_all,
      asset_ids: tpl.asset_ids, allowed_roles: tpl.allowed_roles,
    })
    setTemplateDrawerOpen(true)
  }

  const targetColumns: ColumnsType<AnsibleTarget> = [
    {
      title: '主机', dataIndex: 'name', width: 180,
      render: (v: string, record) => (
        <Space size={6}>
          <Text strong>{v}</Text>
          {record.online ? <Tag color="green">在线</Tag> : <Tag>离线</Tag>}
        </Space>
      ),
    },
    { title: 'IP', dataIndex: 'ip', width: 130 },
    { title: 'SSH', dataIndex: 'ssh_user', width: 120, render: (v: string, r) => `${v}@${r.ip}:${r.ssh_port}` },
    { title: '来源', dataIndex: 'provider', width: 100, render: (v: string) => v || 'manual' },
    {
      title: 'Ansible 就绪', dataIndex: 'ansible_ready', width: 130,
      render: (v: boolean, record) => (
        <Tooltip title={record.auth_notice || (v ? '凭据可用' : '不满足执行条件')}>
          <Badge status={v ? 'success' : 'default'} text={v ? '可执行' : '不可执行'} />
        </Tooltip>
      ),
    },
  ]

  const templateColumns: ColumnsType<AnsibleTemplateItem> = [
    {
      title: '模板', dataIndex: 'name', width: 180,
      render: (v: string, record) => (
        <Space size={6}>
          <FileTextOutlined />
          <Text strong>{v}</Text>
          {record.run_all && <Tag color="blue">全部主机</Tag>}
        </Space>
      ),
    },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    {
      title: '模块', dataIndex: 'module', width: 110,
      render: (v: string, record) => <Text code>{v} {record.module_args ? record.module_args.slice(0, 24) : ''}</Text>,
    },
    {
      title: '授权组', dataIndex: 'allowed_roles', width: 130,
      render: (v: string[]) => (v?.length ? <Tag>{v.length} 个组</Tag> : <Text type="secondary">全员</Text>),
    },
    { title: '更新时间', dataIndex: 'updated_at', width: 165 },
    {
      title: '操作', key: 'ops', width: 220, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small" type="primary" ghost icon={<PlayCircleOutlined />}
            disabled={!canExecute || !enabled}
            onClick={() => execute(() => templateRunMutation.mutateAsync({ template_id: record.template_id }))}
          >
            执行
          </Button>
          <Button size="small" disabled={!canManage} onClick={() => openTemplateEdit(record)}>编辑</Button>
          <Popconfirm
            title="删除该模板？" okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => deleteTemplate.mutate(record.template_id)} disabled={!canManage}
          >
            <Button size="small" danger disabled={!canManage}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const historyColumns: ColumnsType<AnsibleHistoryEntry> = [
    { title: '时间', dataIndex: 'created_at', width: 165 },
    { title: '执行人', dataIndex: 'user', width: 100, render: (v: string) => <Text strong>{v || '-'}</Text> },
    { title: '模块', dataIndex: 'module', width: 120, render: (v: string) => <Text code>{v || '-'}</Text> },
    { title: '目标数', dataIndex: 'target_count', width: 80, align: 'right' },
    {
      title: '耗时', dataIndex: 'duration_ms', width: 90, align: 'right',
      render: (v: number) => `${(v / 1000).toFixed(1)}s`,
    },
    {
      title: '结果', dataIndex: 'ok', width: 100,
      render: (v: boolean, record) => v
        ? <Tag color="green">成功</Tag>
        : <Tag color="red">失败 {record.code}</Tag>,
    },
    {
      title: '操作', key: 'ops', width: 80,
      render: (_, record) => <Button size="small" onClick={() => setHistoryDetail(record)}>详情</Button>,
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>脚本执行</Typography.Title>
          <Text type="secondary">
            基于 Ansible 的批量模块执行与 Playbook 运行
            {config && ` · 控制端 ${available ? config.ansible_version || '就绪' : '不可用'}`}
          </Text>
        </div>
      </Flex>

      {(!enabled || !available) && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 16 }}
          message={config?.last_error || 'Ansible 执行通道未启用或控制端不可用'}
          description={enabled ? '请到「Ansible 配置」页检查可执行文件路径，或安装 ansible-core。' : '管理员可在「Ansible 配置」页启用执行通道。'}
        />
      )}

      <Tabs
        defaultActiveKey="run"
        items={[
          {
            key: 'run',
            label: <Space><PlayCircleOutlined />执行</Space>,
            children: (
              <Card size="small">
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  <Flex justify="space-between" wrap gap="small">
                    <Select
                      value={runMode}
                      onChange={(v) => { setRunMode(v); setRunResult(null) }}
                      style={{ width: 170 }}
                      options={[
                        { value: 'module', label: 'Ad-hoc 模块执行' },
                        { value: 'playbook', label: 'Playbook 运行' },
                        { value: 'template', label: '从模板执行' },
                      ]}
                    />
                    <Space>
                      <Button
                        type="primary" icon={<PlayCircleOutlined />}
                        disabled={!canExecute || !enabled || runMutation.isPending || playbookMutation.isPending || templateRunMutation.isPending}
                        loading={runMutation.isPending || playbookMutation.isPending || templateRunMutation.isPending}
                        onClick={submitRun}
                      >
                        执行（高危确认）
                      </Button>
                    </Space>
                  </Flex>

                  {runMode === 'module' && (
                    <Space direction="vertical" size="small" style={{ width: '100%' }}>
                      <Space wrap>
                        <Select
                          value={moduleName}
                          onChange={setModuleName}
                          style={{ width: 140 }}
                          showSearch
                          options={MODULE_PRESETS.map((m) => ({ value: m, label: m }))}
                        />
                        <Checkbox checked={become} onChange={(e) => setBecome(e.target.checked)}>
                          sudo 提权
                        </Checkbox>
                      </Space>
                      <Input
                        value={moduleArgs}
                        onChange={(e) => setModuleArgs(e.target.value)}
                        placeholder="模块参数，如 uptime 或 df -h"
                        style={{ fontFamily: 'monospace' }}
                      />
                    </Space>
                  )}

                  {runMode === 'playbook' && (
                    <TextArea
                      rows={10}
                      value={playbook}
                      onChange={(e) => setPlaybook(e.target.value)}
                      style={{ fontFamily: 'monospace', fontSize: 12 }}
                      placeholder="粘贴 Ansible Playbook（YAML）"
                    />
                  )}

                  {runMode === 'template' && (
                    <Select
                      value={templateId}
                      onChange={setTemplateId}
                      placeholder="选择批量模板"
                      style={{ width: '100%' }}
                      options={templates.map((t) => ({
                        value: t.template_id,
                        label: `${t.name}（${t.module} · ${t.run_all ? '全部主机' : `${t.asset_ids.length} 台绑定`})`,
                      }))}
                    />
                  )}

                  {runMode !== 'template' && (
                    <>
                      <Flex justify="space-between">
                        <Text strong>目标主机（已选 {selectedIds.length}/{readyTargets.length} 台可执行）</Text>
                        <Space>
                          <Button
                            size="small" disabled={!readyTargets.length}
                            onClick={() => setSelectedIds(readyTargets.map((t) => t.asset_id))}
                          >
                            全选可执行
                          </Button>
                          <Button size="small" onClick={() => setSelectedIds([])}>清空</Button>
                        </Space>
                      </Flex>
                      <Table<AnsibleTarget>
                        rowKey="asset_id"
                        size="small"
                        columns={targetColumns}
                        dataSource={targets}
                        loading={loadingTargets}
                        pagination={{ pageSize: 8, showSizeChanger: false }}
                        rowSelection={{
                          selectedRowKeys: selectedIds,
                          onChange: (keys) => setSelectedIds(keys as string[]),
                          // 不可执行的目标禁止勾选（凭据缺失等）。
                          getCheckboxProps: (record) => ({ disabled: !record.ansible_ready }),
                        }}
                        locale={{ emptyText: <Empty description="暂无可执行目标" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                      />
                    </>
                  )}

                  {runResult && <ResultPanel result={runResult} />}
                </Space>
              </Card>
            ),
          },
          {
            key: 'templates',
            label: <Space><FileTextOutlined />批量模板</Space>,
            children: (
              <Card size="small">
                <Flex justify="flex-end" style={{ marginBottom: 12 }}>
                  <Button type="primary" icon={<PlusOutlined />} disabled={!canManage} onClick={openTemplateCreate}>
                    新增模板
                  </Button>
                </Flex>
                <Table<AnsibleTemplateItem>
                  rowKey="template_id"
                  size="small"
                  columns={templateColumns}
                  dataSource={templates}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: <Empty description="暂无模板" /> }}
                />
              </Card>
            ),
          },
          {
            key: 'history',
            label: <Space><HistoryOutlined />执行历史</Space>,
            children: (
              <Card size="small">
                <Flex justify="flex-end" style={{ marginBottom: 12 }}>
                  <Button icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ['ansible-history'] })}>
                    刷新
                  </Button>
                </Flex>
                <Table<AnsibleHistoryEntry>
                  rowKey="run_id"
                  size="small"
                  columns={historyColumns}
                  dataSource={history}
                  pagination={{ pageSize: 15, showSizeChanger: false }}
                />
              </Card>
            ),
          },
          {
            key: 'config',
            label: <Space><SettingOutlined />Ansible 配置</Space>,
            children: (
              <Card size="small" style={{ maxWidth: 640 }}>
                <Descriptions size="small" column={1} style={{ marginBottom: 16 }}>
                  <Descriptions.Item label="控制端状态">
                    {available
                      ? <Tag color="green">{config?.ansible_version || '可用'}</Tag>
                      : <Tag color="red">{config?.last_error || '不可用'}</Tag>}
                  </Descriptions.Item>
                  <Descriptions.Item label="上次更新">{config?.updated_at || '-'}</Descriptions.Item>
                </Descriptions>
                <Form
                  layout="vertical"
                  onFinish={(values) => configMutation.mutate(values)}
                  disabled={!canManage}
                  initialValues={{
                    enabled: config?.enabled ?? false,
                    executable: config?.executable ?? 'ansible',
                    forks: config?.forks ?? 10,
                    timeout_seconds: config?.timeout_seconds ?? 120,
                    host_key_checking: config?.host_key_checking ?? false,
                  }}
                  key={config?.updated_at}
                >
                  <Form.Item name="enabled" label="启用执行通道" valuePropName="checked">
                    <Switch checkedChildren="启用" unCheckedChildren="停用" />
                  </Form.Item>
                  <Form.Item name="executable" label="可执行文件" extra="如 ansible / /usr/bin/ansible">
                    <Input style={{ width: 280 }} />
                  </Form.Item>
                  <Space size="large">
                    <Form.Item name="forks" label="并发（forks）">
                      <InputNumber min={1} max={100} style={{ width: '100%' }} />
                    </Form.Item>
                    <Form.Item name="timeout_seconds" label="超时（秒）">
                      <InputNumber min={10} max={3600} style={{ width: '100%' }} />
                    </Form.Item>
                  </Space>
                  <Form.Item name="host_key_checking" label="主机密钥校验" valuePropName="checked" extra="关闭可避免新主机首次连接交互卡死">
                    <Switch checkedChildren="校验" unCheckedChildren="跳过" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={configMutation.isPending} disabled={!canManage}>
                    保存配置
                  </Button>
                </Form>
              </Card>
            ),
          },
        ]}
      />

      {/* 模板编辑抽屉 */}
      <Drawer
        title={editingTemplate ? `编辑模板 - ${editingTemplate.name}` : '新增批量模板'}
        open={templateDrawerOpen}
        onClose={() => setTemplateDrawerOpen(false)}
        width={560}
        destroyOnClose
        extra={
          <Space>
            <Button onClick={() => setTemplateDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saveTemplate.isPending} onClick={() => templateForm.submit()}>保存</Button>
          </Space>
        }
      >
        <Form
          form={templateForm}
          layout="vertical"
          onFinish={(values) => saveTemplate.mutate(values)}
          initialValues={{ module: 'shell', become_user: 'root', become: false, run_all: false, allowed_roles: [] }}
        >
          <Form.Item name="name" label="模板名称" rules={[{ required: true, message: '请输入模板名称' }]}>
            <Input placeholder="如：批量检查磁盘水位" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input placeholder="模板用途说明" />
          </Form.Item>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="module" label="Ansible 模块" rules={[{ required: true }]}>
              <Select
                style={{ width: 160 }}
                showSearch
                options={MODULE_PRESETS.map((m) => ({ value: m, label: m }))}
              />
            </Form.Item>
            <Form.Item name="become" label="sudo 提权" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Form.Item name="module_args" label="模块参数">
            <TextArea rows={3} placeholder="df -h" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item name="run_all" label="目标范围" valuePropName="checked" extra="开启=全部可执行主机；关闭=仅绑定 asset_ids 列表">
            <Switch checkedChildren="全部主机" unCheckedChildren="指定主机" />
          </Form.Item>
          <Form.Item name="asset_ids" label="绑定主机（目标范围=指定主机时生效）">
            <Select
              mode="multiple"
              placeholder="选择主机"
              showSearch optionFilterProp="label"
              options={readyTargets.map((t) => ({ value: t.asset_id, label: `${t.name}（${t.ip}）` }))}
            />
          </Form.Item>
          <Form.Item name="allowed_roles" label="授权权限组" extra="空=所有权限组可用">
            <Select
              mode="multiple"
              placeholder="选择权限组"
              options={roleOptions.map((r) => ({ value: r.role, label: r.title }))}
            />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 历史详情抽屉 */}
      <Drawer
        title={`执行详情 - ${historyDetail?.run_id?.slice(0, 8) || ''}`}
        open={!!historyDetail}
        onClose={() => setHistoryDetail(null)}
        width={720}
      >
        {historyDetail && (
          <>
            <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
              <Descriptions.Item label="时间">{historyDetail.created_at}</Descriptions.Item>
              <Descriptions.Item label="执行人">{historyDetail.user || '-'}</Descriptions.Item>
              <Descriptions.Item label="模块"><Text code>{historyDetail.module}</Text></Descriptions.Item>
              <Descriptions.Item label="目标数">{historyDetail.target_count}</Descriptions.Item>
              <Descriptions.Item label="耗时">{(historyDetail.duration_ms / 1000).toFixed(1)}s</Descriptions.Item>
              <Descriptions.Item label="结果">
                {historyDetail.ok ? <Tag color="green">成功</Tag> : <Tag color="red">失败 {historyDetail.code}</Tag>}
              </Descriptions.Item>
            </Descriptions>
            {historyDetail.command_preview && (
              <Text code style={{ display: 'block', marginBottom: 8, fontSize: 12 }}>
                <CodeOutlined /> {historyDetail.command_preview}
              </Text>
            )}
            {historyDetail.stdout && (
              <pre style={{ background: '#fafafa', padding: 8, borderRadius: 4, maxHeight: 240, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>
                {historyDetail.stdout}
              </pre>
            )}
            {historyDetail.stderr && (
              <pre style={{ background: '#fff1f0', padding: 8, borderRadius: 4, maxHeight: 200, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>
                {historyDetail.stderr}
              </pre>
            )}
          </>
        )}
      </Drawer>
    </div>
  )
}
