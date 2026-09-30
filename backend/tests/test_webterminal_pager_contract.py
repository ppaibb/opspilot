"""Web 终端会话契约：分页器环境 + PTY 启动方式。

背景（2026-09-28 截图）：WebSSH 里跑 `systemctl status docker` 会进 less，
wterm 渲染成大块灰底 + 行尾 >，底部 `lines 1-22/22 (END)`。
修复：get_pty + exec 注入 SYSTEMD_PAGERSKIP/PAGER/LESS，登录 shell 不回显 export。
"""
from __future__ import annotations

import inspect

from app.api.terminal_ws import _WEBTERM_PAGER_ENV, _open_ssh_shell


def test_pager_env_disables_systemd_and_generic_pagers():
    assert "SYSTEMD_PAGERSKIP=1" in _WEBTERM_PAGER_ENV
    assert "SYSTEMD_PAGER=cat" in _WEBTERM_PAGER_ENV
    assert "PAGER=cat" in _WEBTERM_PAGER_ENV
    # 一屏内退出 + 保留 ANSI + 退出不拆屏（避免灰块残留）
    assert "LESS=FRX" in _WEBTERM_PAGER_ENV
    assert "MANPAGER" in _WEBTERM_PAGER_ENV


def test_pager_env_starts_login_shell_without_echoed_export():
    # exec 登录 shell：环境在进程级生效，屏幕上不会出现 export 行
    assert "exec" in _WEBTERM_PAGER_ENV
    assert "bash" in _WEBTERM_PAGER_ENV or "SHELL" in _WEBTERM_PAGER_ENV
    # 不是把 export 当交互命令打进去（那种会在终端里回显）
    assert not _WEBTERM_PAGER_ENV.strip().startswith("stty")


def test_open_ssh_shell_signature_unchanged():
    params = inspect.signature(_open_ssh_shell).parameters
    for name in ("ip", "port", "user", "password", "pkey_content", "cols", "rows"):
        assert name in params


def test_open_ssh_shell_uses_pager_env():
    source = inspect.getsource(_open_ssh_shell)
    assert "_WEBTERM_PAGER_ENV" in source
    assert "get_pty" in source
    # 不再用裸 invoke_shell 调用（无法注入会话级环境）；允许注释里提到它
    assert "client.invoke_shell(" not in source
    assert "channel.exec_command(_WEBTERM_PAGER_ENV)" in source
