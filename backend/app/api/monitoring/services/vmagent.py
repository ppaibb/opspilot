"""vmagent（Alloy）采集配置渲染与热重载。

【背景】
 旧版监控使用 vmagent 的 prometheus.scrape 机制，新版已迁移到 Alloy Collector Fleet，
 但兼容层仍需保留，因为存量环境可能仍在使用 vmagent target 清单。

【设计要点】
1. 配置片段化：Alloy 配置中只替换 OpsPilot 托管的 target 片段（managed block），
   不碰用户自定义的其他采集任务，避免越权覆盖；
2. marker 机制：通过 VMAGENT_MANAGED_BEGIN / VMAGENT_MANAGED_END 标记定位旧片段，
   即使文件被外部工具修改过，只要标记存在就能安全替换；
3. 端口规避： Alloy 默认 HTTP 调试端口是 12345，但监控套件容器可能也用了 12345，
   因此 bootstrap 配置中显式指定 12346，避免端口冲突。
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.monitoring.constants import (
    CONFIG_KEY,
    DEFAULT_NODE_EXPORTER_PORT,
    MONITORING_COMPOSE_FILE,
    MONITORING_DEPLOY_DIR,
    RELOADABLE_MONITORING_SERVICES,
    TARGETS_CONFIG_KEY,
    VMAGENT_CONFIG_FILE,
    VMAGENT_LEGACY_MARKER,
    VMAGENT_MANAGED_BEGIN,
    VMAGENT_MANAGED_END,
)
from app.api.monitoring.services.config_store import read_monitoring_targets, save_monitoring_targets
from app.api.monitoring.services.rules import _restart_compose_service_if_running, _yaml_quote
from app.models.asset import Asset


def _run_local_command(args: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
    """执行固定的本地监控运维命令，避免 vmagent 兼容层反向依赖部署服务造成循环导入。"""
    try:
        completed = subprocess.run(
            args,
            cwd=str(MONITORING_DEPLOY_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "code": completed.returncode,
            "stdout": (completed.stdout or "")[-6000:],
            "stderr": (completed.stderr or "")[-6000:],
        }
    except FileNotFoundError as exc:
        return {"ok": False, "code": 127, "stdout": "", "stderr": f"命令不存在: {exc.filename}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": f"命令执行超过 {timeout_seconds} 秒"}


def _compose_args(*parts: str) -> list[str]:
    """生成 docker compose 参数，compose 文件固定，禁止外部输入路径。"""
    return ["docker", "compose", "-f", str(MONITORING_COMPOSE_FILE), *parts]

def _normalize_targets_for_vmagent(db: Session, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    将数据库中的 target 清单规整成 Alloy 可落盘的目标列表。

    处理原则：
    - 只保留仍然存在于资产表中的主机；
    - IP、名称、平台、地域等展示信息始终以当前资产表为准，避免主机改名或换 IP 后配置漂移；
    - 仅当 `enabled=True` 时才会被写入 Alloy 配置。
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
        port = max(1, min(int(item.get("port") or DEFAULT_NODE_EXPORTER_PORT), 65535))
        job = (str(item.get("job") or "opspilot-node").strip()[:80] or "opspilot-node")
        normalized.append(
            {
                "asset_id": asset.instance_id,
                "name": asset.name,
                "ip": asset.ip,
                "provider": str(asset.provider or ""),
                "region": str(asset.region or ""),
                "port": port,
                "job": job,
                "enabled": bool(item.get("enabled")),
                "source": "asset",
                "target": f"{asset.ip}:{port}",
                "status": str(item.get("status") or "pending"),
                "last_checked_at": str(item.get("last_checked_at") or ""),
                "last_error": str(item.get("last_error") or "")[:255],
            }
        )
    return normalized


def _render_opspilot_vmagent_block(targets: list[dict[str, Any]]) -> str:
    """生成由 OpsPilot 托管的 Alloy prometheus.scrape 配置块。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(targets, key=lambda row: (str(row.get("job") or ""), str(row.get("asset_id") or ""))):
        if not item.get("enabled"):
            continue
        job = str(item.get("job") or "opspilot-node").strip()[:80] or "opspilot-node"
        grouped.setdefault(job, []).append(item)

    lines = [
        VMAGENT_MANAGED_BEGIN,
        "// 该区块由 OpsPilot 自动维护；主机纳入/移除监控后会自动刷新。",
    ]
    if not grouped:
        lines.extend(
            [
                'prometheus.scrape "opspilot_node" {',
                "  targets = []",
                "  forward_to = [prometheus.remote_write.opspilot.receiver]",
                "}",
            ]
        )
    else:
        for job_name, items in grouped.items():
            block_name = re.sub(r"[^a-zA-Z0-9_]", "_", job_name).strip("_") or "opspilot_node"
            lines.append(f'prometheus.scrape "{block_name}" {{')
            lines.append("  targets = [")
            for item in items:
                lines.extend(
                    [
                        "    {",
                        f'      "__address__" = {_yaml_quote(item.get("target"))},',
                        f"      asset_id = {_yaml_quote(item.get('asset_id'))},",
                        f"      host_name = {_yaml_quote(item.get('name'))},",
                        f"      provider = {_yaml_quote(item.get('provider'))},",
                        f"      region = {_yaml_quote(item.get('region'))},",
                        f"      job = {_yaml_quote(job_name)},",
                        '      source = "opspilot",',
                        "    },",
                    ]
                )
            lines.append("  ]")
            lines.append("  forward_to = [prometheus.remote_write.opspilot.receiver]")
            lines.append("}")
    lines.append(VMAGENT_MANAGED_END)
    return "\n".join(lines)


