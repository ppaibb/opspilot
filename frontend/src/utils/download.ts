/**
 * Blob 附件下载工具：供凭证导出、证书文件包等 blob 接口复用。
 */

/** 从 Content-Disposition 头解析附件文件名（支持 filename= 与 filename*=UTF-8'' 两种形态） */
function parseFilenameFromDisposition(header?: string): string | null {
  if (!header) return null
  const utf8Match = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1].trim().replace(/^"|"$/g, ''))
    } catch {
      // 编码异常时回落到普通 filename 解析
    }
  }
  const plainMatch = /filename="?([^";]+)"?/i.exec(header)
  return plainMatch?.[1]?.trim() || null
}

/**
 * 触发浏览器保存一个 Blob。
 *
 * @param blob            接口返回的 blob 数据
 * @param fallbackName    头里解析不到文件名时使用的兜底文件名
 * @param disposition     响应的 Content-Disposition 头（可省略）
 */
export function downloadBlob(blob: Blob, fallbackName: string, disposition?: string): void {
  const link = document.createElement('a')
  const url = URL.createObjectURL(blob)
  try {
    link.href = url
    link.download = parseFilenameFromDisposition(disposition) || fallbackName
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}
