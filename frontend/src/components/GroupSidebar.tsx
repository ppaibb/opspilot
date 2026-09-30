/**
 * 公共分组管理侧栏组件
 * 支持：筛选切换 / 新增分组 / 编辑分组 / 删除分组 / 把选中资产分配到分组
 * 用法：
 *   <GroupSidebar
 *     resourceType="host"
 *     selectedGroupId={groupFilter}
 *     onSelect={setGroupFilter}
 *     selectedIds={selectedRowKeys}    // 当前表格勾选的行 ID 列表
 *   />
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  App, Button, Card, ColorPicker, Form, Input, Modal, Popconfirm, Space, Tooltip,
} from 'antd'
import {
  ApartmentOutlined, PlusOutlined, EditOutlined, DeleteOutlined, FolderAddOutlined,
} from '@ant-design/icons'
import { groupsApi } from '@/api'
import type { GroupCreate } from '@/types/api'

interface Props {
  resourceType?: string
  selectedGroupId: number | null
  onSelect: (id: number | null) => void
  /** 当前表格已勾选的资源 ID，用于"分配到分组"功能 */
  selectedIds?: string[]
}

export default function GroupSidebar({ resourceType = 'host', selectedGroupId, onSelect, selectedIds = [] }: Props) {
  const queryClient = useQueryClient()
  const { message } = App.useApp()

  const [modalOpen, setModalOpen]   = useState(false)
  const [editingId, setEditingId]   = useState<number | null>(null)
  const [form]                      = Form.useForm<GroupCreate & { color: string }>()

  const { data: groups = [] } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  const saveMut = useMutation({
    mutationFn: ({ id, data }: { id?: number; data: GroupCreate }) =>
      id ? groupsApi.update(id, data) : groupsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      setModalOpen(false)
      message.success(editingId ? '分组已更新' : '分组已创建')
    },
    onError: () => message.error('操作失败'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => groupsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      if (selectedGroupId !== null) onSelect(null)
      message.success('分组已删除')
    },
    onError: () => message.error('删除失败，该分组可能还有资产'),
  })

  const assignMut = useMutation({
    mutationFn: (groupId: number | null) => groupsApi.assign(selectedIds, groupId, resourceType),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      message.success('已分配')
    },
    onError: () => message.error('分配失败'),
  })

  const openCreate = () => {
    setEditingId(null)
    form.resetFields()
    form.setFieldsValue({ color: '#1677ff' })
    setModalOpen(true)
  }

  const openEdit = (e: React.MouseEvent, id: number) => {
    e.stopPropagation()
    const g = groups.find((x) => x.id === id)
    if (!g) return
    setEditingId(id)
    form.setFieldsValue({ name: g.name, color: g.color, description: g.description })
    setModalOpen(true)
  }

  const handleSave = () => {
    form.validateFields().then(({ name, description, color }) => {
      const colorStr = typeof color === 'string' ? color : (color as { toHexString: () => string }).toHexString?.() ?? '#1677ff'
      saveMut.mutate({ id: editingId ?? undefined, data: { name, description, color: colorStr } })
    })
  }

  const groupItems = [
    { id: null as number | null, name: '全部', color: '#8c8c8c', member_count: undefined },
    ...groups,
  ]

  return (
    <>
      <Card
        size="small"
        title={<><ApartmentOutlined /> 分组</>}
        style={{ width: 200, flexShrink: 0 }}
        styles={{ body: { padding: 6 } }}
        extra={
          <Tooltip title="新增分组">
            <Button type="text" size="small" icon={<PlusOutlined />} onClick={openCreate} />
          </Tooltip>
        }
      >
        {groupItems.map((g) => (
          <div
            key={String(g.id)}
            onClick={() => onSelect(g.id)}
            style={{
              padding: '5px 8px', borderRadius: 6, cursor: 'pointer',
              backgroundColor: selectedGroupId === g.id ? '#e6f4ff' : 'transparent',
              fontWeight: selectedGroupId === g.id ? 600 : 400,
              marginBottom: 2,
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              gap: 4,
            }}
          >
            <Space size={4} style={{ minWidth: 0 }}>
              <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', backgroundColor: g.color, flexShrink: 0 }} />
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 }}>{g.name}</span>
              {g.member_count !== undefined && (
                <span style={{ fontSize: 11, color: '#8c8c8c' }}>({g.member_count})</span>
              )}
            </Space>
            {g.id !== null && (
              <Space size={0} style={{ flexShrink: 0 }}>
                <Button type="text" size="small" icon={<EditOutlined style={{ fontSize: 11 }} />}
                  onClick={(e) => openEdit(e, g.id!)} />
                <Popconfirm title="删除分组？" description="分组内资产将变为未分组。"
                  onConfirm={(e) => { e?.stopPropagation(); deleteMut.mutate(g.id!) }}
                  onPopupClick={(e) => e.stopPropagation()}>
                  <Button type="text" size="small" danger icon={<DeleteOutlined style={{ fontSize: 11 }} />}
                    onClick={(e) => e.stopPropagation()} />
                </Popconfirm>
              </Space>
            )}
          </div>
        ))}

        {/* 分配到分组（选中资产时才出现） */}
        {selectedIds.length > 0 && (
          <div style={{ marginTop: 8, borderTop: '1px solid #f0f0f0', paddingTop: 8 }}>
            <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 4 }}>
              已选 {selectedIds.length} 项 → 分配到：
            </div>
            {groups.map((g) => (
              <div
                key={g.id}
                onClick={() => assignMut.mutate(g.id)}
                style={{ padding: '3px 8px', borderRadius: 4, cursor: 'pointer', fontSize: 12, color: '#1677ff' }}
                onMouseEnter={(e) => (e.currentTarget.style.background = '#f0f0f0')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <FolderAddOutlined style={{ marginRight: 4 }} />{g.name}
              </div>
            ))}
            <div
              onClick={() => assignMut.mutate(null)}
              style={{ padding: '3px 8px', borderRadius: 4, cursor: 'pointer', fontSize: 12, color: '#8c8c8c' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = '#f0f0f0')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              移出分组
            </div>
          </div>
        )}
      </Card>

      {/* 新增/编辑分组 Modal */}
      <Modal
        title={editingId ? '编辑分组' : '新增分组'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        okText="保存"
        cancelText="取消"
        confirmLoading={saveMut.isPending}
        centered
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="name" label="分组名称" rules={[{ required: true, message: '请输入分组名称' }]}>
            <Input placeholder="例如 生产环境" />
          </Form.Item>
          <Form.Item name="color" label="颜色标识">
            <ColorPicker showText format="hex" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
