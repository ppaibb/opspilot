import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Card, Table, Button, Space, Tag, Drawer, List, Typography, Empty, Flex, Input,
} from 'antd'
import {
  PlusOutlined, HistoryOutlined, SearchOutlined, EyeOutlined, RollbackOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import client from '@/api/client'
import ConfigDiffViewer from '@/components/ConfigDiffViewer'

const { Text } = Typography

interface ConfigItem {
  config_id: string
  key: string
  value: string
  value_type: string
  env: string
  credential_ref?: string
  remark?: string
  updated_at: string
}

interface ConfigRevision {
  id: number
  change_type: string
  old_value?: string
  new_value?: string
  operator: string
  created_at: string
  remark?: string
}

export default function AppConfigPage() {
  const { appId } = useParams<{ appId: string }>()
  const [search, setSearch] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [selectedConfigId, setSelectedConfigId] = useState<string | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareRevisionId, setCompareRevisionId] = useState<number | null>(null)

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['application-configs', appId, search],
    queryFn: () =>
      client.get<ConfigItem[]>(`/applications/${appId}/configs`, { params: { q: search } })
        .then((res) => res.data),
  })

  const { data: revisions = [] } = useQuery({
    queryKey: ['config-revisions', appId, selectedConfigId],
    queryFn: () =>
      client
        .get<ConfigRevision[]>(`/applications/${appId}/configs/${selectedConfigId}/revisions`)
        .then((res) => res.data),
    enabled: !!selectedConfigId && historyOpen,
  })

  const showHistory = (configId: string) => {
    setSelectedConfigId(configId)
    setHistoryOpen(true)
  }

  const showCompare = (revisionId: number) => {
    setCompareRevisionId(revisionId)
    setCompareOpen(true)
    setHistoryOpen(false)
  }

  const columns: ColumnsType<ConfigItem> = [
    {
      title: '配置键',
      dataIndex: 'key',
      width: 200,
      render: (key) => <Text strong>{key}</Text>,
    },
    {
      title: '环境',
      dataIndex: 'env',
      width: 100,
      render: (env) => <Tag color={env === 'prod' ? 'red' : env === 'test' ? 'blue' : 'default'}>{env}</Tag>,
    },
    {
      title: '值类型',
      dataIndex: 'value_type',
      width: 100,
      render: (type) => <Tag>{type}</Tag>,
    },
    {
      title: '配置值',
      dataIndex: 'value',
      ellipsis: true,
      render: (value, record) => {
        if (record.value_type === 'credential_ref' && record.credential_ref) {
          return <Text type="secondary">凭证引用: {record.credential_ref}</Text>
        }
        return (
          <Text
            ellipsis={{ tooltip: value }}
            style={{
              maxWidth: 300,
              display: 'inline-block',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {value}
          </Text>
        )
      },
    },
    {
      title: '备注',
      dataIndex: 'remark',
      ellipsis: true,
      render: (remark) => remark || '-',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (time) => time || '-',
    },
    {
      title: '操作',
      width: 120,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<HistoryOutlined />}
            onClick={() => showHistory(record.config_id)}
          >
            历史
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap={16} style={{ padding: 24 }}>
      <Flex justify="space-between" align="center">
        <Space>
          <Text strong style={{ fontSize: 18 }}>配置管理</Text>
          <Tag color="blue">{configs.length} 个配置项</Tag>
        </Space>
        <Button type="primary" icon={<PlusOutlined />}>
          新建配置
        </Button>
      </Flex>

      <Card size="small">
        <Input
          placeholder="搜索配置键、值或备注"
          prefix={<SearchOutlined />}
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 300 }}
        />
      </Card>

      <Card size="small" styles={{ body: { padding: 0 } }}>
        <Table
          dataSource={configs}
          columns={columns}
          rowKey="config_id"
          size="small"
          loading={isLoading}
          pagination={{
            pageSize: 20,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 个配置`,
          }}
        />
      </Card>

      {/* 历史版本抽屉 */}
      <Drawer
        title="配置历史版本"
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        width={600}
      >
        {revisions.length === 0 ? (
          <Empty description="暂无历史版本" />
        ) : (
          <List
            dataSource={revisions}
            renderItem={(item: ConfigRevision) => (
              <List.Item
                actions={[
                  <Button
                    type="link"
                    size="small"
                    icon={<EyeOutlined />}
                    onClick={() => showCompare(item.id)}
                  >
                    对比
                  </Button>,
                  <Button
                    type="link"
                    size="small"
                    icon={<RollbackOutlined />}
                    onClick={() => {
                      setCompareRevisionId(item.id)
                      setCompareOpen(true)
                      setHistoryOpen(false)
                    }}
                  >
                    回滚
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Text strong>版本 {item.id}</Text>
                      <Tag color={
                        item.change_type === 'create' ? 'green' :
                        item.change_type === 'update' ? 'blue' :
                        item.change_type === 'rollback' ? 'orange' :
                        'default'
                      }>
                        {item.change_type}
                      </Tag>
                    </Space>
                  }
                  description={
                    <Space direction="vertical" size={2}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {item.created_at} | {item.operator}
                      </Text>
                      {item.remark && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {item.remark}
                        </Text>
                      )}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Drawer>

      {/* 配置对比抽屉 */}
      {compareRevisionId && selectedConfigId && (
        <ConfigDiffViewer
          open={compareOpen}
          onClose={() => {
            setCompareOpen(false)
            setHistoryOpen(true)
          }}
          appId={appId!}
          configId={selectedConfigId}
          targetRevisionId={compareRevisionId}
        />
      )}
    </Flex>
  )
}
