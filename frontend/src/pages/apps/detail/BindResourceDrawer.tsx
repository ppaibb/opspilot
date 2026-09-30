import {
  Drawer, Button, Flex, Form, Select,
} from 'antd'
import type { FormInstance } from 'antd'
import { RESOURCE_TYPE_LABEL } from './constants'

export default function BindResourceDrawer({
  open,
  onClose,
  bindForm,
  availableResourceOptions,
  onSave,
  saving,
}: {
  open: boolean
  onClose: () => void
  bindForm: FormInstance<{ selectedValues: string[] }>
  availableResourceOptions: Array<{ type: string; id: string; name: string }>
  onSave: () => void
  saving: boolean
}) {
  return (
    <Drawer
      title="绑定资源"
      open={open}
      onClose={onClose}
      width={640}
      footer={
        <Flex justify="end" gap={8}>
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" onClick={onSave} loading={saving}>
            保存
          </Button>
        </Flex>
      }
    >
      <Form form={bindForm} layout="vertical">
        <Form.Item
          label="选择资源"
          name="selectedValues"
          tooltip="从现有资源中选择需要绑定的主机、虚拟机、域名或证书"
        >
          <Select
            mode="multiple"
            placeholder="选择资源"
            optionFilterProp="label"
            options={availableResourceOptions.map(r => ({
              label: `${RESOURCE_TYPE_LABEL[r.type]?.label || r.type} - ${r.name}`,
              value: JSON.stringify({ resource_type: r.type, resource_id: r.id }),
            }))}
          />
        </Form.Item>
      </Form>
    </Drawer>
  )
}
