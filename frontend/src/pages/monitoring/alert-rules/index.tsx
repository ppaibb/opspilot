/**
 * 告警规则页（/monitoring/alert-rules）。
 *
 * 布局：页头（启用/停用统计 + 新增规则）/ 筛选条一行 /
 * 主次分区（规则列表 2/3 + 说明/模板 1/3）。
 * 按启用/停用 + 严重度排序；行内 Switch 切换 enabled；
 * 表达式 Tooltip 全文；「测试命中」按钮前置到行内。
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Card, Table, Button, Space, Drawer, Form, Input, Select, Switch, Spin,
  App, Tag, Popconfirm, Alert, Divider, Steps, Typography, Collapse, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, SaveOutlined, ThunderboltOutlined, EyeOutlined, LinkOutlined,
} from '@ant-design/icons'
import { imApi, monitoringApi, systemApi, assetsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { AlertRuleItem, RuleTestResponse } from '@/types/api'
import SeverityTag from '@/components/monitoring/SeverityTag'
import EmptyState from '@/components/monitoring/EmptyState'
import { PageHeader, FilterBar, MainSplit, SideCard } from '@/components/monitoring/PageShell'
import { filterRules, ruleStats, type RuleStatusFilter } from './ruleList'

const { TextArea } = Input
const { Text } = Typography

/** 新增/编辑表单的字段结构（与后端 name/duration 契约一致） */
interface AlertRuleFormValues {
  name: string
  expr: string
  duration: string
  severity: string
  summary?: string
  description?: string
  channel_ids?: string[]
  host_ids?: string[]
  im_user_ids?: number[]
  enabled?: boolean
  recovery_debounce_seconds?: number
  reminder_interval_minutes?: number
  max_reminders_cap?: number
  group?: string
}

