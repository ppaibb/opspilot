import {
  Drawer, Button, Space, Form, Input, Select,
} from 'antd'
import type { FormInstance } from 'antd'
import type { AppGroupItem, EditAppFormValues } from './types'

export default function EditAppDrawer({
  open,
  onClose,
  editAppForm,
  groups,
  saving,
  onFinish,
}: {
  open: boolean
  onClose: () => void
  editAppForm: FormInstance<EditAppFormValues>
  groups: AppGroupItem[]
  saving: boolean
  onFinish: (values: EditAppFormValues) => void
}) {
  return (
    <Drawer
      title="编辑应用信息"
      open={open}
      onClose={onClose}
      width={600}
      footer={
        <Space style={{ float: 'right' }}>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            loading={saving}
            onClick={() => editAppForm.submit()}
          >
            保存
          </Button>
        </Space>
      }
    >
      <Form
        form={editAppForm}
        layout="vertical"
        onFinish={(values) => onFinish(values)}
      >
        <Form.Item
          label="应用名称"
          name="name"
          rules={[{ required: true, message: '请输入应用名称' }]}
        >
          <Input placeholder="例如：Harbor 镜像仓库" />
        </Form.Item>

        <Form.Item label="所属分组" name="group_id">
          <Select allowClear placeholder="选择分组（可选）">
            {groups.map((group) => (
              <Select.Option key={group.group_id} value={group.group_id}>
                <Space>
                  <span style={{ 
                    display: 'inline-block', 
                    width: 8, 
                    height: 8, 
                    borderRadius: '50%', 
                    backgroundColor: group.color || '#1677ff' 
                  }} />
                  {group.name}
                </Space>
              </Select.Option>
            ))}
          </Select>
        </Form.Item>

        <Form.Item label="分类" name="category">
          <Input placeholder="例如：基础设施" />
        </Form.Item>

        <Form.Item label="环境" name="environment">
          <Select>
            <Select.Option value="prod">生产环境</Select.Option>
            <Select.Option value="test">测试环境</Select.Option>
            <Select.Option value="dev">开发环境</Select.Option>
            <Select.Option value="mixed">混合环境</Select.Option>
          </Select>
        </Form.Item>

        <Form.Item label="状态" name="status">
          <Select>
            <Select.Option value="active">运行中</Select.Option>
            <Select.Option value="maintenance">维护中</Select.Option>
            <Select.Option value="offline">已下线</Select.Option>
          </Select>
        </Form.Item>

        <Form.Item label="负责人" name="owner">
          <Input placeholder="例如：张三" />
        </Form.Item>

        <Form.Item label="应用入口" name="entry_url">
          <Input placeholder="https://harbor.example.com" />
        </Form.Item>

        <Form.Item label="应用描述" name="description">
          <Input.TextArea rows={3} placeholder="简要描述该应用的用途" />
        </Form.Item>
      </Form>
    </Drawer>
  )
}
