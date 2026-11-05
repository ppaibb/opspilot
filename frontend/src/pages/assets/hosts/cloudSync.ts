/** 统一轻量服务器分类，避免 Flexus L 在预览中遗漏或混入普通 ECS。 */
export function isLightCloudInstance(resourceType: string): boolean {
  return ['SWAS', 'Lighthouse', 'Lightsail', 'FlexusL'].includes(resourceType)
}

/**
 * 表格仅回传当前 Tab 的勾选；合并时只替换当前可见实例，
 * 保留另一 Tab 的选择，让 ECS 与 Flexus L 可以一次导入。
 */
export function mergeCloudSelection(
  current: string[], visibleIds: string[], selectedIds: string[],
): string[] {
  const visible = new Set(visibleIds)
  return Array.from(new Set([...current.filter((id) => !visible.has(id)), ...selectedIds]))
}