# 将新的 target 片段合并进现有 Alloy 配置。
# 策略：若已存在 managed block，则整段替换；若不存在，追加到文件末尾并附上前置注释。
# 这样既能平滑升级存量环境，又能保证新环境首次写入时结构清晰。
def _merge_vmagent_config_text(original_text: str, managed_block: str) -> str:
    """
    将 OpsPilot 托管区块合并进 Alloy 配置文件。

    兼容两种情况：
    - 新版：文件内已经存在 begin/end 标记，直接替换；
    - 旧版：文件末尾还是最初的占位注释和空 target，直接从旧注释处覆盖到文件末尾。
    """
    if VMAGENT_MANAGED_BEGIN in original_text and VMAGENT_MANAGED_END in original_text:
        before, _, tail = original_text.partition(VMAGENT_MANAGED_BEGIN)
        _, _, after = tail.partition(VMAGENT_MANAGED_END)
        suffix = after.lstrip("\r\n")
        merged = f"{before.rstrip()}\n{managed_block}\n"
        if suffix:
            merged += suffix
        return merged

    return f"{original_text.rstrip()}\n\n{managed_block}\n"


def _write_vmagent_config_file(db: Session, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """
    将启用中的 target 真正写入 `deploy/monitoring/config/alloy.alloy`。

    文件同步失败时必须阻断数据库提交，避免“数据库显示已纳管，但 Alloy 配置未生效”的状态漂移。
    """
    if not VMAGENT_CONFIG_FILE.exists():
        raise RuntimeError("Alloy 配置文件不存在")

    normalized = _normalize_targets_for_vmagent(db, targets)
    managed_block = _render_opspilot_vmagent_block(normalized)
    original_text = VMAGENT_CONFIG_FILE.read_text(encoding="utf-8")
    merged_text = _merge_vmagent_config_text(original_text, managed_block)
    changed = merged_text != original_text
    if changed:
        VMAGENT_CONFIG_FILE.write_text(merged_text, encoding="utf-8")
    return {
        "path": str(VMAGENT_CONFIG_FILE),
        "target_count": sum(1 for item in normalized if item.get("enabled")),
        "changed": changed,
    }


def _reload_vmagent_if_running() -> dict[str, Any]:
    """
    在内置监控套件已运行时，重载 Alloy 使最新 target 生效。

    这里不主动启动整套监控，只在检测到 Alloy 正在运行时执行固定重启；
    如果监控套件尚未启动，则仅提示“配置已写入，待下次启动生效”。
    """
    if not MONITORING_COMPOSE_FILE.exists():
        return {"attempted": False, "ok": False, "message": "监控套件 compose 文件不存在，已仅写入配置文件"}

    ps_result = _run_local_command(_compose_args("ps", "--status", "running", "alloy"), 15)
    if not ps_result["ok"]:
        message = str(ps_result["stderr"] or ps_result["stdout"] or "无法检测 Alloy 运行状态").strip()
        return {"attempted": False, "ok": False, "message": message}

    if "alloy" not in str(ps_result["stdout"] or ""):
        return {"attempted": False, "ok": False, "message": "Alloy 当前未运行，配置已写入，待套件启动后生效"}

    restart_result = _run_local_command(_compose_args("restart", "alloy"), 60)
    return {
        "attempted": True,
        "ok": bool(restart_result["ok"]),
        "message": "Alloy 已按最新 target 配置重载" if restart_result["ok"] else str(restart_result["stderr"] or restart_result["stdout"] or "Alloy 重载失败").strip(),
        "stdout": str(restart_result["stdout"] or ""),
        "stderr": str(restart_result["stderr"] or ""),
    }


def _persist_targets_and_sync_vmagent(db: Session, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """
    保存 target 清单并联动 Alloy 配置落盘。

    流程：
    1. 先更新数据库中的 `monitoring_targets`；
    2. 再把启用中的 target 写入 `alloy.alloy`；
    3. 只有文件写成功后才提交数据库事务；
    4. 如果配置内容发生变化，再尝试重载本地运行中的 Alloy。
    """
    try:
        save_monitoring_targets(db, targets)
        sync_result = _write_vmagent_config_file(db, targets)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"同步 Alloy 配置失败: {str(exc)}")

    reload_result = {
        "attempted": False,
        "ok": False,
        "message": "Alloy 配置未变化，无需重载",
    }
    if sync_result.get("changed"):
        reload_result = _reload_vmagent_if_running()
    return {"config_sync": sync_result, "runtime_reload": reload_result}


