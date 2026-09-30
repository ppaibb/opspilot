# WebSSH 终端与 SFTP 文件管理开发记录

**入口**：主机管理页（`/assets/hosts`）表格操作列「WebSSH 终端」按钮，弹出终端 Modal  
**组件**：`frontend/src/components/TerminalModal.tsx`（居中悬浮 / 全屏 macOS 风格 Modal，无独立路由）  
**状态**：✅ 完成（浮动/全屏 Modal + SFTP 全功能 + 🤖 AI 助手 + ⚡ 快捷指令 + 🎨 皮肤切换）

---

## 功能说明

| 功能 | 实现方式 |
|---|---|
| SSH 伪终端渲染 | 使用 `@wterm/react`（WASM+DOM），集成 `@wterm/react/css` 深色主题 |
| Token 凭证鉴权 | `POST /api/assets/terminal/session` 获取一次性 `session_token` |
| WebSocket 传输 | `wss://<host>/api/assets/terminal/session?token=xxx` 长连接 |
| 帧拆包解析 | `splitJsonFrames()` 解决 Vite 代理合并多个 JSON 帧导致的解析异常 |
| 伪终端 Resize | `ws.onopen` 及全屏切换时向 WebSocket 推送 `{ type: 'resize', cols, rows }` |
| 输入与焦点 | `useCallback` 稳定 `handleTerminalData` 引用，配合全局按键与点击自动 focus |
| 🤖 AI 命令助手 | Modal 界面，自然语言意图输入；先匹配预设词库，未命中则调用后端 `aiApi.getStreamUrl('ops')` SSE 流式生成，失败再关键词兜底，提炼纯净命令后一键注入终端执行 |
| ⚡ 常用快捷指令 | 下拉菜单预设 `uname`, `free -h`, `df -h`, `docker ps` 等常用运维指令一键执行 |
| 🎨 终端主题皮肤 | 4 款配色随意切换：🌙 夜空深蓝 (Slate) / 🟢 极客矩阵 (Matrix) / 🟣 赛博朋克 (Cyberpunk) / ☀️ 日光浅色 (Solarized) |
| 🧹 一键清屏 | Header 挂载清屏按键，发送 `clear` 快捷清空日志 |
| SFTP 目录导航 | 地址栏 Input 跳转、`[root]` 一键根路径、`[上级]` 返回、`[刷新]` 刷盘 |
| SFTP 新建目录 | `+新建` 按钮，Modal 命名弹窗，调用 `POST /assets/terminal/files/{id}/mkdir` |
| SFTP 文件上传 | `↑上传` 按钮，调用 `POST /assets/terminal/files/{id}/upload`（含 100MB 限制） |
| SFTP 文件下载 | `↓下载` 按钮，生成 `/api/assets/terminal/files/{id}/download` 链接安全下载 |
| SFTP 文件删除 | `🗑️删除` 按钮，Popconfirm 二次确认后调用 `DELETE /assets/terminal/files/{id}` |
| SFTP 格式展报 | 文件夹/文件图标区分，文件大小自动格式化（B/KB/MB/GB）及权限 `mode` 展示 |

---

## 布局结构

### 居中悬浮 / macOS 全屏 Modal 旗舰版 (`TerminalModal.tsx`)

```
┌───────────────────────────────────────────────────────────────────────────────────────────┬──────────────┐
│ [● ● ●] root@192.168.3.225 (harbor) [已连接] [🤖AI助手] [⚡快捷指令] [🎨皮肤] [🧹清屏] [SFTP文件] [-][⤢][X] │
├───────────────────────────────────────────────────────────────────────────────────────────┼──────────────┤
│                                                                                           │ SFTP 文件管理│
│  [system] 已成功建立 SSH 终端会话！ (harbor)                                               │──────────────│
│  Welcome to Ubuntu 22.04.5 LTS                                                           │ [/root     ] │
│                                                                                           │ [root][上级] │
│  System load: 0.13      Processes: 240                                                    │ [+新建][↑上传]│
│  Usage of /:  11.9%     IPv4: 192.168.3.225                                               │──────────────│
│                                                                                           │ 📁 src       │
│  root@harbor:~# _                                                                         │ 📄 main.py   │
│                                                                                           │    2.4 KB [↓]│
└───────────────────────────────────────────────────────────────────────────────────────────┴──────────────┘
```

---

## 修复记录

- **AI 命令助手改为人在回路**：后端 `/api/ai/ops/query/stream` 是只读分析 Agent（系统提示词明确“不建议执行高危写操作”），与前端“生成命令并一键注入执行”的预期相冲。已新增 `injectCommandForReview()`：AI 生成的命令仅填入终端提示符（末尾不带 `\r`、只取首行避免多行串行），由用户 review 后手动按 Enter 执行；预设安全快捷指令仍走 `injectCommand()` 直接执行。按钮文案同步改为“填入终端（需确认执行）”。
- **终端最底行光标被遮挡**：wterm 容器注入样式底部内边距仅 14px，不足一行高度，最后一行光标被 `overflow: hidden` 裁切。已将底部内边距加大到 28px（约一行高度），最后一行留出落脚空间。
- **架构收敛**：早期存在整页版 `pages/assets/terminal/index.tsx` 与弹窗版 `TerminalModal.tsx` 两套实现并存，已统一保留弹窗版，删除整页版文件及其 `/assets/terminal` 路由，主机页入口改为直接弹出 Modal。

---

## 待完善

- [ ] WebSSH 会话超时自动断开提醒（后端设定的 15 分钟空闲 / 4 小时最大存活）
- [ ] SFTP 拖拽上传文件支持
