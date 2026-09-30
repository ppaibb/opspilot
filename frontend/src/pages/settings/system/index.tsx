import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Tabs, Form, Input, Button, Switch, Space, Table, Drawer, Select, InputNumber, Collapse,
  Flex, App, Typography, Divider, Tag, Tooltip, Alert, Card, Row, Col,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, ApiOutlined, SaveOutlined,
} from '@ant-design/icons'
import { systemApi, imApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { NotificationChannel, FeatureToggles, LoginPageConfig, NotificationTemplateItem } from '@/types/api'

const { Title, Text } = Typography

// ────────────────────────────────────────────
// Tab 1: 通知渠道
// ────────────────────────────────────────────
function ChannelsTab() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingIdx, setEditingIdx] = useState<number | null>(null)
  const [form] = Form.useForm<NotificationChannel>()

  const { data, isLoading } = useQuery({ queryKey: ['notify-channels'], queryFn: systemApi.getChannels })
  const channels: NotificationChannel[] = data?.channels ?? []

  const saveMut = useMutation({
    mutationFn: (list: NotificationChannel[]) => systemApi.saveChannels(list),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notify-channels'] })
      message.success('已保存')
    },
    onError: () => message.error('保存失败'),
  })

  const testMut = useMutation({
    mutationFn: (id: string) => systemApi.testChannel(id),
    onSuccess: () => message.success('测试消息已发送'),
    onError: () => message.error('测试失败，请检查 Webhook 地址'),
  })

  const openCreate = () => { setEditingIdx(null); form.resetFields(); form.setFieldsValue({ type: 'webhook', enabled: true, send_resolved: true }); setDrawerOpen(true) }
  const openEdit = (idx: number) => { setEditingIdx(idx); form.setFieldsValue(channels[idx]); setDrawerOpen(true) }

  const handleSave = () => {
    form.validateFields().then((v) => {
      const next = editingIdx === null ? [...channels, v] : channels.map((c, i) => i === editingIdx ? v : c)
      saveMut.mutate(next)
      setDrawerOpen(false)
    })
  }

  const handleDelete = (idx: number) => saveMut.mutate(channels.filter((_, i) => i !== idx))

  const columns: ColumnsType<NotificationChannel & { _idx: number }> = [
    { title: '名称', dataIndex: 'name', width: 160 },
    {
      title: '类型', dataIndex: 'type', width: 120,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    { title: 'Webhook URL', dataIndex: 'webhook_url', ellipsis: true },
    {
      title: '启用', dataIndex: 'enabled', width: 80,
      render: (v: boolean, r) => (
        <Switch size="small" checked={v} onChange={(checked) => {
          const next = channels.map((c, i) => i === r._idx ? { ...c, enabled: checked } : c)
          saveMut.mutate(next)
        }} />
      ),
    },
    {
      title: '操作', key: 'action', width: 140,
      render: (_: unknown, r) => (
        <Space size="small">
          {r.id && <Tooltip title="测试发送"><Button size="small" icon={<ApiOutlined />} onClick={() => testMut.mutate(r.id!)} /></Tooltip>}
          <Tooltip title="编辑"><Button size="small" type="primary" icon={<EditOutlined />} onClick={() => openEdit(r._idx)} /></Tooltip>
          <Tooltip title="删除"><Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(r._idx)} /></Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={12}>
      <Flex justify="space-between">
        <Text type="secondary">配置告警通知渠道（Webhook / 飞书 / 企微 / 钉钉等）</Text>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增渠道</Button>
      </Flex>
      <Table
        rowKey={(r) => String(r._idx)}
        columns={columns}
        dataSource={channels.map((c, i) => ({ ...c, _idx: i }))}
        loading={isLoading}
        size="middle"
        pagination={false}
      />

      <Drawer title={editingIdx === null ? '新增通知渠道' : '编辑通知渠道'} open={drawerOpen} onClose={() => setDrawerOpen(false)} width={480}
        extra={<Button type="primary" loading={saveMut.isPending} onClick={handleSave}>保存</Button>}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="渠道名称" rules={[{ required: true }]}><Input placeholder="例如 运维告警-飞书" /></Form.Item>
          <Form.Item name="type" label="渠道类型" rules={[{ required: true }]}>
            <Select options={[
              { value: 'webhook',  label: '通用 Webhook' },
              { value: 'feishu',   label: '飞书' },
              { value: 'dingtalk', label: '钉钉' },
              { value: 'wechat',   label: '企业微信' },
              { value: 'email',    label: '邮件' },
            ]} />
          </Form.Item>
          <Form.Item name="webhook_url" label="Webhook URL" rules={[{ required: true }]}><Input placeholder="https://..." /></Form.Item>
          <Flex gap={24}>
            <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue={true}><Switch /></Form.Item>
            <Form.Item name="send_resolved" label="发送恢复通知" valuePropName="checked" initialValue={true}><Switch /></Form.Item>
          </Flex>
        </Form>
      </Drawer>
    </Flex>
  )
}

// ────────────────────────────────────────────
// Tab 2: 功能开关
// ────────────────────────────────────────────
function TogglesTab() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const { data, isLoading } = useQuery({ queryKey: ['feature-toggles'], queryFn: systemApi.getToggles })
  const toggles: FeatureToggles = data?.toggles ?? {}

  const saveMut = useMutation({
    mutationFn: (t: FeatureToggles) => systemApi.saveToggles(t),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['feature-toggles'] }); message.success('已保存') },
    onError: () => message.error('保存失败'),
  })

  if (isLoading) return <div style={{ padding: 32, textAlign: 'center' }}>加载中...</div>

  return (
    <Flex vertical gap={16} style={{ maxWidth: 600 }}>
      <Text type="secondary">控制系统各功能模块的启用状态</Text>
      {Object.entries(toggles).map(([key, val]) => (
        <Flex key={key} justify="space-between" align="center" style={{ padding: '8px 12px', borderRadius: 6, border: '1px solid #f0f0f0' }}>
          <div>
            <Text strong style={{ fontSize: 14 }}>{key}</Text>
            <br />
            <Text type="secondary" style={{ fontSize: 12 }}>{val ? '已启用' : '已禁用'}</Text>
          </div>
          <Switch
            checked={val}
            onChange={(checked) => saveMut.mutate({ ...toggles, [key]: checked })}
            loading={saveMut.isPending}
          />
        </Flex>
      ))}
    </Flex>
  )
}

