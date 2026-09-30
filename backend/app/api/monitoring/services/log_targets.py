"""日志采集目标管理与 Vector 远程配置。

【为什么保留 Vector 兼容层】
 新版已全面迁移到 Alloy Collector Fleet，但存量环境可能仍在使用 Vector 做日志转发。
 本模块保留 Vector 的远程配置渲染与 SSH 下发能力，作为过渡兼容层。

【安全约束】
1. 远程配置通过 SSH 写入 /etc/vector/vector.yaml，使用资产凭证中的只读/管理账号；
2. Vector sink 地址指向 VictoriaLogs  ingest 接口，后端在渲染时做 URL 校验，防止恶意指向外网；
3. 主机侧 Vector 进程以普通用户运行，除非明确需要采集 /var/log/secure 等特权日志。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.collector import CollectorAgent
from app.models.user import User
from app.core.security import require_permissions
from app.api.assets import (
    decode_asset_password,
    ensure_asset_credential_access,
    get_credential_content,
    now_ts,
    run_ssh_command,
)
from app.services.asset_auth import resolve_asset_ssh_auth
from app.api.monitoring.constants import (
    CONFIG_KEY,
    DEFAULT_TIMEOUT_SECONDS,
    LOG_TARGETS_CONFIG_KEY,
)
from app.api.monitoring.services.config_store import read_monitoring_config, save_log_targets
from app.api.monitoring.services.proxy import (
    _build_authorization_header,
    _normalize_vlogs_ingest_base,
)
from app.api.monitoring.services.rules import _yaml_quote
from app.api.monitoring.services.targets import _get_asset_or_404

def _asset_log_target_record(
    asset: Asset,
    profile: str,
    status: str = "pending",
    error: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """生成日志采集主机记录。"""
    safe_profile = (profile or "linux-default").strip()[:80] or "linux-default"
    return {
        "asset_id": asset.instance_id,
        "name": asset.name,
        "ip": asset.ip,
        "provider": str(asset.provider or ""),
        "region": str(asset.region or ""),
        "agent": "alloy",
        "profile": safe_profile,
        "enabled": enabled,
        "status": status,
        "config_path": "/etc/alloy/config.alloy",
        "last_checked_at": now_ts(),
        "last_error": (error or "")[:255],
    }


def _normalize_log_targets_for_assets(db: Session, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    把日志采集主机记录与当前资产表对齐。

    处理原则：
    - 仅保留当前仍存在的资产；
    - 名称 / IP / 平台 / 地域以最新资产表为准；
    - 关闭项仍会保留，便于前端展示“已停用”状态。
    """
    asset_map = {asset.instance_id: asset for asset in db.query(Asset).all()}
    normalized: list[dict[str, Any]] = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        asset = asset_map.get(asset_id)
        if not asset:
            continue
        normalized.append(
            {
                **item,
                "asset_id": asset.instance_id,
                "name": asset.name,
                "ip": asset.ip,
                "provider": str(asset.provider or ""),
                "region": str(asset.region or ""),
                "agent": str(item.get("agent") or "alloy"),
                "config_path": str(item.get("config_path") or "/etc/alloy/config.alloy"),
                "profile": str(item.get("profile") or "linux-default"),
                "enabled": bool(item.get("enabled")),
            }
        )
    return normalized


def _sync_docker_log_policy(
    db: Session,
    asset: Asset,
    enabled: bool,
    current_user: User | None = None,
) -> dict[str, Any]:
    """把「Docker 日志采集开/关」下发到该主机的 Alloy managed 策略。

    复用 `_ensure_asset_alloy_policy` 的发布链（配置版本化 + Agent 拉取），
    logs_override 强制只采 Docker（不采 syslog），保证启停立即生效。
    返回是否成功下发，供前端以「是否成功下发」为准显示。
    """
    from app.api.monitoring.services.alloy_policy import (
        _ensure_asset_alloy_policy,
        _latest_agent_for_asset,
        _log_collect_state,
    )

    agent = _latest_agent_for_asset(db, asset.instance_id)
    if not agent:
        return {
            "asset_id": asset.instance_id,
            "ok": False,
            "logs_enabled": False,
            "message": "无 Alloy Agent，无法下发日志采集策略",
        }
    try:
        logs_override = {"system": False, "docker_containers": bool(enabled)}
        version = _ensure_asset_alloy_policy(
            db,
            asset,
            agent,
            current_user,
            logs_override=logs_override,
        )
        state = _log_collect_state(db, asset, agent, target_enabled=enabled)
        return {
            "asset_id": asset.instance_id,
            "ok": True,
            "logs_enabled": bool(state.get("logs_enabled")),
            "last_log_at": str(state.get("last_log_at") or ""),
            "version_id": getattr(version, "version_id", ""),
            "message": "Docker 日志采集已下发" if enabled else "已停用该主机日志采集块",
        }
    except Exception as exc:
        return {
            "asset_id": asset.instance_id,
            "ok": False,
            "logs_enabled": False,
            "message": f"日志策略下发失败: {str(exc)[:200]}",
        }


