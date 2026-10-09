/**
 * AI 模型配置页（/ai/llm-config）。
 *
 * 管理平台可用的 LLM Provider 配置：
 * - 配置 CRUD（provider / model / base_url / 采样参数 / 系统提示词）
 * - 单配置「激活」——全局同一时刻只有一个配置生效
 * - 连通性测试（不打扰当前激活状态，用表单当前值即时探测）
 *
 * 安全约定：列表接口不回传明文 API Key（has_api_key 标记），
 * 编辑时留空表示沿用已存密钥；using_env_fallback 表示走环境变量兜底。
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Table, Button, Space, Tag, Flex, Typography, Drawer, Form, Input, InputNumber, Select,
  Popconfirm, Slider, App, Tooltip, Badge, Alert, Card,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, ReloadOutlined, ThunderboltOutlined, CheckCircleOutlined, ApiOutlined,
} from '@ant-design/icons'
import { aiPlatformApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { LLMConfigDetail, LLMConfigTestResult } from '@/types/api'

const { Text } = Typography
const { TextArea } = Input

/** Provider 选项：value 与后端 DEFAULT_PROVIDER 体系对齐。 */
const PROVIDER_OPTIONS = [
  { value: 'gemini', label: 'Google Gemini' },
  { value: 'openai_compatible', label: 'OpenAI 兼容（DeepSeek / Qwen / vLLM…）' },
  { value: 'anthropic', label: 'Anthropic Claude' },
]

const DEFAULT_SYSTEM_PROMPT =
  '你是 OpsPilot 运维智能助手，基于平台只读上下文回答运维问题，回答保持简洁、可执行。'

interface FormValues {
  name: string
  provider: string
  base_url?: string
  model: string
  api_key?: string
  temperature: number
  max_output_tokens: number
  timeout_seconds: number
  system_prompt: string
  activate: boolean
}

