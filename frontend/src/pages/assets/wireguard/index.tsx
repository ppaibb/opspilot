/**
 * 虚拟组网管理页（/assets/wireguard）—— WireGuard 隧道编排。
 *
 * 结构：组网卡片列表 → 点击展开成员管理抽屉（对标 wg-easy 的客户端管理）。
 *
 * 功能：
 * 1. 组网 CRUD（创建时后端自动生成服务端密钥并保留网段 .1 给出口节点）；
 * 2. 成员：添加（自动/手工分配隧道 IP）、启停、删除；
 * 3. 配置出口：服务端 conf、成员 conf + 二维码（移动端扫码即用）、
 *    一键安装脚本（可粘贴到「脚本执行」批量下发）。
 *
 * 安全约定：接口永不回传私钥明文；配置/二维码即凭据，页面提供下载而非长期展示。
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Row, Col, Button, Space, Tag, Typography, Flex, Modal, Drawer, Form, Input, InputNumber,
  Switch, Select, Table, App, Tooltip, Popconfirm, Empty, Spin, Descriptions, Image, Alert, Badge, Tabs,
  Dropdown,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  PlusOutlined, ReloadOutlined, LinkOutlined, DownloadOutlined, QrcodeOutlined, SettingOutlined,
  PauseCircleOutlined, PlayCircleOutlined, DeleteOutlined, ApiOutlined, GlobalOutlined, CopyOutlined,
  SyncOutlined, RocketOutlined, MoreOutlined,
} from '@ant-design/icons'
import { assetsApi, toolsApi, wireguardApi } from '@/api'
import { usePermission, useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'
import { useCrudResource } from '@/hooks/useCrudResource'
import type { WgNetwork, WgNetworkPayload, WgPeer, WgPeerPayload } from '@/types/api'
import type { MessageInstance } from 'antd/es/message/interface'

const { Text } = Typography

/** wg show 状态回读的 peer 行（GET /wireguard/networks/{id}/status，后端 SSH 采集）。 */
interface WgPeerStatus {
  peer_id: number | string
  online?: boolean
  latest_handshake_text?: string
  rx_bytes?: number
  tx_bytes?: number
  [key: string]: unknown
}

/** 主机资产行（实例扫描纳管后可能附带 instance_id 关联字段）。 */
type AssetRow = import('@/types/api').AssetResponse & { instance_id?: string }

/** 复制文本到剪贴板并提示（脚本/配置分发的统一出口）。 */
function useCopy(message: MessageInstance) {
  return (text: string, tip = '已复制到剪贴板') => {
    navigator.clipboard.writeText(text).then(
      () => message.success(tip),
      () => message.error('复制失败，请手动选择复制'),
    )
  }
}

// ───────────────────────── 组网创建/编辑抽屉 ─────────────────────────