def _persist_log_targets(
    db: Session,
    targets: list[dict[str, Any]],
    current_user: User | None = None,
    disabled_asset_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    保存日志采集主机清单，并把 Docker 日志采集下发/停用到各主机 Alloy 策略。

    - 启用目标：logs_override 打开 docker_containers 并发布配置版本；
    - 停用/移除目标：关闭对应采集块；
    - DB 仍保留目标状态，但接口展示以「是否成功下发」为准（logs_enabled）。
    """
    try:
        normalized = _normalize_log_targets_for_assets(db, targets)
        asset_map = {asset.instance_id: asset for asset in db.query(Asset).all()}
        sync_results: list[dict[str, Any]] = []
        for item in normalized:
            asset = asset_map.get(str(item.get("asset_id") or ""))
            if not asset:
                continue
            enabled = bool(item.get("enabled"))
            result = _sync_docker_log_policy(db, asset, enabled, current_user)
            sync_results.append(result)
            # 以是否成功下发为准写入展示字段（DB 的 enabled 仍保留用户意图）
            item["logs_enabled"] = bool(result.get("logs_enabled"))
            item["last_log_at"] = str(result.get("last_log_at") or "")
            if not result.get("ok"):
                item["last_error"] = str(result.get("message") or item.get("last_error") or "")[:255]

        # 显式停用的资产（不在 targets 里）也要停掉采集块
        for asset_id in disabled_asset_ids or []:
            asset = asset_map.get(str(asset_id or "").strip())
            if not asset:
                continue
            result = _sync_docker_log_policy(db, asset, False, current_user)
            sync_results.append(result)

        save_log_targets(db, normalized)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存日志采集配置失败: {str(exc)}")
    synced = sum(1 for r in sync_results if r.get("ok"))
    return {
        "saved_count": len(normalized),
        "log_sync": {
            "synced": synced,
            "failed": len(sync_results) - synced,
            "results": sync_results,
        },
    }


def _build_remote_vector_check_command() -> str:
    """生成远端主机检测 Vector 状态的固定命令（存量 shim，已废弃）。"""
    config_path = "/etc/vector/vector.toml"
    return f"""
set -eu
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi
if ! command -v vector >/dev/null 2>&1; then
  echo "vector not installed" >&2
  exit 20
fi
if [ ! -f {config_path} ]; then
  echo "vector config missing" >&2
  exit 21
fi
$SUDO vector validate {config_path}
if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl is-active vector
else
  pgrep -x vector >/dev/null 2>&1
fi
""".strip()


def _build_remote_vector_disable_command() -> str:
    """生成远端主机停用 Vector 的固定命令。"""
    return """
set -eu
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi
if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl disable --now vector >/dev/null 2>&1 || true
  if $SUDO systemctl is-active vector >/dev/null 2>&1; then
    echo "vector service still active" >&2
    exit 22
  fi
else
  pkill -x vector >/dev/null 2>&1 || true
fi
echo "vector stopped"
""".strip()


# 通过 SSH 在远程资产上执行命令。
# 【安全审计】
# - 凭证通过 app.api.assets 的 decode_asset_password 解密，密钥由后端 Fernet 托管；
# - 命令参数使用 shlex.quote 转义，防止注入；
# - 超时时间固定 120 秒，避免网络不可达时挂死连接池；
# - 返回值包含 stdout/stderr/exit_code，便于前端展示"安装失败的具体原因"。
def _run_asset_ssh(
    db: Session,
    asset: Asset,
    command: str,
    timeout_seconds: int,
    current_user: User | None = None,
) -> tuple[bool, str, str, str, int]:
    """按资产绑定的凭证执行固定 SSH 命令。

    使用 resolve_asset_ssh_auth 统一认证解析，支持密码型凭证和私钥型凭证。
    """
    ensure_asset_credential_access(db, asset, current_user)
    auth = resolve_asset_ssh_auth(db, asset, current_user)
    return run_ssh_command(
        asset.ip,
        int(asset.ssh_port or 22),
        asset.ssh_user or "root",
        command,
        timeout_seconds,
        auth.password,
        auth.pkey_content,
    )


def _format_remote_error(title: str, message: str, stdout: str, stderr: str, code: int) -> str:
    """
    压缩远端命令失败信息。

    安装 Agent 时最常见的问题是包管理器、代理、证书或 systemd 失败。
    这里保留 stdout/stderr 的尾部，避免前端只看到“未注册”而无法定位真实原因。
    """
    parts = [f"{title}，退出码 {code}"]
    if message:
        parts.append(f"SSH: {message}")
    if stderr:
        parts.append(f"stderr: {stderr[-1200:]}")
    if stdout:
        parts.append(f"stdout: {stdout[-1200:]}")
    return "\n".join(parts)[:3000]

