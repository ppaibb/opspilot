import {
  Flex, Table, Alert,
} from 'antd'
import type { ApplicationDependenciesResponse } from '@/types/api'
import { RESOURCE_TYPE_LABEL } from './constants'

export default function DependenciesPanel({
  dependencies,
}: {
  dependencies: ApplicationDependenciesResponse | undefined
}) {
  return (
    <Flex vertical gap={16}>
      <Alert
        type="info"
        showIcon
        message={`当前应用关联 ${dependencies?.resources.length || 0} 个资源、${dependencies?.components.length || 0} 个运行实例，已有 ${dependencies?.release_count || 0} 条发布记录`}
      />
      <Table
        rowKey={(row) => `${row.resource_type}:${row.resource_id}`}
        size="middle"
        dataSource={dependencies?.resources || []}
        pagination={false}
        columns={[
          { title: '资源类型', dataIndex: 'resource_type', render: (type: string) => RESOURCE_TYPE_LABEL[type]?.label || type },
          { title: '资源名称', dataIndex: 'name' },
          { title: '资源 ID', dataIndex: 'resource_id' },
        ]}
        locale={{ emptyText: '暂未建立资源依赖' }}
      />
      <Table
        rowKey="component_id"
        size="middle"
        dataSource={dependencies?.components || []}
        pagination={false}
        columns={[
          { title: '运行实例', dataIndex: 'name' },
          { title: '部署方式', dataIndex: 'deployment_type' },
          { title: '资源引用', dataIndex: 'resource_refs', render: (refs: string[]) => refs?.join('、') || '-' },
        ]}
        locale={{ emptyText: '暂未登记运行实例' }}
      />
    </Flex>
  )
}
