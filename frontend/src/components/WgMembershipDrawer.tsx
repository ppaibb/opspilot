/**
 * 主机虚拟组网抽屉 —— CMDB 主机 × WireGuard 联动展示。
 *
 * 在主机列表「更多」菜单打开，展示该主机参与的组网：
 * 所属组网（名称/网段/端点/模式）、隧道 IP、启停状态；
 * 提供「前往组网管理」跳转（保留组网 id 便于定位）。
 */
import { useQuery } from '@tanstack/react-query'
import { Drawer, Empty, List, Tag, Typography, Button, Space, Tooltip, Badge } from 'antd'
import { LinkOutlined, ArrowRightOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { wireguardApi } from '@/api'
import { usePermission } from '@/hooks/usePermission'

const { Text } = Typography

interface Membership {
  peer: {
    id: number
    name: string
    allowed_ip: string
    public_key: string
    enabled: boolean
  }
  network: {
    id: number
    name: string
    cidr: string
    endpoint: string
    port: number
    full_tunnel: boolean
  }
}

interface Props {
  open: boolean
  assetId: string
  hostName?: string
  onClose: () => void
}

export default function WgMembershipDrawer({ open, assetId, hostName, onClose }: Props) {
  const navigate = useNavigate()
  const { has } = usePermission()
  const canView = has('module.wireguard.manage')

  const { data, isLoading } = useQuery({
    queryKey: ['wg-by-asset', assetId],
    queryFn: () => wireguardApi.byAsset(assetId),
    enabled: open && !!assetId && canView,
  })

  // byAsset 返回 Record<string, unknown>，此处按后端 /wireguard/by-asset/{assetId} 契约收窄
  const memberships = (data as { memberships?: Membership[] } | undefined)?.memberships ?? []

  return (
    <Drawer
      title={
        <Space>
          <LinkOutlined style={{ color: '#1677ff' }} />
          虚拟组网 · {hostName || assetId}
        </Space>
      }
      open={open}
      onClose={onClose}
      width={560}
      destroyOnClose
    >
      {!canView ? (
        <Empty description="无虚拟组网查看权限" style={{ padding: '60px 0' }} />
      ) : isLoading ? (
        <div style={{ textAlign: 'center', padding: '60px 0' }}>加载中...</div>
      ) : memberships.length === 0 ? (
        <Empty
          description={
            <span>
              该主机未加入任何组网
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>可在「资产管理 → 虚拟组网」添加为成员</Text>
            </span>
          }
          style={{ padding: '60px 0' }}
        >
          <Button type="primary" onClick={() => navigate('/assets/wireguard')}>前往组网管理</Button>
        </Empty>
      ) : (
        <List
          dataSource={memberships}
          renderItem={(item) => (
            <List.Item
              actions={[
                <Button
                  key="go" size="small" type="link" icon={<ArrowRightOutlined />}
                  onClick={() => navigate('/assets/wireguard')}
                >
                  管理
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={
                  <Space size={6}>
                    <Text strong>{item.network.name}</Text>
                    <Tag color={item.network.full_tunnel ? 'orange' : 'green'}>
                      {item.network.full_tunnel ? '全隧道' : '分流'}
                    </Tag>
                    {!item.peer.enabled && <Tag>已禁用</Tag>}
                  </Space>
                }
                description={
                  <Space direction="vertical" size={2}>
                    <Text code>{item.peer.allowed_ip}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      出口 {item.network.endpoint}:{item.network.port} · 网段 {item.network.cidr}
                    </Text>
                    <Tooltip title="成员公钥">
                      <Text type="secondary" style={{ fontSize: 11 }} ellipsis>
                        {item.peer.public_key}
                      </Text>
                    </Tooltip>
                  </Space>
                }
              />
              <Badge status={item.peer.enabled ? 'success' : 'default'} text={item.peer.enabled ? '启用' : '禁用'} />
            </List.Item>
          )}
        />
      )}
    </Drawer>
  )
}