// ────────────────────────────────────────────
// Tab 3: 登录页配置
// ────────────────────────────────────────────
function LoginPageTab() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [form] = Form.useForm<LoginPageConfig>()

  const { isLoading } = useQuery({
    queryKey: ['login-page-config'],
    queryFn: systemApi.getLoginPage,
    onSuccess: (data: LoginPageConfig) => form.setFieldsValue(data),
  } as Parameters<typeof useQuery>[0])

  const saveMut = useMutation({
    mutationFn: (data: Partial<LoginPageConfig>) => systemApi.saveLoginPage(data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['login-page-config'] }); message.success('已保存') },
    onError: () => message.error('保存失败'),
  })

  return (
    <Form form={form} layout="vertical" style={{ maxWidth: 540 }}>
      <Form.Item name="hero_title" label="主标题"><Input placeholder="例如 广阔天地 大有作为" /></Form.Item>
      <Form.Item name="hero_subtitle" label="副标题"><Input placeholder="例如 —— 数字化工作台 ——" /></Form.Item>
      <Form.Item name="background_image_url" label="背景图 URL（可选）"><Input placeholder="https://..." /></Form.Item>
      <Divider />
      <Flex gap={24}>
        <Form.Item name="sso_enabled" label="启用 SSO 登录" valuePropName="checked"><Switch /></Form.Item>
      </Flex>
      <Form.Item name="sso_button_text" label="SSO 按钮文案"><Input placeholder="企业单点登录" /></Form.Item>
      <Form.Item name="sso_hint" label="SSO 提示说明"><Input /></Form.Item>
      <Button type="primary" icon={<SaveOutlined />} loading={saveMut.isPending || isLoading}
        onClick={() => form.validateFields().then((v) => saveMut.mutate(v))}
      >
        保存
      </Button>
    </Form>
  )
}

