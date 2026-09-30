import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App, Button, Card, InputNumber, Space, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CheckCircleOutlined, CloseCircleOutlined, ClockCircleOutlined,
  ReloadOutlined, SaveOutlined,
} from '@ant-design/icons'
import { systemApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { ScheduledTaskStatus } from '@/types/api'

const { Text } = Typography

type Draft = Record<string, { enabled: boolean; interval_seconds: number }>

function formatInterval(seconds: number): string {
  if (seconds >= 86400 && seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} 小时`
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
}

function statusTag(row: ScheduledTaskStatus) {
  if (!row.enabled) return <Tag>已停用</Tag>
  if (row.last_status === 'ok') {
    return <Tag color="success" icon={<CheckCircleOutlined />}>正常</Tag>
  }
  if (row.last_status === 'error') {
    return <Tag color="error" icon={<CloseCircleOutlined />}>最近失败</Tag>
  }
  return <Tag icon={<ClockCircleOutlined />}>等待首轮</Tag>
}

export default function ScheduledTasksPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>({})
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['settings', 'scheduled-tasks'],
    queryFn: systemApi.scheduledTasks,
    refetchInterval: 15_000,
  })
  const tasks = useMemo(() => data?.tasks ?? [], [data?.tasks])

  useEffect(() => {
    if (!tasks.length) return
    // 草稿初始化延后到微任务，避免在 effect 内同步 setState 触发级联渲染
    queueMicrotask(() => {
      setDraft((current) => {
        if (Object.keys(current).length) return current
        return Object.fromEntries(tasks.map((task) => [task.key, {
          enabled: task.enabled,
          interval_seconds: task.interval_seconds,
        }]))
      })
    })
  }, [tasks])

  const dirty = tasks.some((task) => {
    const item = draft[task.key]
    return Boolean(item && (
      item.enabled !== task.enabled || item.interval_seconds !== task.interval_seconds
    ))
  })

  const saveMutation = useMutation({
    mutationFn: () => systemApi.updateScheduledTasks(draft),
    onSuccess: (result) => {
      queryClient.setQueryData(['settings', 'scheduled-tasks'], result)
      setDraft({})
      message.success('定时任务配置已保存')
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const updateDraft = (key: string, patch: Partial<Draft[string]>) => {
    const source = draft[key] ?? tasks.find((task) => task.key === key)
    if (!source) return
    setDraft((current) => ({
      ...current,
      [key]: {
        enabled: source.enabled,
        interval_seconds: source.interval_seconds,
        ...patch,
      },
    }))
  }

  const columns: ColumnsType<ScheduledTaskStatus> = [
    {
      title: '任务',
      dataIndex: 'name',
      width: 300,
      render: (name: string, row) => (
        <Space direction="vertical" size={0} style={{ maxWidth: '100%' }}>
          <Text strong>{name}</Text>
          <Text type="secondary" ellipsis={{ tooltip: row.description }}>
            {row.description}
          </Text>
        </Space>
      ),
    },
    {
      title: '状态',
      width: 130,
      render: (_, row) => {
        const enabled = draft[row.key]?.enabled ?? row.enabled
        return (
          <Space size={8}>
            <Switch
              size="small"
              checked={enabled}
              onChange={(value) => updateDraft(row.key, { enabled: value })}
            />
            <Tooltip title={row.last_error || undefined}>{statusTag({ ...row, enabled })}</Tooltip>
          </Space>
        )
      },
    },
    {
      title: '执行周期',
      width: 180,
      render: (_, row) => (
        <Space size={6}>
          <InputNumber
            min={row.min_interval_seconds}
            max={row.max_interval_seconds}
            precision={0}
            controls
            value={draft[row.key]?.interval_seconds ?? row.interval_seconds}
            onChange={(value) => {
              if (value != null) updateDraft(row.key, { interval_seconds: value })
            }}
            style={{ width: 112 }}
          />
          <Text type="secondary">秒</Text>
        </Space>
      ),
    },
    {
      title: '最近执行',
      dataIndex: 'last_run_at',
      width: 170,
      render: (value: string) => value || <Text type="secondary">尚未执行</Text>,
    },
    {
      title: '下次执行',
      dataIndex: 'next_run_at',
      width: 170,
      render: (value: string, row) => {
        const enabled = draft[row.key]?.enabled ?? row.enabled
        return enabled ? (value || <Text type="secondary">首轮等待中</Text>) : '-'
      },
    },
    {
      title: '执行次数',
      dataIndex: 'run_count',
      width: 100,
      render: (value: number, row) => (
        <Tooltip title={row.error_count ? `失败 ${row.error_count} 次` : undefined}>
          <span>{value} 次{row.error_count ? ` / ${row.error_count} 次失败` : ''}</span>
        </Tooltip>
      ),
    },
    {
      title: '范围',
      width: 150,
      render: (_, row) => (
        <Tooltip title={`默认 ${formatInterval(row.default_interval_seconds)}`}>
          <Text type="secondary">
            {formatInterval(row.min_interval_seconds)} ～ {formatInterval(row.max_interval_seconds)}
          </Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <Card
      title="定时任务"
      styles={{ body: { padding: 0 } }}
      extra={(
        <Space size={8}>
          <Tooltip title="刷新状态">
            <Button
              aria-label="刷新状态"
              icon={<ReloadOutlined spin={isFetching} />}
              onClick={() => refetch()}
            />
          </Tooltip>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            disabled={!dirty}
            loading={saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            保存
          </Button>
        </Space>
      )}
    >
      {isError ? (
        <div style={{ padding: 24 }}>
          <Text type="danger">定时任务状态读取失败</Text>
        </div>
      ) : (
        <Table<ScheduledTaskStatus>
          rowKey="key"
          loading={isLoading}
          columns={columns}
          dataSource={tasks}
          pagination={false}
          size="middle"
          scroll={{ x: 1200 }}
        />
      )}
    </Card>
  )
}