function NetworkDrawer({ open, editing, onClose }: { open: boolean; editing: WgNetwork | null; onClose: () => void }) {
  const [form] = Form.useForm()

  const { data: assets = [] } = useQuery({
    queryKey: ['hosts'],
    queryFn: assetsApi.list,
    enabled: open,
  })

  const { saveMut: saveMutation } = useCrudResource<WgNetworkPayload, number, Partial<WgNetworkPayload>>({
    queryKey: ['wg-networks'],
    entityName: '组网',
    createFn: wireguardApi.createNetwork,
    updateFn: wireguardApi.updateNetwork,
    deleteFn: wireguardApi.deleteNetwork,
    onSaved: () => onClose(),
  })

  // 选定出口节点后自动回填 endpoint（可手改）。
  const handleServerPick = (assetId: string) => {
    const asset = (assets as AssetRow[]).find((a) => a.id === assetId || a.instance_id === assetId)
    form.setFieldsValue({
      server_asset_id: assetId,
      server_name: asset?.name || '',
      endpoint: asset?.ip || form.getFieldValue('endpoint'),
    })
  }

  return (
    <Drawer
      title={editing ? `编辑组网 - ${editing.name}` : '创建组网'}
      open={open}
      onClose={onClose}
      width={560}
      destroyOnClose
      extra={<Button type="primary" loading={saveMutation.isPending} onClick={() => form.submit()}>保存</Button>}
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(values) =>
          saveMutation.mutate(editing ? { id: editing.id, data: values } : { data: values })
        }
        initialValues={{ port: 51820, forward_iface: 'eth0', full_tunnel: false }}
      >
        <Form.Item name="name" label="组网名称" rules={[{ required: true, message: '请输入名称' }]}>
          <Input placeholder="生产-跨机房内网" />
        </Form.Item>
        <Form.Item
          name="cidr" label="隧道网段"
          rules={[
            { required: true, message: '请输入网段' },
            { pattern: /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/, message: '格式如 10.8.0.0/24' },
          ]}
          extra="创建后不可修改；.1 自动保留给出口节点"
        >
          <Input placeholder="10.8.0.0/24" disabled={!!editing} style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        <Form.Item label="出口节点（服务器端）" extra={editing ? '编辑模式不更换出口节点' : '从主机资产选择，自动带出 IP 作为端点'}>
          <Select
            showSearch
            optionFilterProp="label"
            placeholder="选择主机资产"
            value={Form.useWatch('server_asset_id', form) || undefined}
            onChange={handleServerPick}
            options={assets.map((a) => ({ value: a.id, label: `${a.name}（${a.ip}）` }))}
          />
        </Form.Item>
        <Form.Item name="server_name" hidden><Input /></Form.Item>
        <Form.Item
          name="endpoint" label="对端可达地址（Endpoint）"
          rules={[{ required: true, message: '请输入出口节点的公网地址或域名' }]}
          extra="成员从该地址接入隧道；选出口节点后自动带出，可改为域名"
        >
          <Input placeholder="203.0.113.10 或 wg.example.com" />
        </Form.Item>
        <Space size="large" style={{ display: 'flex' }}>
          <Form.Item name="port" label="监听端口" rules={[{ required: true }]}>
            <InputNumber min={1024} max={65535} style={{ width: 130 }} />
          </Form.Item>
          <Form.Item name="forward_iface" label="出口网卡" extra="MASQUERADE 用">
            <Input style={{ width: 130 }} placeholder="eth0" />
          </Form.Item>
        </Space>
        <Form.Item name="full_tunnel" label="成员流量模式" valuePropName="checked"
          extra="关闭=分流（仅隧道网段走 WG）；开启=全隧道（成员全部流量经出口节点出公网）">
          <Switch checkedChildren="全隧道" unCheckedChildren="分流" />
        </Form.Item>
        <Form.Item name="dns" label="成员 DNS（可选）">
          <Input placeholder="1.1.1.1, 10.8.0.1" />
        </Form.Item>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} placeholder="用途、负责人等" />
        </Form.Item>
      </Form>
    </Drawer>
  )
}

// ───────────────────────── 成员配置弹窗（配置 + 二维码 + 脚本） ─────────────────────────