// ────────────────────────────────────────────
// Tab 4: 通知模板
// ────────────────────────────────────────────
function TemplatesTab() {
  const queryClient = useQueryClient()
  const { message, modal } = App.useApp()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingIdx, setEditingIdx] = useState<number | null>(null)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({ queryKey: ['notification-templates'], queryFn: systemApi.getTemplates })
  const templates = data?.templates ?? []

  const saveMut = useMutation({
    mutationFn: (t: NotificationTemplateItem[]) => systemApi.saveTemplates(t),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['notification-templates'] }); message.success('已保存') },
    onError: () => message.error('保存失败'),
  })

  const handleSave = () => {
    form.validateFields().then((values) => {
      const newList = [...templates]
      if (editingIdx === null) {
        newList.push({ id: `tmpl-${Date.now()}`, ...values })
      } else {
        newList[editingIdx] = { ...newList[editingIdx], ...values }
      }
      saveMut.mutate(newList)
      setDrawerOpen(false)
      form.resetFields()
    })
  }

  const handleDelete = (idx: number) => {
    modal.confirm({
      title: '确认删除',
      content: '确定删除此通知模板吗？',
      onOk: () => {
        const newList = templates.filter((_, i: number) => i !== idx)
        saveMut.mutate(newList)
      },
    })
  }

  const columns: ColumnsType<NotificationTemplateItem> = [
    { title: '模板名称', dataIndex: 'name', key: 'name' },
    { title: '标题模板', dataIndex: 'title_template', key: 'title_template', ellipsis: true },
    { title: '内容模板', dataIndex: 'body_template', key: 'body_template', ellipsis: true },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_: unknown, record, idx: number) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => { setEditingIdx(idx); form.setFieldsValue(record); setDrawerOpen(true) }}
          />
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(idx)} />
        </Space>
      ),
    },
  ]

  if (isLoading) return <div style={{ padding: 32, textAlign: 'center' }}>加载中...</div>

  return (
    <Flex vertical gap={16}>
      <Flex justify="space-between" align="center">
        <Text type="secondary">配置告警通知的消息模板，支持变量替换</Text>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => { setEditingIdx(null); form.resetFields(); setDrawerOpen(true) }}
        >
          新增模板
        </Button>
      </Flex>

      <Table
        dataSource={templates}
        columns={columns}
        rowKey="id"
        pagination={false}
        size="small"
      />

      <Drawer
        title={editingIdx === null ? '新增通知模板' : '编辑通知模板'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={600}
        footer={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" icon={<SaveOutlined />} onClick={handleSave}>保存</Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="模板名称" rules={[{ required: true }]}>
            <Input placeholder="例如：告警通知" />
          </Form.Item>
          <Form.Item name="title_template" label="标题模板" rules={[{ required: true }]}>
            <Input placeholder="例如：【告警】{{ alert.name }}" />
          </Form.Item>
          <Form.Item
            name="body_template"
            label="内容模板"
            rules={[{ required: true }]}
            extra="支持变量：{{ alert.name }}, {{ alert.severity }}, {{ alert.instance }}, {{ alert.summary }}"
          >
            <Input.TextArea
              rows={8}
              placeholder={`例如：
告警名称：{{ alert.name }}
严重程度：{{ alert.severity }}
实例：{{ alert.instance }}
描述：{{ alert.summary }}
触发时间：{{ alert.started_at }}`}
            />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="模板用途说明" />
          </Form.Item>
        </Form>
      </Drawer>
    </Flex>
  )
}

