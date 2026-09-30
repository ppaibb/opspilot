# 43 - Web 终端 systemctl 分页灰块

> 更新时间：2026-09-28

## 问题

WebSSH 里执行 `systemctl status docker`，上半段彩色输出正常，下面出现一大块**灰底矩形**，
右侧每行一个 `>`，底部 `lines 1-22/22 (END)`。

**根因**：`systemctl status` 检测到 TTY 后用 `less` 做分页器。`less` 会切备用屏、
画反色状态行；`@wterm/react 0.3.x` 对这套序列渲染不正确 → 灰块 + 行尾 `>`。
`journalctl` / `man` / 等走 `$PAGER` 的命令同源。

**不是** PTY 尺寸或 SSH 编码问题——上半段 systemd 自己的着色输出已经正确渲染。

## 修复（仅后端）

`backend/app/api/terminal_ws.py`：

1. 新增 `_WEBTERM_PAGER_ENV`：
   ```
   export SYSTEMD_PAGERSKIP=1 SYSTEMD_PAGER=cat PAGER=cat LESS=FRX MANPAGER='less -FRX'
   exec "${SHELL:-/bin/bash}" -il
   ```
   - `SYSTEMD_PAGERSKIP=1`：systemctl/journalctl 直接打印，不进 pager
   - `PAGER=cat`：其它 `$PAGER` 调用直出
   - `LESS=FRX`：若仍进 less——一屏内退、保留 ANSI、退出不拆屏（防灰块残留）
   - 用户手动 `less file` 仍可用

2. `_open_ssh_shell` 从 `invoke_shell` 改为 **`get_pty` + `exec_command(_WEBTERM_PAGER_ENV)`**：
   - 环境变量在 exec 的登录 shell 进程级生效，**不经 sshd AcceptEnv、屏幕上不回显 export 行**
   - `resize_pty` 行为不变

## 回归

- 契约测试 `tests/test_webterminal_pager_contract.py`（4 例）
- `scripts/run_checks.py` 4/4（后端 **656** passed / lint 0 err / build 通过）

## 部署 10.10.0.9

| 项 | 值 |
|---|---|
| 镜像 | `opspilot/backend:1.0.6`（1.0.5→；前端保持 1.0.9） |
| 验证 | login-page 200；镜像内 `_WEBTERM_PAGER_ENV` 含 SYSTEMD_PAGERSKIP；日志无 ERROR |

## 人工验证建议

强刷后重新打开终端，再跑 `systemctl status docker`——应整段直出、无灰块。
再试 `journalctl -n 20`、`man ls`（应一屏内直接退出）。

---

## 追加（2026-09-28）：滚轮无法回看历史输出

**现象**：灰块修好后，鼠标滚轮在终端里不能往上滚了。

**根因**：`TerminalModal.tsx` 注入的主题样式写死了

```css
.wterm, div[class*="wterm"], ... { overflow: hidden !important; }
```

wterm 自带 scrollback 时靠 `.wterm.has-scrollback { overflow-y: auto }` 让根节点可滚；
`!important` 把它盖死 → 有历史输出也滚不动。关掉 pager 后长输出全进 scrollback，
这个矛盾立刻暴露（以前在 less 备用屏里看，用不到终端回看）。

**修复**（仅前端）：

- 去掉一刀切的 `overflow: hidden !important`
- `.wterm.has-scrollback` → `overflow-y: auto !important`（滚轮回看）
- `.wterm:not(.has-scrollback)` → `overflow: hidden !important`（无历史时锁死）

**部署**：`opspilot/frontend:1.0.10`（1.0.9→）；`/` 200；hosts chunk 含 `has-scrollback` 样式。
检查 4/4（656 passed）。
