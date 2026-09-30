"""
WebSocket 实时终端接口。

设计目标：
1. 通过一次性短期 token 建立会话；
2. 使用 paramiko.invoke_shell 提供交互式终端；
3. 将浏览器输入与 SSH 通道双向桥接，做到“活终端”体验。
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import time
from typing import Callable, Optional

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
import paramiko

from app.db.database import SessionLocal
from app.models.audit import AuditLog
from app.services.ssh_client import HOST_KEY_MISMATCH_MESSAGE, pin_and_connect
from app.services.terminal_session import (
    consume_terminal_session,
    pop_active_terminal_session,
    register_active_terminal_session,
    terminal_runtime_limits,
    touch_active_terminal_session,
)

router = APIRouter()

_REASON_IDLE_TIMEOUT = "__idle_timeout__"
_REASON_MAX_DURATION = "__max_duration__"


def _build_private_key(pkey_content: str) -> Optional[paramiko.PKey]:
    """尝试解析多种私钥格式，兼容 RSA / ED25519 / ECDSA。"""
    raw = (pkey_content or "").strip()
    if not raw:
        return None
    for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(raw))
        except Exception:
            continue
    return None


def _write_audit(user: str, action: str, resource: str, ip: str, result: str) -> None:
    """写入审计日志（WebSocket 不会经过 HTTP 审计中间件，需要手动补记）。"""
    db = SessionLocal()
    try:
        db.add(
            AuditLog(
                user=user or "未知用户",
                action=action,
                resource=resource,
                ip=ip or "-",
                result=result,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# Web 终端里 systemctl/man/journalctl 会拉起 less，wterm 对备用屏/状态行
# 渲染成大块灰底 + 行尾 >（见 2026-09-28 截图）。会话级关掉自动分页：
# - SYSTEMD_PAGERSKIP=1：systemctl/journalctl 不进 pager
# - PAGER=cat：其它调用 $PAGER 的工具直出
# - LESS=FRX：若仍进 less——一屏内直接退出、保留 ANSI、退出不拆屏
# 用户手动敲 `less file` 仍可用。
_WEBTERM_PAGER_ENV = (
    "export SYSTEMD_PAGERSKIP=1 SYSTEMD_PAGER=cat PAGER=cat "
    "LESS=FRX MANPAGER='less -FRX'; "
    'exec "${SHELL:-/bin/bash}" -il'
)


def _open_ssh_shell(
    *,
    ip: str,
    port: int,
    user: str,
    password: str,
    pkey_content: str,
    cols: int,
    rows: int,
) -> tuple[paramiko.SSHClient, paramiko.Channel]:
    """建立 SSH 连接并打开 PTY 交互通道。"""
    if not password and not pkey_content:
        raise RuntimeError("未配置 SSH 密码或私钥，无法建立终端")

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, ip, int(port))

    pkey = _build_private_key(pkey_content)
    if pkey_content and not pkey:
        raise RuntimeError("私钥解析失败，请检查凭证格式")

    try:
        if pkey:
            client.connect(
                hostname=ip,
                port=int(port),
                username=user,
                pkey=pkey,
                timeout=6.0,
                banner_timeout=6.0,
                auth_timeout=6.0,
            )
        else:
            client.connect(
                hostname=ip,
                port=int(port),
                username=user,
                password=password,
                timeout=6.0,
                banner_timeout=6.0,
                auth_timeout=6.0,
            )
    except paramiko.AuthenticationException as exc:
        client.close()
        raise RuntimeError("SSH 鉴权失败：用户名/密码或私钥不正确") from exc
    except (socket.timeout, socket.error) as exc:
        client.close()
        raise RuntimeError("SSH 网络不可达或端口不可用") from exc
    except paramiko.SSHException as exc:
        client.close()
        if HOST_KEY_MISMATCH_MESSAGE in str(exc):
            # 指纹钉扎拦截：保持原文提示，让用户知道是安全拦截而非协议故障。
            raise RuntimeError(str(exc)) from exc
        raise RuntimeError("SSH 协议协商失败") from exc

    # 用 get_pty + exec 而非 invoke_shell：exec 前可把分页器环境变量直接
    # 写进登录 shell 进程（不经 sshd AcceptEnv，也不在屏幕上回显 export）。
    transport = client.get_transport()
    if transport is None:
        client.close()
        raise RuntimeError("SSH 传输通道未建立")

    try:
        channel = transport.open_session(timeout=6.0)
    except Exception as exc:
        client.close()
        raise RuntimeError(f"无法打开 SSH 会话通道：{exc}") from exc

    width = min(max(int(cols or 120), 60), 300)
    height = min(max(int(rows or 36), 20), 120)
    try:
        channel.get_pty(term="xterm-256color", width=width, height=height)
        channel.exec_command(_WEBTERM_PAGER_ENV)
    except Exception as exc:
        channel.close()
        client.close()
        raise RuntimeError(f"启动交互式 shell 失败：{exc}") from exc

    channel.settimeout(0.0)
    return client, channel


async def _channel_to_websocket_loop(
    websocket: WebSocket,
    channel: paramiko.Channel,
    on_activity: Callable[[], None],
) -> None:
    """把 SSH 通道输出持续转发到前端。"""
    try:
        while True:
            if channel.closed:
                await websocket.send_text(
                    json.dumps({"type": "system", "level": "info", "message": "SSH 会话已关闭"})
                )
                return
            if channel.recv_ready():
                chunk = await asyncio.to_thread(channel.recv, 4096)
                if chunk:
                    on_activity()
                    await websocket.send_text(
                        json.dumps(
                            {"type": "output", "data": chunk.decode(errors="ignore")},
                            ensure_ascii=False,
                        )
                    )
                continue
            await asyncio.sleep(0.03)
    except Exception as exc:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "system",
                    "level": "error",
                    "message": f"终端输出通道异常：{str(exc)}",
                },
                ensure_ascii=False,
            )
        )


async def _websocket_to_channel_loop(
    websocket: WebSocket,
    channel: paramiko.Channel,
    on_activity: Callable[[], None],
) -> None:
    """接收前端输入并写入 SSH 通道，同时处理窗口尺寸变化。"""
    while True:
        message = await websocket.receive_text()
        on_activity()
        try:
            payload = json.loads(message)
        except Exception:
            continue
        msg_type = str(payload.get("type") or "").strip().lower()
        if msg_type == "input":
            data = str(payload.get("data") or "")
            if data:
                await asyncio.to_thread(channel.send, data)
        elif msg_type == "resize":
            cols = int(payload.get("cols") or 120)
            rows = int(payload.get("rows") or 36)
            await asyncio.to_thread(
                channel.resize_pty,
                width=min(max(cols, 60), 300),
                height=min(max(rows, 20), 120),
            )
        elif msg_type == "ping":
            await websocket.send_text(json.dumps({"type": "pong"}))


async def _session_guard_loop(
    websocket: WebSocket,
    *,
    started_ts: float,
    last_active_ref: dict[str, float],
    limits: dict[str, int],
) -> None:
    """
    会话守护协程：监控“最长会话时长 + 空闲超时”。

    说明：
    - 该任务只负责触发关闭条件，不直接清理资源；
    - 资源清理由外层 `finally` 统一完成，避免重复关闭。
    """
    idle_timeout = int(limits.get("idle_timeout_seconds") or 900)
    max_duration = int(limits.get("max_duration_seconds") or 14400)
    while True:
        await asyncio.sleep(2.0)
        now_ts = time.monotonic()
        idle_seconds = now_ts - float(last_active_ref.get("ts") or started_ts)
        age_seconds = now_ts - started_ts
        if idle_seconds >= idle_timeout:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "system",
                        "level": "warning",
                        "message": "会话空闲超时，已自动断开",
                    },
                    ensure_ascii=False,
                )
            )
            raise RuntimeError(_REASON_IDLE_TIMEOUT)
        if age_seconds >= max_duration:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "system",
                        "level": "warning",
                        "message": "会话达到最大时长，已自动断开",
                    },
                    ensure_ascii=False,
                )
            )
            raise RuntimeError(_REASON_MAX_DURATION)


@router.websocket("/ws/{session_id}")
async def terminal_ws(websocket: WebSocket, session_id: str):
    """建立实时终端 WebSocket 连接。"""
    limits = terminal_runtime_limits()
    token = (websocket.query_params.get("token") or "").strip()
    ok, message, context = consume_terminal_session(session_id, token)
    if not ok or context is None:
        await websocket.close(code=4401, reason=message)
        return

    await websocket.accept()
    peer_ip = websocket.client.host if websocket.client else "-"
    registered, reason = register_active_terminal_session(
        session_id=context.session_id,
        user=context.created_by,
        asset_id=context.asset_id,
        ip=context.ip,
        port=context.port,
    )
    if not registered:
        await websocket.send_text(
            json.dumps(
                {"type": "system", "level": "error", "message": reason},
                ensure_ascii=False,
            )
        )
        _write_audit(
            context.created_by,
            action="打开 Web 终端会话",
            resource=f"/api/terminal/ws/{context.session_id}",
            ip=peer_ip,
            result=f"failed:{reason}",
        )
        await websocket.close(code=4409, reason=reason[:120])
        return

    _write_audit(
        context.created_by,
        action="打开 Web 终端会话",
        resource=f"/api/terminal/ws/{context.session_id}",
        ip=peer_ip,
        result="success",
    )

    ssh_client: Optional[paramiko.SSHClient] = None
    ssh_channel: Optional[paramiko.Channel] = None
    close_reason = "completed"
    started_ts = time.monotonic()
    last_active_ref = {"ts": started_ts}

    def mark_activity() -> None:
        """统一更新活跃时间，供守护协程判断会话空闲状态。"""
        now_ts = time.monotonic()
        last_active_ref["ts"] = now_ts
        touch_active_terminal_session(context.session_id)

    try:
        ssh_client, ssh_channel = await asyncio.to_thread(
            _open_ssh_shell,
            ip=context.ip,
            port=context.port,
            user=context.user,
            password=context.password,
            pkey_content=context.pkey_content,
            cols=context.cols,
            rows=context.rows,
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "system",
                    "level": "info",
                    "message": f"已连接 {context.asset_name} ({context.user}@{context.ip}:{context.port})",
                },
                ensure_ascii=False,
            )
        )

        to_ws_task = asyncio.create_task(_channel_to_websocket_loop(websocket, ssh_channel, mark_activity))
        to_ssh_task = asyncio.create_task(_websocket_to_channel_loop(websocket, ssh_channel, mark_activity))
        guard_task = asyncio.create_task(
            _session_guard_loop(
                websocket,
                started_ts=started_ts,
                last_active_ref=last_active_ref,
                limits=limits,
            )
        )
        done, pending = await asyncio.wait(
            {to_ws_task, to_ssh_task, guard_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if not exc:
                continue
            if isinstance(exc, WebSocketDisconnect):
                close_reason = "client_disconnect"
                continue
            if isinstance(exc, RuntimeError) and str(exc) == _REASON_IDLE_TIMEOUT:
                close_reason = "idle_timeout"
                continue
            if isinstance(exc, RuntimeError) and str(exc) == _REASON_MAX_DURATION:
                close_reason = "max_duration"
                continue
            raise exc
    except WebSocketDisconnect:
        close_reason = "client_disconnect"
    except Exception as exc:
        close_reason = f"error:{str(exc)}"
        try:
            await websocket.send_text(
                json.dumps(
                    {"type": "system", "level": "error", "message": f"终端连接失败：{str(exc)}"},
                    ensure_ascii=False,
                )
            )
        except Exception:
            pass
        _write_audit(
            context.created_by,
            action="打开 Web 终端会话",
            resource=f"/api/terminal/ws/{context.session_id}",
            ip=peer_ip,
            result="failed",
        )
    finally:
        # 先从活动会话池移除，再输出断开审计，确保并发计数及时回收。
        session_summary = pop_active_terminal_session(context.session_id)
        duration_seconds = float(session_summary.get("duration_seconds") or 0.0)
        idle_seconds = float(session_summary.get("idle_seconds") or 0.0)
        _write_audit(
            context.created_by,
            action="关闭 Web 终端会话",
            resource=f"/api/terminal/ws/{context.session_id}",
            ip=peer_ip,
            result=f"{close_reason};duration={duration_seconds:.1f}s;idle={idle_seconds:.1f}s",
        )
        if ssh_channel is not None:
            try:
                ssh_channel.close()
            except Exception:
                pass
        if ssh_client is not None:
            try:
                ssh_client.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