// ────────────────────────────────────────────
// Tab 5: IM 接入（飞书）
// ────────────────────────────────────────────
function ImAccessTab() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const { data, isLoading } = useQuery({ queryKey: ['im-feishu-config'], queryFn: imApi.getFeishuConfig })

  const saveMut = useMutation({
    mutationFn: (v: { app_id: string; app_secret?: string; default_webhook_url: string }) => imApi.saveFeishuConfig(v),
    onSuccess: () => { message.success('已保存') },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '保存失败')
    },
  })

  return (
    <Flex vertical gap={16}>
      <Alert
        type="info"
        showIcon
        message="飞书接入配置（个人群 @ 通知）"
        description="配置飞书自建应用凭证与系统默认群机器人。凭证仅用于把用户填写的手机号/邮箱解析为飞书身份（open_id），不用于发送消息；消息通过群自定义机器人投递。"
      />
      <Form
        form={form}
        layout="vertical"
        style={{ maxWidth: 540 }}
        initialValues={data ? { ...data, app_secret: '' } : undefined}
        key={JSON.stringify(data ?? {})}
      >
        <Form.Item name="app_id" label="App ID" rules={[{ required: true, message: '请输入飞书自建应用 App ID' }]}>
          <Input placeholder="cli_ 开头的应用 ID" />
        </Form.Item>
        <Form.Item
          name="app_secret"
          label="App Secret"
          extra={data?.app_secret_set ? '已配置，留空表示不修改' : '尚未配置'}
          rules={[{ required: !data?.app_secret_set, message: '请输入 App Secret' }]}
        >
          <Input.Password placeholder="应用密钥" autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="default_webhook_url"
          label="系统默认群机器人 Webhook"
          rules={[
            {
              validator: (_, value: string) =>
                !value || (value.startsWith('https://') && value.includes('open.feishu.cn'))
                  ? Promise.resolve()
                  : Promise.reject(new Error('必须是 https://open.feishu.cn 的群机器人地址')),
            },
          ]}
          extra="用户未填个人群机器人时，@ 消息发到这个群"
        >
          <Input placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." />
        </Form.Item>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saveMut.isPending || isLoading}
          onClick={() => form.validateFields().then((v) => saveMut.mutate(v))}
        >
          保存
        </Button>
      </Form>

      <Card title="配置步骤（管理员一次性操作）" size="small">
        <ol style={{ margin: 0, paddingLeft: 20, color: 'rgba(0,0,0,0.65)' }}>
          <li>
            飞书开放平台创建<Text strong>企业自建应用</Text>，在「权限管理」中开通
            <Text code>contact:user.id:readonly</Text>（按手机号/邮箱获取用户 ID），并发布应用版本。
          </li>
          <li>把 App ID / App Secret 填到上方并保存。</li>
          <li>
            在需要接收通知的飞书群里：群设置 → 群机器人 → 添加<Text strong>自定义机器人</Text>，
            复制 Webhook 地址填到「系统默认群机器人」。
          </li>
          <li>通知 @ 某人时，该成员必须在此群里，否则 @ 不会生效。</li>
          <li>用户在「个人 IM 通知」页绑定手机号/邮箱后即可接收。</li>
        </ol>
      </Card>
    </Flex>
  )
}

