import type { LogLine, NormalizedLogEntry } from '@/api'

/**
 * 把后端归一化行映射为展示条目。
 * 契约：{id, time, message, fields} + 可选 level/service/host；兼容旧 raw VLogs 字段。
 * 单独成文件，满足 react-refresh/only-export-components。
 */
export function normalizeLog(line: LogLine): NormalizedLogEntry {
  const fields = (line.fields as Record<string, unknown>) || {}
  const id = String(line.id || line.stream_id || line._stream_id || fields._stream_id || '')
  const time = String(line.time || line._time || fields._time || '')
  const message = String(line.message ?? line._msg ?? fields._msg ?? fields.message ?? fields.log ?? '').trim()
  const level = String(line.level || fields.level || fields.severity || fields.log_level || 'INFO').toUpperCase()
  const service = String((line.service as string) || (fields.service as string) || (fields.job as string) || '-')
  const host = String((line.host as string) || (fields.host as string) || (fields.instance as string) || '-')
  return { id, time, level, service, host, message, stream_id: id, fields }
}

export function formatLogTime(value: string): string {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false })
}
