"""应用运行对象远程执行与审计工具。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
import time
import uuid

from sqlalchemy.orm import Session

from app.api.assets import (
    decode_asset_password,
    get_credential_content as get_asset_credential_content,
    run_ssh_command,
)
from app.api.tools import AnsibleRunResponse
from app.models.asset import Asset
from app.models.audit import AuditLog
from app.models.user import User
from app.services.application_runtime import MAX_RUNTIME_CONTROL_OUTPUT_LENGTH


@dataclass(frozen=True)
class BackgroundRequestContext:
    """后台任务使用的最小请求上下文。

    FastAPI 的 `Request` 对象只在请求生命周期内可靠存在。发布后台任务只需要
    客户端 IP 写审计，因此这里显式定义最小上下文，避免用 `SimpleNamespace`
    伪装成完整 Request 后被下游误读其它属性。
    """

    client_ip: str = "-"


class _RequestLike(Protocol):
    """运行审计需要的请求对象最小协议。"""

    client: Any


def _client_ip_from_context(request: _RequestLike | BackgroundRequestContext) -> str:
    """兼容真实 Request 与后台上下文，统一提取客户端 IP。"""
    if isinstance(request, BackgroundRequestContext):
        return request.client_ip or "-"
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "-") or "-")


def _audit_resource_label(asset: Asset, audit_resource: str | None = None) -> str:
    """生成不含命令明文的审计资源描述。

    远程命令可能包含镜像参数、环境变量、制品路径等敏感信息。审计需要保留
    “谁对哪个目标做了什么”的证据，但不应把完整 shell 命令写入数据库。
    """
    target = asset.name or asset.instance_id or asset.ip or "unknown-host"
    if audit_resource:
        return f"{target}::{audit_resource}"[:255]
    return str(target)[:255]


def _append_runtime_control_audit_log(
    db: Session,
    request: _RequestLike | BackgroundRequestContext,
    current_user: User,
    resource: str,
    result: str,
    action: str = "应用运行控制",
) -> None:
    """写入运行对象相关审计。"""
    db.add(
        AuditLog(
            user=str(current_user.name or current_user.username or "未知用户"),
            action=action,
            resource=resource[:255],
            ip=_client_ip_from_context(request),
            result=result,
        )
    )
    db.flush()


def _execute_runtime_ssh_task(
    asset: Asset,
    module_args: str,
    request: _RequestLike | BackgroundRequestContext,
    db: Session,
    current_user: User,
    audit_action: str = "应用运行控制",
    audit_resource: str | None = None,
) -> AnsibleRunResponse:
    """通过资产 SSH 凭证直连执行固定运行控制命令。

    安全边界：`module_args` 只来自后端白名单命令模板，不接收前端命令文本。
    审计中只保存脱敏后的目标描述，完整命令仅返回给本次操作响应。
    """
    started_at = time.perf_counter()
    pkey_content = ""
    if asset.credential_ref:
        pkey_content = get_asset_credential_content(db, asset.credential_ref, current_user)

    ok, notice, stdout, stderr, code = run_ssh_command(
        asset.ip,
        int(asset.ssh_port or 22),
        asset.ssh_user or "root",
        module_args,
        120,
        decode_asset_password(asset),
        pkey_content,
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    command_ok = bool(ok and code == 0)
    _append_runtime_control_audit_log(
        db=db,
        request=request,
        current_user=current_user,
        resource=_audit_resource_label(asset, audit_resource),
        result="success" if command_ok else "failed",
        action=audit_action,
    )
    return AnsibleRunResponse(
        ok=command_ok,
        run_id=f"SSH-{str(uuid.uuid4())[:8].upper()}",
        module="shell",
        target_count=1,
        duration_ms=duration_ms,
        code=int(code),
        stdout=str(stdout or "")[-MAX_RUNTIME_CONTROL_OUTPUT_LENGTH:],
        stderr=str(stderr or "")[-MAX_RUNTIME_CONTROL_OUTPUT_LENGTH:],
        command_preview=f"ssh {asset.ssh_user or 'root'}@{asset.ip} -- {module_args}",
        notice=notice or "SSH 直连执行完成",
    )