function PeerConfigModal({ peerId, onClose }: { peerId: number | null; onClose: () => void }) {
  const { message } = App.useApp()
  const copy = useCopy(message)

  const { data: bundle, isLoading } = useQuery({
    queryKey: ['wg-peer-bundle', peerId],
    queryFn: () => wireguardApi.peerBundle(peerId!),
    enabled: !!peerId,
  })

  // 脚本执行深度集成：成员关联了 CMDB 主机时，直接通过 Ansible shell 通道下发安装。
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canExecute = has('action.tools.scripts.execute')
  const installMutation = useMutation({
    mutationFn: () =>
      toolsApi.runTask({
        asset_ids: [bundle!.peer.asset_id],
        module: 'shell',
        module_args: bundle!.install_script,
      }),
    onSuccess: (result) => {
      if (result?.ok) message.success(`远程安装完成（${result.target_count} 台，耗时 ${(result.duration_ms / 1000).toFixed(1)}s），可刷新状态验证握手`)
      else message.warning(`执行退出码 ${result?.code}，详见输出`)
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '远程安装失败（检查主机 SSH 与 Ansible 配置）'),
  })

  const download = () => {
    if (!bundle) return
    const blob = new Blob([bundle.config], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${bundle.peer.name || `peer-${peerId}`}.conf`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <Modal
      title={`成员配置 - ${bundle?.peer.name || ''}`}
      open={!!peerId}
      onCancel={onClose}
      width={760}
      footer={
        <Space>
          {bundle?.peer.asset_id && (
            <Tooltip title="通过 Ansible shell 通道把安装脚本下发到该成员主机（高危确认）">
              <Button type="primary" ghost icon={<RocketOutlined />}
                loading={installMutation.isPending}
                disabled={!canExecute}
                onClick={() => riskConfirm.confirm('scripts:execute', () => installMutation.mutate())}>
                远程安装到主机
              </Button>
            </Tooltip>
          )}
          <Button icon={<DownloadOutlined />} onClick={download} disabled={!bundle}>下载 .conf</Button>
          <Button icon={<CopyOutlined />} disabled={!bundle} onClick={() => copy(bundle!.config, '配置已复制')}>复制配置</Button>
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
    >
      {isLoading || !bundle ? (
        <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="生成配置..." /></div>
      ) : (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Alert
            type="warning" showIcon
            message="配置即凭据：包含私钥，请勿明文传播；二维码仅展示给需要扫码的成员本人。"
          />
          <Row gutter={16}>
            <Col span={10} style={{ textAlign: 'center' }}>
              <Image
                src={bundle.qr}
                alt="WireGuard 配置二维码"
                width={220}
                style={{ border: '1px solid #f0f0f0', borderRadius: 8 }}
                preview={false}
              />
              <div><Text type="secondary" style={{ fontSize: 12 }}>移动端 WireGuard App 扫码导入</Text></div>
            </Col>
            <Col span={14}>
              <Tabs size="small" defaultActiveKey="config" items={[
                {
                  key: 'config',
                  label: '配置',
                  children: (
                    <pre style={{
                      background: '#fafafa', padding: 8, borderRadius: 6, fontSize: 11,
                      whiteSpace: 'pre-wrap', maxHeight: 300, overflow: 'auto',
                    }}>{bundle.config}</pre>
                  ),
                },
                {
                  key: 'script',
                  label: '安装脚本',
                  children: (
                    <>
                      <Alert type="info" showIcon style={{ marginBottom: 8 }}
                        message="粘贴到「运维工具 → 脚本执行」（shell 模块）即可远程安装。" />
                      <Button size="small" icon={<CopyOutlined />}
                        onClick={() => copy(bundle.install_script, '安装脚本已复制')} style={{ marginBottom: 8 }}>
                        复制脚本
                      </Button>
                      <pre style={{
                        background: '#fafafa', padding: 8, borderRadius: 6, fontSize: 11,
                        whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto',
                      }}>{bundle.install_script}</pre>
                    </>
                  ),
                },
              ]} />
            </Col>
          </Row>
        </Space>
      )}
    </Modal>
  )
}

// ───────────────────────── 成员管理抽屉 ─────────────────────────

function PeersDrawer({ network, onClose }: { network: WgNetwork | null; onClose: () => void }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.wireguard.manage')
  const [addOpen, setAddOpen] = useState(false)
  const [addForm] = Form.useForm()
  const [configPeerId, setConfigPeerId] = useState<number | null>(null)

  const { data: peers = [], isLoading } = useQuery({
    queryKey: ['wg-peers', network?.id],
    queryFn: () => wireguardApi.listPeers(network!.id),
    enabled: !!network,
  })

  // 实时状态：SSH 回读 wg show（出口节点未配置/不可达时优雅降级为提示）。
  const { data: status, isLoading: loadingStatus, refetch: refetchStatus } = useQuery({
    queryKey: ['wg-network-status', network?.id],
    queryFn: () => wireguardApi.networkStatus(network!.id),
    enabled: !!network,
    refetchOnWindowFocus: false,
  })
  const statusMap = useMemo(() => {
    if (!status?.available) return {} as Record<string, WgPeerStatus>
    return Object.fromEntries(((status.peers || []) as WgPeerStatus[]).map((p) => [String(p.peer_id), p]))
  }, [status])

  const { data: assets = [] } = useQuery({
    queryKey: ['hosts'],
    queryFn: assetsApi.list,
    enabled: addOpen,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['wg-peers', network?.id] })

  const addMutation = useMutation({
    mutationFn: (values: WgPeerPayload) => wireguardApi.createPeer(network!.id, values),
    onSuccess: () => { message.success('成员已添加（密钥已生成）'); setAddOpen(false); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '添加失败'),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ peer, enabled }: { peer: WgPeer; enabled: boolean }) =>
      wireguardApi.updatePeer(peer.id, { enabled }),
    onSuccess: () => { message.success('状态已更新；请重新下载服务端配置并重载生效'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '操作失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: (peer: WgPeer) => wireguardApi.deletePeer(peer.id),
    onSuccess: () => { message.success('成员已删除；请重新下载服务端配置并重载生效'); invalidate() },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '删除失败'),
  })

  const assetName = (assetId: string) => {
    if (!assetId) return <Text type="secondary">手工登记</Text>
    const asset = (assets as AssetRow[]).find((a) => a.id === assetId || a.instance_id === assetId)
    return asset ? `${asset.name}（${asset.ip}）` : assetId
  }

  const columns: ColumnsType<WgPeer> = [
    {
      title: '成员', dataIndex: 'name', width: 170,
      render: (v: string, record) => (
        <Space size={6}>
          <Text strong>{v}</Text>
          {record.enabled
            ? <Tag color="green">启用</Tag>
            : <Tag>已禁用</Tag>}
        </Space>
      ),
    },
    { title: '隧道 IP', dataIndex: 'allowed_ip', width: 130, render: (v: string) => <Text code>{v}</Text> },
    {
      title: '在线', key: 'online', width: 90,
      filters: undefined,
      render: (_: unknown, record: WgPeer) => {
        if (!status?.available) return <Tooltip title={status?.message || '状态未回读'}><Text type="secondary">-</Text></Tooltip>
        const s = statusMap[record.id]
        if (!s) return <Tooltip title="出口节点上无此 peer（配置未同步？）"><Badge status="default" text="未注册" /></Tooltip>
        return (
          <Tooltip title={`最近握手 ${s.latest_handshake_text} · ↓${(Number(s.rx_bytes) / 1024).toFixed(0)}KB ↑${(Number(s.tx_bytes) / 1024).toFixed(0)}KB`}>
            <Badge status={s.online ? 'success' : 'error'} text={s.online ? '在线' : '离线'} />
          </Tooltip>
        )
      },
    },
    { title: '关联资产', dataIndex: 'asset_id', width: 190, ellipsis: true, render: (v: string) => assetName(v) },
    { title: '备注', dataIndex: 'remark', ellipsis: true, render: (v: string) => v || '-' },
    {
      title: '操作', key: 'ops', width: 140, fixed: 'right',
      render: (_, record) => {
        const moreItems: MenuProps['items'] = [
          { key: 'delete', icon: <DeleteOutlined />, label: '删除成员', danger: true },
        ]
        const onMore: MenuProps['onClick'] = ({ key }) => {
          if (key !== 'delete') return
          Modal.confirm({
            title: `删除成员 ${record.name}`,
            content: '成员配置将随之失效，确定继续？',
            okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
            onOk: () => riskConfirm.confirm('wireguard:delete', () => deleteMutation.mutate(record)),
          })
        }
        return (
          <Space size={4}>
            <Button size="small" type="primary" ghost icon={<QrcodeOutlined />}
              onClick={() => setConfigPeerId(record.id)}>
              配置
            </Button>
            {record.enabled ? (
              <Tooltip title="禁用（服务端配置移除该 Peer 段）">
                <Button size="small" type="text" icon={<PauseCircleOutlined />} disabled={!canManage}
                  onClick={() => toggleMutation.mutate({ peer: record, enabled: false })} />
              </Tooltip>
            ) : (
              <Tooltip title="启用">
                <Button size="small" type="text" icon={<PlayCircleOutlined />} disabled={!canManage}
                  onClick={() => toggleMutation.mutate({ peer: record, enabled: true })} />
              </Tooltip>
            )}
            <Dropdown
              menu={{ items: moreItems, onClick: onMore }}
              trigger={['click']}
              disabled={!canManage}
            >
              <Button size="small" type="text" icon={<MoreOutlined />} disabled={!canManage} />
            </Dropdown>
          </Space>
        )
      },
    },
  ]

  return (
    <Drawer
      title={
        <Space>
          <span>成员管理</span>
          <Text code>{network?.name}</Text>
          <Tag>{network?.cidr}</Tag>
        </Space>
      }
      open={!!network}
      onClose={onClose}
      width={860}
      extra={
        <Space>
          <Button size="small" icon={<PlusOutlined />} type="primary" disabled={!canManage}
            onClick={() => { addForm.resetFields(); setAddOpen(true) }}>
            添加成员
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Card size="small" title="服务端配置（出口节点执行）">
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Descriptions size="small" column={3}>
              <Descriptions.Item label="端点">{network?.endpoint}:{network?.port}</Descriptions.Item>
              <Descriptions.Item label="隧道 IP（出口）"><Text code>{network?.cidr.split('/')[0].replace(/\.0$/, '.1')}</Text></Descriptions.Item>
              <Descriptions.Item label="模式">{network?.full_tunnel ? '全隧道' : '分流'}</Descriptions.Item>
            </Descriptions>
            <Space>
              <Button size="small" icon={<DownloadOutlined />}
                onClick={() => window.open(`/api/wireguard/networks/${network?.id}/server-config?download=1`)}>
                下载 wg0.conf
              </Button>
              <ServerScriptButton networkId={network?.id || 0} />
              <Tooltip title="SSH 到出口节点回读 wg show（握手/流量）">
                <Button size="small" icon={<SyncOutlined />} loading={loadingStatus} onClick={() => refetchStatus()}>
                  刷新状态
                </Button>
              </Tooltip>
            </Space>
            {status && !status.available && (
              <Alert type="info" showIcon style={{ marginTop: 4 }} message={status.message} />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              成员启停/增删后需重新下载本配置并在出口节点重载（systemctl restart wg-quick@wg0）。
            </Text>
          </Space>
        </Card>

        <Table<WgPeer>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={peers}
          loading={isLoading}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有成员，点右上角「添加成员」" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        />
      </Space>

      <Modal
        title="添加成员"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => addForm.submit()}
        okText="添加（自动生成密钥）" cancelText="取消"
        confirmLoading={addMutation.isPending}
      >
        <Form form={addForm} layout="vertical" onFinish={(values) => addMutation.mutate(values)}>
          <Form.Item name="name" label="成员名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="上海节点-A / iPhone-运维" />
          </Form.Item>
          <Form.Item name="asset_id" label="关联主机资产（可选）" extra="手工登记的移动端/外部机器可不选">
            <Select
              allowClear placeholder="选择 CMDB 主机"
              showSearch optionFilterProp="label"
              options={assets.map((a) => ({ value: a.id, label: `${a.name}（${a.ip}）` }))}
            />
          </Form.Item>
          <Form.Item name="allowed_ip" label="隧道 IP（留空自动分配）">
            <Input placeholder={`自动分配 ${network?.cidr} 中的下一个空闲地址`} style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input placeholder="用途/负责人" />
          </Form.Item>
        </Form>
      </Modal>

      <PeerConfigModal peerId={configPeerId} onClose={() => setConfigPeerId(null)} />
    </Drawer>
  )
}

/** 服务端安装脚本按钮：拉取脚本 → Modal 展示 + 复制。 */
function ServerScriptButton({ networkId }: { networkId: number }) {
  const { message } = App.useApp()
  const copy = useCopy(message)
  const [open, setOpen] = useState(false)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['wg-server-script', networkId],
    queryFn: () => wireguardApi.serverInstallScript(networkId),
    enabled: open,
  })

  return (
    <>
      <Button size="small" icon={<ApiOutlined />} onClick={() => { setOpen(true); refetch() }}>
        生成安装脚本
      </Button>
      <Modal
        title="出口节点安装脚本"
        open={open}
        onCancel={() => setOpen(false)}
        width={720}
        footer={
          <Space>
            <Button icon={<CopyOutlined />} disabled={!data} onClick={() => copy(data?.script || '', '脚本已复制')}>复制脚本</Button>
            <Button onClick={() => setOpen(false)}>关闭</Button>
          </Space>
        }
      >
        {isLoading ? (
          <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>
        ) : (
          <pre style={{
            background: '#fafafa', padding: 12, borderRadius: 6, fontSize: 11,
            whiteSpace: 'pre-wrap', maxHeight: 420, overflow: 'auto',
          }}>{data?.script}</pre>
        )}
      </Modal>
    </>
  )
}

// ───────────────────────── 页面主体 ─────────────────────────

export default function WireGuardPage() {
  const { message } = App.useApp()
  const copy = useCopy(message)
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const canManage = has('action.wireguard.manage')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<WgNetwork | null>(null)
  const [peersNetwork, setPeersNetwork] = useState<WgNetwork | null>(null)

  const { data: networks = [], isLoading, refetch } = useQuery({
    queryKey: ['wg-networks'],
    queryFn: wireguardApi.listNetworks,
  })

  const { deleteMut: deleteNetworkMut } = useCrudResource<WgNetworkPayload, number, Partial<WgNetworkPayload>>({
    queryKey: ['wg-networks'],
    entityName: '组网',
    createFn: wireguardApi.createNetwork,
    updateFn: wireguardApi.updateNetwork,
    deleteFn: wireguardApi.deleteNetwork,
  })

  const openCreate = () => { setEditing(null); setDrawerOpen(true) }
  const openEdit = (network: WgNetwork) => { setEditing(network); setDrawerOpen(true) }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>虚拟组网</Typography.Title>
          <Text type="secondary">基于 WireGuard 的主机虚拟组网：出口节点自动生成密钥与配置，成员扫码/脚本接入</Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canManage} onClick={openCreate}>
            创建组网
          </Button>
        </Space>
      </Flex>

      {networks.length === 0 && !isLoading && (
        <Card>
          <Empty
            description="还没有虚拟组网"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            style={{ padding: '48px 0' }}
          >
            {canManage && (
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>创建第一个组网</Button>
            )}
          </Empty>
        </Card>
      )}

      <Spin spinning={isLoading}>
        <Row gutter={[16, 16]}>
          {networks.map((network) => (
            <Col xs={24} md={12} xl={8} key={network.id}>
              <Card
                size="small"
                hoverable
                onClick={() => setPeersNetwork(network)}
                title={
                  <Space size={6}>
                    <LinkOutlined style={{ color: '#1677ff' }} />
                    <Text strong>{network.name}</Text>
                  </Space>
                }
                extra={
                  <SettingOutlined onClick={(e) => { e.stopPropagation(); openEdit(network) }} />
                }
              >
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="网段"><Text code>{network.cidr}</Text></Descriptions.Item>
                  <Descriptions.Item label="出口">{network.server_name || '-'} <Text type="secondary">@{network.endpoint}:{network.port}</Text></Descriptions.Item>
                  <Descriptions.Item label="成员">
                    <Tag color="blue">{network.peer_count} 个</Tag>
                    <Tag color={network.full_tunnel ? 'orange' : 'green'}>{network.full_tunnel ? '全隧道' : '分流'}</Tag>
                  </Descriptions.Item>
                </Descriptions>
                <Flex justify="space-between" style={{ marginTop: 8 }} onClick={(e) => e.stopPropagation()}>
                  <Tooltip title="复制服务端公钥">
                    <Button size="small" type="text" icon={<GlobalOutlined />}
                      onClick={() => copy(network.server_public_key, '服务端公钥已复制')} />
                  </Tooltip>
                  <Popconfirm
                    title={`删除组网 ${network.name}？`}
                    description="将连带删除全部成员记录；服务器上的 wg 接口请自行卸载。"
                    okText="删除" okButtonProps={{ danger: true }} cancelText="取消"
                    onConfirm={() => riskConfirm.confirm('wireguard:delete', () => deleteNetworkMut.mutate(network.id))}
                    disabled={!canManage}
                  >
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={!canManage} />
                  </Popconfirm>
                </Flex>
              </Card>
            </Col>
          ))}
        </Row>
      </Spin>

      <NetworkDrawer open={drawerOpen} editing={editing} onClose={() => setDrawerOpen(false)} />
      <PeersDrawer network={peersNetwork} onClose={() => setPeersNetwork(null)} />
    </div>
  )
}
