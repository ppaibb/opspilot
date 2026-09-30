/**
 * 轻量通知适配层，兼容 WebTerminal 的 showNotice 调用。
 * 内部转发到 antd App.message（通过全局单例）。
 */
import { message } from 'antd'

export type NoticeType = 'info' | 'success' | 'warning' | 'error'

export function showNotice(_title: string, content: string, type: NoticeType = 'info') {
  message[type === 'warning' ? 'warning' : type](content)
}