export default function AlertRulesPage() {
  const queryClient = useQueryClient()
  const { message, modal } = App.useApp()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingIdx, setEditingIdx] = useState<number | null>(null)
  const [form] = Form.useForm<AlertRuleFormValues>()
  const [preview, setPreview] = useState<RuleTestResponse | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<RuleStatusFilter>('all')
  const [severityFilter, setSeverityFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [rowTest, setRowTest] = useState<{ name: string; result: RuleTestResponse | null; loading: boolean } | null>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['alert-rules'],
    queryFn: () => monitoringApi.listRules(),
    retry: false,
  })

  const rules: AlertRuleItem[] = data?.rules ?? []
  const stats = useMemo(() => ruleStats(rules), [rules])
  const displayRules = useMemo(
    () => filterRules(rules, { status: statusFilter, severity: severityFilter, search }),
    [rules, statusFilter, severityFilter, search],
  )

  // 保存用原始顺序（filterRules 会排序，保存时必须按原索引写回）
  const ruleIndex = useMemo(() => {
    const map = new Map<string, number>()
    rules.forEach((r, i) => map.set(r.id || r.name, i))
    return map
  }, [rules])

  const { data: imUsers } = useQuery({ queryKey: ['im-users'], queryFn: imApi.listUsers })
  const userNameMap = new Map<number, string>((imUsers ?? []).map((u) => [u.id, u.name || u.username]))

  const { data: channelsData } = useQuery({ queryKey: ['monitoring-channels'], queryFn: systemApi.getChannels })
  const { data: hosts = [] } = useQuery({ queryKey: ['hosts'], queryFn: assetsApi.list })
  const channelOptions = useMemo(
    () => (channelsData?.channels || []).map((c) => ({ value: c.id, label: c.name || c.id })),
    [channelsData],
  )
  const hostOptions = useMemo(
    () => hosts.map((h) => ({ value: h.id, label: `${h.name || h.hostname || h.ip}${h.ip ? ` (${h.ip})` : ''}` })),
    [hosts],
  )

  const saveMut = useMutation({
    mutationFn: (r: AlertRuleItem[]) => monitoringApi.saveRules(r),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-rules'] })
      message.success('已保存，内置告警引擎约 30 秒内生效')
      setSaveError(null)
    },
    onError (err: unknown) {
      if (axios.isAxiosError(err) && err.response?.status === 403) {
        setSaveError('没有修改告警规则的权限。请让管理员在「权限管理」开通 action.monitoring.rules.manage。')
        return
      }
      setSaveError(extractAxiosDetail(err) ?? '保存失败，请检查表达式与必填字段后重试')
    },
  })

  /** 把表单值合并进规则时，保留 channel_ids / host_ids 等非表单字段。 */
  const mergeRule = (base: Partial<AlertRuleItem>, values: AlertRuleFormValues): AlertRuleItem => ({
    ...base,
    ...values,
    name: values.name,
    duration: values.duration,
    channel_ids: values.channel_ids ?? base.channel_ids ?? [],
    host_ids: values.host_ids ?? base.host_ids ?? [],
    im_user_ids: values.im_user_ids ?? base.im_user_ids ?? [],
    enabled: values.enabled ?? base.enabled ?? true,
  })

  const handleSave = () => {
    form.validateFields().then((values) => {
      const newList = [...rules]
      if (editingIdx === null) {
        newList.push(mergeRule({ id: `rule-${Date.now()}` }, values))
      } else {
        newList[editingIdx] = mergeRule(newList[editingIdx], values)
      }
      saveMut.mutate(newList)
      setDrawerOpen(false)
      form.resetFields()
      setPreview(null)
    }).catch(() => {
      message.warning('请先补全必填项')
    })
  }

  const handlePreview = async () => {
    const values = form.getFieldsValue()
    if (!values.expr?.trim()) {
      message.warning('先填写 PromQL 表达式')
      return
    }
    setPreviewing(true)
    setPreview(null)
    try {
      const result = await monitoringApi.testRule({
        expr: values.expr.trim(),
        host_ids: values.host_ids || [],
      })
      setPreview(result)
    } catch (err) {
      setPreview({
        ok: false,
        error: extractAxiosDetail(err) ?? '预览失败',
        expr: values.expr,
        match_count: 0,
        series: [],
      })
    } finally {
      setPreviewing(false)
    }
  }

  /** 行内「测试命中」：直接用规则表达式试跑，结果弹在侧栏。 */
  const handleRowTest = async (record: AlertRuleItem) => {
    setRowTest({ name: record.name, result: null, loading: true })
    try {
      const result = await monitoringApi.testRule({
        expr: record.expr,
        host_ids: record.host_ids || [],
      })
      setRowTest({ name: record.name, result, loading: false })
    } catch (err) {
      setRowTest({
        name: record.name,
        result: {
          ok: false,
          error: extractAxiosDetail(err) ?? '测试失败',
          expr: record.expr,
          match_count: 0,
          series: [],
        },
        loading: false,
      })
    }
  }

  const handleDelete = (idx: number) => {
    modal.confirm({
      title: '确认删除',
      content: '确定删除此告警规则吗？',
      onOk: () => {
        const newList = rules.filter((_, i) => i !== idx)
        saveMut.mutate(newList)
      },
    })
  }

  const handleToggle = (idx: number) => {
    const newList = rules.map((r, i) => (i === idx ? { ...r, enabled: !r.enabled } : r))
    saveMut.mutate(newList)
  }

  /** 显示行 → 原始索引（排序/过滤后仍能正确写回）。 */
  const resolveIdx = (record: AlertRuleItem) => {
    const key = record.id || record.name
    const idx = ruleIndex.get(key)
    return idx == null ? -1 : idx
  }

  const ruleTemplates = [
    { name: 'CPU 高负载', expr: 'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) * 100 > 80', duration: '5m' },
    { name: '内存不足', expr: '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100 > 90', duration: '5m' },
    { name: '磁盘空间不足', expr: '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100 > 85', duration: '5m' },
    { name: '主机离线', expr: 'up{job="node"} == 0', duration: '1m' },
  ]

  const columns: ColumnsType<AlertRuleItem> = [
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 70,
      render: (enabled: boolean, record) => {
        const idx = resolveIdx(record)
        return (
          <Switch
            checked={enabled}
            size="small"
            disabled={idx < 0 || saveMut.isPending}
            onChange={() => handleToggle(idx)}
          />
        )
      },
    },
    { title: '规则名称', dataIndex: 'name', key: 'name', width: 150, ellipsis: true },
    {
      title: '表达式',
      dataIndex: 'expr',
      key: 'expr',
      ellipsis: true,
      render: (expr: string) => (
        <Tooltip title={<span style={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>{expr}</span>}>
          <Text code style={{ fontSize: 12 }}>{expr}</Text>
        </Tooltip>
      ),
    },
    {
      title: '持续',
      dataIndex: 'duration',
      key: 'duration',
      width: 70,
    },
    {
      title: '严重程度',
      dataIndex: 'severity',
      key: 'severity',
      width: 100,
      render: (sev: string) => <SeverityTag severity={sev} />,
    },
    {
      title: '通知渠道',
      dataIndex: 'channel_ids',
      key: 'channel_ids',
      width: 120,
      render: (ids: string[] | undefined) => {
        if (!Array.isArray(ids) || ids.length === 0) return <Tag>未设置</Tag>
        return (
          <Space size={4} wrap>
            {ids.map((id) => (
              <Tag key={id} color="geekblue">{channelOptions.find((c) => c.value === id)?.label || id}</Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: '飞书通知人',
      dataIndex: 'im_user_ids',
      key: 'im_user_ids',
      width: 110,
      render: (ids: number[] | undefined) => {
        if (!Array.isArray(ids) || ids.length === 0) return <Tag>未设置</Tag>
        return (
          <Space size={4} wrap>
            {ids.map((id) => (
              <Tag key={id} color="processing">{userNameMap.get(id) || `用户${id}`}</Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: unknown, record: AlertRuleItem) => {
        const idx = resolveIdx(record)
        return (
          <Space size={4} wrap>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              onClick={() => handleRowTest(record)}
              loading={rowTest?.name === record.name && rowTest.loading}
            >
              测试命中
            </Button>
            <Button
              size="small"
              icon={<EditOutlined />}
              disabled={idx < 0}
              onClick={() => {
                setEditingIdx(idx)
                form.setFieldsValue({
                  ...record,
                  channel_ids: record.channel_ids || [],
                  host_ids: record.host_ids || [],
                  im_user_ids: record.im_user_ids || [],
                })
                setPreview(null)
                setDrawerOpen(true)
              }}
            />
            <Popconfirm title="确认删除？" disabled={idx < 0} onConfirm={() => handleDelete(idx)}>
              <Button size="small" danger icon={<DeleteOutlined />} disabled={idx < 0} />
            </Popconfirm>
          </Space>
        )
      },
    },
  ]

  if (isLoading) return <Spin style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }} size="large" />

  if (isError) {
    const status = (error as { response?: { status?: number } })?.response?.status
    return (
      <EmptyState
        kind={status === 403 ? 'forbidden' : 'no-result'}
        title={status === 403 ? undefined : '规则加载失败'}
        description={status === 403 ? undefined : `原因：${extractAxiosDetail(error) || '未知'}。下一步：检查后端服务与网络后刷新。`}
        actionLabel="刷新"
        onAction={() => queryClient.invalidateQueries({ queryKey: ['alert-rules'] })}
      />
    )
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <PageHeader
        title="告警规则"
        subtitle={
          <Space size={12}>
            <span>启用 <b style={{ color: 'var(--ok)' }}>{stats.enabled}</b></span>
            <span>停用 <b>{stats.disabled}</b></span>
            <span>共 <b style={{ color: 'var(--ink)' }}>{stats.total}</b></span>
            {stats.critical > 0 && <span>严重启用 <b style={{ color: 'var(--danger)' }}>{stats.critical}</b></span>}
          </Space>
        }
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditingIdx(null)
              form.resetFields()
              setPreview(null)
              setDrawerOpen(true)
            }}
          >
            新增规则
          </Button>
        }
      />

      <FilterBar
        left={
          <>
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              style={{ width: 110 }}
              options={[
                { value: 'all', label: '全部状态' },
                { value: 'enabled', label: '已启用' },
                { value: 'disabled', label: '已停用' },
              ]}
            />
            <Select
              value={severityFilter}
              onChange={setSeverityFilter}
              style={{ width: 110 }}
              options={[
                { value: 'all', label: '全部级别' },
                { value: 'critical', label: 'Critical' },
                { value: 'warning', label: 'Warning' },
                { value: 'info', label: 'Info' },
              ]}
            />
            <Input.Search
              placeholder="搜索名称 / 表达式"
              allowClear
              style={{ width: 200 }}
              onSearch={setSearch}
            />
          </>
        }
        right={
          <Text type="secondary" style={{ fontSize: 12 }}>
            启用优先 · 严重度置顶
          </Text>
        }
      />

      {saveError && (
        <Alert
          type="error"
          showIcon
          message="保存失败"
          description={saveError}
          closable
          style={{ marginBottom: 8 }}
          onClose={() => setSaveError(null)}
        />
      )}

      <MainSplit
        main={
          <Card title={`规则列表 (${displayRules.length})`} size="small">
            {displayRules.length === 0 ? (
              <EmptyState
                kind="no-result"
                title="暂无规则"
                description="换筛选条件，或点右上角「新增规则」配置 PromQL 告警。"
                actionLabel="新增规则"
                onAction={() => {
                  setEditingIdx(null)
                  form.resetFields()
                  setDrawerOpen(true)
                }}
              />
            ) : (
              <Table
                dataSource={displayRules}
                columns={columns}
                rowKey={(record: AlertRuleItem) => record.id || record.name}
                pagination={false}
                size="small"
              />
            )}
          </Card>
        }
        side={
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {(channelsData?.channels || []).length === 0 && (
              <Alert
                type="warning"
                showIcon
                message="未配置通知渠道"
                description="规则可保存，但不会通知到人。"
                action={<Button size="small" onClick={() => window.location.assign('/settings/system')}>去配置</Button>}
              />
            )}

            <SideCard title="说明">
              <Text type="secondary" style={{ fontSize: 12 }}>
                字段契约 name/duration。行内 Switch 即可启停；「测试命中」对表达式即时试跑并列出命中序列。
                悬停表达式可看全文。保存前也可在抽屉里「命中预览」。
              </Text>
            </SideCard>

            {rowTest && (
              <Card size="small" title={`测试命中 · ${rowTest.name}`}>
                {rowTest.loading ? (
                  <Spin size="small" tip="试跑中..." />
                ) : rowTest.result && !rowTest.result.ok ? (
                  <Alert type="error" showIcon message="测试失败" description={rowTest.result.error} />
                ) : rowTest.result ? (
                  <Alert
                    type={rowTest.result.match_count > 0 ? 'success' : 'warning'}
                    showIcon
                    message={`命中 ${rowTest.result.match_count} 条序列`}
                    description={
                      rowTest.result.match_count === 0
                        ? '当前未命中。下一步：检查指标是否存在、阈值是否过严。'
                        : (
                          <Space direction="vertical" size={2} style={{ width: '100%' }}>
                            {rowTest.result.series.slice(0, 8).map((s, i) => (
                              <Text key={i} code style={{ fontSize: 12 }}>
                                {s.instance || '-'} = {s.value || '-'}
                              </Text>
                            ))}
                          </Space>
                        )
                    }
                  />
                ) : null}
              </Card>
            )}

            <SideCard title="快速模板">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                {ruleTemplates.map((t) => (
                  <Button
                    key={t.name}
                    size="small"
                    icon={<ThunderboltOutlined />}
                    onClick={() => {
                      setEditingIdx(null)
                      form.setFieldsValue({ name: t.name, expr: t.expr, duration: t.duration, severity: 'warning', summary: t.name })
                      setDrawerOpen(true)
                    }}
                  >
                    {t.name}
                  </Button>
                ))}
              </Space>
            </SideCard>

            <SideCard title="快捷入口">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/alerts">告警中心</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/settings/system">通知渠道</Button>
                <Button type="link" size="small" icon={<LinkOutlined />} href="/monitoring/metrics">指标查询</Button>
              </Space>
            </SideCard>
          </Space>
        }
      />

      <Drawer
        title={editingIdx === null ? '新增告警规则' : '编辑告警规则'}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={680}
        footer={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button icon={<EyeOutlined />} loading={previewing} onClick={handlePreview}>
              命中预览
            </Button>
            <Button type="primary" icon={<SaveOutlined />} loading={saveMut.isPending} onClick={handleSave}>
              保存
            </Button>
          </Space>
        }
      >
        <Steps
          size="small"
          current={-1}
          items={[
            { title: '指标' },
            { title: '条件' },
            { title: '渠道' },
            { title: '预览' },
          ]}
          style={{ marginBottom: 16 }}
        />
        <Form form={form} layout="vertical">
          <Collapse
            defaultActiveKey={['metric', 'condition', 'notify']}
            ghost
            items={[
              {
                key: 'metric',
                label: '① 指标与名称',
                children: (
                  <>
                    <Form.Item name="name" label="规则名称" rules={[{ required: true, message: '填写规则名称' }]}>
                      <Input placeholder="例如：HighCPU" />
                    </Form.Item>
                    <Form.Item label="快速模板">
                      <Space wrap>
                        {ruleTemplates.map(t => (
                          <Button
                            key={t.name}
                            size="small"
                            icon={<ThunderboltOutlined />}
                            onClick={() => form.setFieldsValue({ name: t.name, expr: t.expr, duration: t.duration })}
                          >
                            {t.name}
                          </Button>
                        ))}
                      </Space>
                    </Form.Item>
                    <Form.Item
                      name="expr"
                      label="PromQL 表达式"
                      rules={[{ required: true, message: '填写 PromQL 表达式' }]}
                      extra="告警触发条件，例如：avg(rate(node_cpu_seconds_total[5m])) > 0.8"
                    >
                      <TextArea rows={3} placeholder="输入 PromQL 表达式" style={{ fontFamily: 'monospace' }} />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: 'condition',
                label: '② 条件与级别',
                children: (
                  <>
                    <Form.Item
                      name="duration"
                      label="持续时间"
                      rules={[{ required: true, message: '填写持续时间' }]}
                      extra="条件持续多久后触发告警，例如：5m"
                      initialValue="5m"
                    >
                      <Input placeholder="例如：5m, 10m, 1h" />
                    </Form.Item>
                    <Form.Item name="severity" label="严重程度" rules={[{ required: true }]} initialValue="warning">
                      <Select
                        options={[
                          { value: 'info', label: 'Info' },
                          { value: 'warning', label: 'Warning' },
                          { value: 'critical', label: 'Critical' },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item name="host_ids" label="限定主机" extra="留空表示全部主机；选择后评估时自动过滤 instance">
                      <Select
                        mode="multiple"
                        allowClear
                        placeholder="选择主机"
                        optionFilterProp="label"
                        options={hostOptions}
                      />
                    </Form.Item>
                    <Form.Item name="summary" label="摘要" rules={[{ required: true, message: '填写摘要' }]}>
                      <Input placeholder="告警摘要，支持 {{ $labels.instance }}" />
                    </Form.Item>
                    <Form.Item name="description" label="详细描述">
                      <TextArea rows={3} placeholder="告警详细描述" />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: 'notify',
                label: '③ 通知渠道',
                children: (
                  <>
                    <Form.Item
                      name="channel_ids"
                      label="通知渠道"
                      extra="告警触发/恢复时推送到这些 Webhook 渠道；保存时会保留已有绑定"
                    >
                      <Select
                        mode="multiple"
                        allowClear
                        placeholder="选择通知渠道"
                        optionFilterProp="label"
                        options={channelOptions}
                        notFoundContent="尚未配置渠道，请到系统设置添加"
                      />
                    </Form.Item>
                    <Form.Item
                      name="im_user_ids"
                      label="飞书通知人"
                      extra="告警触发/恢复时在飞书群里 @ 这些用户（需对方完成「个人 IM 通知」绑定）"
                    >
                      <Select
                        mode="multiple"
                        allowClear
                        placeholder="选择通知人"
                        optionFilterProp="label"
                        options={(imUsers ?? []).map((u) => ({
                          value: u.id,
                          label: u.name || u.username,
                        }))}
                      />
                    </Form.Item>
                    <Form.Item name="group" label="分组标签">
                      <Input placeholder="可选，用于规则归类筛选" maxLength={60} />
                    </Form.Item>
                    <Form.Item name="enabled" label="启用规则" valuePropName="checked" initialValue={true}>
                      <Switch />
                    </Form.Item>
                  </>
                ),
              },
            ]}
          />

          <Divider />
          <div>
            <Text strong>④ 命中预览</Text>
            <div style={{ marginTop: 8 }}>
              {previewing && <Spin size="small" tip="试跑中..." />}
              {!previewing && !preview && (
                <Text type="secondary">保存前点「命中预览」，用当前 expression 即时评估并列出命中序列。</Text>
              )}
              {!previewing && preview && !preview.ok && (
                <Alert type="error" showIcon message="预览失败" description={preview.error || '表达式无法评估'} />
              )}
              {!previewing && preview && preview.ok && (
                <Alert
                  type={preview.match_count > 0 ? 'success' : 'warning'}
                  showIcon
                  message={`命中 ${preview.match_count} 条序列${preview.match_count > 50 ? '（仅展示前 50）' : ''}`}
                  description={
                    preview.match_count === 0
                      ? '当前表达式未返回样本。下一步：检查指标是否存在、阈值是否过严，或去掉主机过滤。'
                      : (
                        <Space direction="vertical" size={2} style={{ width: '100%' }}>
                          {preview.series.slice(0, 10).map((s, i) => (
                            <Text key={i} code style={{ fontSize: 12 }}>
                              {s.instance || '-'} = {s.value || '-'}
                            </Text>
                          ))}
                        </Space>
                      )
                  }
                />
              )}
            </div>
          </div>
        </Form>
      </Drawer>
    </div>
  )
}