export default function LLMConfigPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.ai.llm.manage')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<LLMConfigDetail | null>(null)
  const [form] = Form.useForm()
  const [testResult, setTestResult] = useState<LLMConfigTestResult | null>(null)
  const [testing, setTesting] = useState(false)

  const { data: configList, isLoading, refetch } = useQuery({
    queryKey: ['ai-llm-configs'],
    queryFn: aiPlatformApi.listLLMConfigs,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['ai-llm-configs'] })

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      // 编辑时留空 api_key = 沿用已存密钥，不覆盖为空。
      const payload = { ...values, api_key: values.api_key || undefined }
      return editing
        ? aiPlatformApi.updateLLMConfig(editing.id, payload)
        : aiPlatformApi.createLLMConfig(payload)
    },
    onSuccess: () => {
      message.success(editing ? '配置已更新' : '配置已创建')
      setDrawerOpen(false)
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => aiPlatformApi.deleteLLMConfig(id),
    onSuccess: () => {
      message.success('配置已删除')
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const activateMutation = useMutation({
    mutationFn: (id: string) => aiPlatformApi.activateLLMConfig(id),
    onSuccess: () => {
      message.success('已切换激活配置')
      invalidate()
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '激活失败'),
  })

  /** 用抽屉表单当前值做一次连通性探测（不落库、不影响激活态）。 */
  const runConnectivityTest = async () => {
    const values = await form.validateFields(['provider', 'base_url', 'model', 'api_key', 'temperature', 'max_output_tokens', 'timeout_seconds', 'system_prompt'])
    setTesting(true)
    setTestResult(null)
    try {
      const result = await aiPlatformApi.testLLMConfig({
        ...values,
        // 编辑已有配置且未重填密钥时，用空串触发服务端「沿用已存密钥」逻辑。
        api_key: values.api_key || '',
      })
      setTestResult(result)
    } catch (error: unknown) {
      message.error(extractAxiosDetail(error) ?? '测试请求失败')
    } finally {
      setTesting(false)
    }
  }

  const openCreate = () => {
    setEditing(null)
    setTestResult(null)
    form.resetFields()
    setDrawerOpen(true)
  }

  const openEdit = (record: LLMConfigDetail) => {
    setEditing(record)
    setTestResult(null)
    form.setFieldsValue({
      name: record.name,
      provider: record.provider,
      base_url: record.base_url,
      model: record.model,
      api_key: '',
      temperature: record.temperature,
      max_output_tokens: record.max_output_tokens,
      timeout_seconds: record.timeout_seconds,
      system_prompt: record.system_prompt,
      activate: record.is_active,
    })
    setDrawerOpen(true)
  }

  const columns: ColumnsType<LLMConfigDetail> = [
    {
      title: '名称', dataIndex: 'name', width: 160,
      render: (v: string, record) => (
        <Space size={6}>
          <Text strong>{v}</Text>
          {record.is_active && <Tag color="green">激活中</Tag>}
        </Space>
      ),
    },
    {
      title: 'Provider', dataIndex: 'provider', width: 170,
      render: (v: string) => PROVIDER_OPTIONS.find((p) => p.value === v)?.label || v,
    },
    {
      title: '模型', dataIndex: 'model', width: 180, ellipsis: true,
      render: (v: string) => <Text code>{v}</Text>,
    },
    {
      title: '密钥', dataIndex: 'has_api_key', width: 130,
      render: (v: boolean, record) => (
        <Tooltip title={record.using_env_fallback ? '未存密钥，走环境变量兜底' : v ? '已加密保存' : '未配置'}>
          <Badge
            status={record.using_env_fallback ? 'warning' : v ? 'success' : 'default'}
            text={record.using_env_fallback ? '环境变量' : v ? '已保存' : '未配置'}
          />
        </Tooltip>
      ),
    },
    {
      title: '采样温度', dataIndex: 'temperature', width: 90, align: 'right',
      render: (v: number) => v.toFixed(2),
    },
    { title: '更新时间', dataIndex: 'updated_at', width: 165 },
    {
      title: '操作', key: 'actions', width: 230, fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            disabled={!canManage || record.is_active}
            loading={activateMutation.isPending && activateMutation.variables === record.id}
            onClick={() => activateMutation.mutate(record.id)}
          >
            {record.is_active ? '当前生效' : '激活'}
          </Button>
          <Button size="small" disabled={!canManage} onClick={() => openEdit(record)}>编辑</Button>
          <Popconfirm
            title="删除该模型配置？"
            description={record.is_active ? '该配置正在生效，删除后 AI 能力将降级。' : '不可恢复。'}
            okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
            onConfirm={() => deleteMutation.mutate(record.id)}
            disabled={!canManage}
          >
            <Button size="small" danger disabled={!canManage}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>模型配置</Typography.Title>
          <Text type="secondary">管理 LLM Provider 与默认参数；同一时刻只有一个配置对全局生效</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canManage} onClick={openCreate}>
            新增配置
          </Button>
        </Space>
      </Flex>

      {configList && configList.items.length > 0 && !configList.active_id && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 16 }}
          message="尚无激活配置"
          description="AI 问答将使用环境变量兜底配置。"
        />
      )}

      <Table<LLMConfigDetail>
        rowKey="id"
        columns={columns}
        dataSource={configList?.items || []}
        loading={isLoading}
        pagination={false}
      />

      <Drawer
        title={editing ? `编辑配置 - ${editing.name}` : '新增模型配置'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={560}
        destroyOnClose
        extra={
          <Space>
            <Button icon={<ApiOutlined />} loading={testing} onClick={runConnectivityTest}>
              测试连通
            </Button>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saveMutation.isPending} onClick={() => form.submit()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={(values) => saveMutation.mutate(values)}
          initialValues={{
            provider: 'gemini',
            temperature: 0.2,
            max_output_tokens: 1200,
            timeout_seconds: 30,
            system_prompt: DEFAULT_SYSTEM_PROMPT,
            activate: true,
          }}
        >
          <Form.Item name="name" label="配置名称" rules={[{ required: true, message: '请输入配置名称' }]}>
            <Input placeholder="如：生产-Gemini 2.5 Pro" />
          </Form.Item>
          <Form.Item name="provider" label="Provider" rules={[{ required: true }]}>
            <Select options={PROVIDER_OPTIONS} />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, cur) => prev.provider !== cur.provider}
          >
            {({ getFieldValue }) => (
              <Form.Item
                name="base_url"
                label="Base URL"
                rules={getFieldValue('provider') === 'openai_compatible'
                  ? [{ required: true, message: 'OpenAI 兼容模式必须填写 Base URL' }]
                  : []}
                extra="OpenAI 兼容模式必填（如 https://api.deepseek.com/v1）；Gemini / Claude 可留空走官方地址"
              >
                <Input placeholder="https://api.example.com/v1" />
              </Form.Item>
            )}
          </Form.Item>
          <Form.Item name="model" label="模型 ID" rules={[{ required: true, message: '请输入模型 ID' }]}>
            <Input placeholder="gemini-2.5-pro / deepseek-chat / claude-sonnet-4" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            extra={editing?.has_api_key ? '已加密保存；留空表示沿用原密钥' : '保存在服务端，列表接口不会回传'}
          >
            <Input.Password placeholder={editing?.has_api_key ? '留空保持不变' : '粘贴 API Key'} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="temperature" label="采样温度（低=稳定，高=发散）">
            <Slider min={0} max={1} step={0.05} marks={{ 0: '0', 0.2: '0.2', 1: '1' }} />
          </Form.Item>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item name="max_output_tokens" label="最大输出 Tokens" style={{ minWidth: 200 }}>
              <InputNumber min={128} max={8192} step={128} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="timeout_seconds" label="超时（秒）" style={{ minWidth: 200 }}>
              <InputNumber min={5} max={120} step={5} style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Form.Item name="system_prompt" label="系统提示词">
            <TextArea rows={4} placeholder="定义 AI 助手的人设与回答风格" />
          </Form.Item>
          <Form.Item name="activate" label="保存后激活">
            <Select
              options={[
                { value: true, label: '是（保存后立即生效）' },
                { value: false, label: '否（仅保存）' },
              ]}
            />
          </Form.Item>
        </Form>

        {testResult && (
          <Card size="small" title="连通性测试结果" style={{ marginTop: 8 }}>
            <Space direction="vertical" size={2}>
              <Space>
                {testResult.ok
                  ? <Tag color="green" icon={<CheckCircleOutlined />}>成功</Tag>
                  : <Tag color="red">失败</Tag>}
                <Text type="secondary">延迟 {testResult.latency_ms}ms · {testResult.model}</Text>
              </Space>
              <Text type={testResult.ok ? 'secondary' : 'danger'} style={{ fontSize: 12 }}>
                {testResult.detail}
              </Text>
            </Space>
          </Card>
        )}
      </Drawer>
    </div>
  )
}
