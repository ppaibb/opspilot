import type {
  ApplicationComponentPayload,
  ApplicationPayload,
} from '@/types/api'

/** 编辑应用表单值（group_id 不进应用载荷，单独处理分组关系）。 */
export interface EditAppFormValues extends Partial<ApplicationPayload> {
  name: string
  group_id?: number | null
}

/** GET /groups?resource_type=application 返回的分组项。 */
export interface AppGroupItem {
  group_id: number
  name: string
  color?: string
}

/** GET /groups/members?resource_type=application&resource_id=... 返回的应用分组成员关系。 */
export interface AppGroupMembership {
  group_id: number
}

export type ScanHostStatus = 'pending' | 'scanning' | 'success' | 'failed'
export type ScanStage = 'config' | 'scanning' | 'preview'
export type ComponentFormValues = ApplicationComponentPayload & {
  target_resource_id?: string
  compose_project_name?: string
  compose_deploy_dir?: string
  compose_filename?: string
}