// ────────────────────────────────────────────
// 监控连接（支持分体部署）
// ────────────────────────────────────────────
function MonitoringTab() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [testResult, setTestResult] = useState<{
    overall_ok?: boolean
    checks?: Array<{ name: string; ok: boolean; message: string }>
    hints?: string[]
    agent_write_urls?: { remote_write_url?: string; vlogs_ingest_url?: string }
  } | null>(null)
  const { data, isLoading } = useQuery({
    queryKey: ['monitoring-config'],
    queryFn: () => systemApi.getMonitoring(),
  })
  const saveMut = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      systemApi.saveMonitoring(values as never),
    onSuccess: () => {
      message.success('监控连接已保存')
      queryClient.invalidateQueries({ queryKey: ['monitoring-config'] })
    },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '保存失败'),
  })

  const testMut = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      systemApi.selfCheck(values as never) as Promise<{
        overall_ok?: boolean
        checks?: Array<{ name: string; ok: boolean; message: string }>
        hints?: string[]
        agent_write_urls?: { remote_write_url?: string; vlogs_ingest_url?: string }
      }>,
    onSuccess: (result) => {
      setTestResult(result)
      if (result?.overall_ok) message.success('全部连通正常')
      else message.warning('存在未连通项，详见下方结果')
    },
    onError: (e: unknown) => message.error(extractAxiosDetail(e) ?? '测试失败'),
  })

  return (
    <Flex vertical gap={16}>
      <Form
        form={form}
        layout="vertical"
        style={{ maxWidth: 860 }}
        key={JSON.stringify(data ?? {})}
        initialValues={{
          vm_url: data?.vm_url || '',
          vlogs_url: data?.vlogs_url || '',
          remote_write_url: data?.remote_write_url || '',
          vlogs_ingest_url: data?.vlogs_ingest_url || '',
          grafana_url: data?.grafana_url || '',
          vmui_url: data?.vmui_url || '',
          vlogsui_url: data?.vlogsui_url || '',
          timeout_seconds: data?.timeout_seconds || 15,
          default_step_seconds: data?.default_step_seconds || 60,
          verify_tls: data?.verify_tls ?? true,
          auth_type: data?.auth_type || 'none',
          username: data?.username || '',
          secret: '',
        }}
        onFinish={(values) => {
          // 只提交改动过的字段，支持「只想改 Grafana」这类单项修改
          const keys = [
            'vm_url', 'vlogs_url', 'remote_write_url', 'vlogs_ingest_url',
            'grafana_url', 'vmui_url', 'vlogsui_url',
            'timeout_seconds', 'default_step_seconds', 'verify_tls',
            'auth_type', 'username', 'secret',
          ] as const
          const payload: Record<string, unknown> = {}
          for (const k of keys) {
            const touched = k === 'secret' ? !!values.secret : form.isFieldTouched(k)
            if (touched) payload[k] = values[k]
          }
          if (Object.keys(payload).length === 0) {
            message.info('没有需要保存的修改')
            return
          }
          saveMut.mutate(payload)
        }}
      >
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="vm_url" label="VictoriaMetrics">
              <Input placeholder="http://192.168.3.99:8428" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="vlogs_url" label="VictoriaLogs">
              <Input placeholder="http://192.168.3.99:9428" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="grafana_url" label="Grafana">
              <Input placeholder="http://192.168.3.99:3002" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="timeout_seconds" label="超时（秒）">
              <InputNumber min={3} max={60} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>

        <Collapse
          ghost
          style={{ marginBottom: 16 }}
          items={[{
            key: 'adv',
            label: <Text type="secondary" style={{ fontSize: 13 }}>高级（一般不用改，留空自动推导）</Text>,
            children: (
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="remote_write_url" label="指标写入（Agent）">
                    <Input placeholder="留空 = {VM}/api/v1/write" />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="vlogs_ingest_url" label="日志写入（Agent）">
                    <Input placeholder="留空 = {VLogs}/insert/..." />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="vmui_url" label="VMUI">
                    <Input placeholder="留空 = {VM}/vmui" />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="vlogsui_url" label="VictoriaLogs UI">
                    <Input placeholder="留空 = {VLogs}/select/vmui" />
                  </Form.Item>
                </Col>
              </Row>
            ),
          }]}
        />

        <Space>
          <Button
            icon={<ApiOutlined />}
            loading={testMut.isPending}
            onClick={() => {
              // 测试当前表单值，不落库
              form.validateFields().then((values) => {
                const payload: Record<string, unknown> = { ...values }
                if (!values.secret) delete payload.secret
                testMut.mutate(payload)
              })
            }}
          >
            测试连接
          </Button>
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saveMut.isPending || isLoading}>
            保存
          </Button>
        </Space>
      </Form>

      {testResult && (
        <Card size="small" title="连通测试结果" style={{ maxWidth: 640 }}>
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {testResult.checks?.map((c: { name: string; ok: boolean; message: string }) => (
              <Flex key={c.name} justify="space-between" align="center" gap={12}>
                <Space>
                  {c.ok ? (
                    <Tag color="success">成功</Tag>
                  ) : (
                    <Tag color="error">失败</Tag>
                  )}
                  <Text>{c.name}</Text>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>{c.message}</Text>
              </Flex>
            ))}
            {(testResult.hints || []).map((h: string) => (
              <Alert key={h} type="warning" showIcon message={h} style={{ padding: '4px 12px' }} />
            ))}
            {testResult.agent_write_urls && (
              <div style={{ fontSize: 12, color: 'var(--ink-3)', marginTop: 4 }}>
                <div>
                  Agent 指标写入：{testResult.agent_write_urls.remote_write_url || '(自动推导)'}
                </div>
                <div>
                  Agent 日志写入：{testResult.agent_write_urls.vlogs_ingest_url || '(自动推导)'}
                </div>
              </div>
            )}
          </Space>
        </Card>
      )}
    </Flex>
  )
}

// ────────────────────────────────────────────
// 主页面
// ────────────────────────────────────────────
export default function SystemPage() {
  return (
    <Flex vertical gap={16}>
      <Title level={5} style={{ margin: 0 }}>系统管理</Title>
      <Tabs
        items={[
          { key: 'monitoring', label: '监控连接', children: <MonitoringTab /> },
          { key: 'channels', label: '通知渠道', children: <ChannelsTab /> },
          { key: 'templates', label: '通知模板', children: <TemplatesTab /> },
          { key: 'toggles',  label: '功能开关',  children: <TogglesTab /> },
          { key: 'loginpage', label: '登录页配置', children: <LoginPageTab /> },
          { key: 'im', label: 'IM 接入（飞书）', children: <ImAccessTab /> },
        ]}
      />
    </Flex>
  )
}
