"""本地监控套件（Docker Compose）部署运维。

【为什么需要独立模块】
 部署操作涉及本地 shell 命令（docker compose ps/up/down/restart），与 HTTP 代理、
 数据查询等业务逻辑属于完全不同的故障域。独立成模块后：
1. 权限边界清晰：部署动作需要 module.settings.system + action.settings.system.manage，
   而查询大盘只需要 module.monitoring.read；
2. 失败隔离：docker 命令超时或权限不足不会影响告警规则同步；
3. 可测试性：_run_local_command 返回标准化 dict，单元测试可用 mock 替换，无需真的起容器。
"""
from __future__ import annotations

import json
import re
from typing import Any

import shlex
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.monitoring.constants import (
    CONFIG_KEY,
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEPLOYMENT_ACTIONS,
    MONITORING_COMPOSE_FILE,
    MONITORING_DEPLOY_DIR,
    VMAGENT_CONFIG_FILE,
)
from app.api.assets import now_ts
from app.models.system_config import SystemConfig
from app.api.monitoring.services.config_store import read_monitoring_targets, _safe_load_json_dict
from app.api.monitoring.services.vmagent import (
    _write_vmagent_config_file,
)

# 执行本地 shell 命令并返回结构化结果。
# 为什么不用 subprocess.run 直接抛出 CalledProcessError？
# 因为前端需要同时看到 stdout、stderr、returncode 与 ok 状态，直接抛异常会丢失这些信息。
# 返回 dict 是前后端契约的一部分，保证 UI 能精准展示"哪一步失败、为什么失败"。
def _run_local_command(args: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
    """
    执行本机固定运维命令。

    安全约束：
    - 只接收后端拼好的参数数组，不经过 shell；
    - 工作目录固定到 deploy/monitoring；
    - 标准输出做长度截断，避免异常命令输出撑爆响应体。
    """
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
        stderr = f"命令不存在: {exc.filename}"
        if exc.filename == "docker":
            stderr += "\n提示：由于 OpsPilot 部署在 Docker 容器内，容器中默认没有安装 docker CLI，且未挂载 Docker Socket。请直接在宿主机的项目目录下手动运行以下命令来启动监控套件：\n  docker compose -f deploy/monitoring/docker-compose.yml up -d"
        return {"ok": False, "code": 127, "stdout": "", "stderr": stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": f"命令执行超过 {timeout_seconds} 秒"}


def _compose_args(*parts: str) -> list[str]:
    """生成 docker compose 参数，compose 文件固定，禁止前端传入路径。"""
    return ["docker", "compose", "-f", str(MONITORING_COMPOSE_FILE), *parts]


def _probe_builtin_monitoring_services() -> list[dict[str, Any]]:
    """
    探测容器网络内的内置监控服务。

    为什么需要这个兜底：
    Docker 化部署时，监控套件通常已经由宿主机脚本启动，后端容器不一定具备
    Docker CLI 或宿主机 Docker Socket。此时仅依赖 `docker compose ps` 会误导用户，
    所以状态页额外探测服务 HTTP 入口，用于证明套件实际在线。
    """
    probes = [
        ("VictoriaMetrics", "http://opspilot-victoriametrics:8428/-/healthy"),
        ("VictoriaLogs", "http://opspilot-victorialogs:9428/"),
        ("Grafana", "http://opspilot-grafana:3000/api/health"),
    ]
    results: list[dict[str, Any]] = []
    for name, url in probes:
        try:
            resp = requests.get(url, timeout=2)
            results.append({"name": name, "ok": resp.status_code < 500, "message": f"HTTP {resp.status_code}"})
        except requests.RequestException as exc:
            results.append({"name": name, "ok": False, "message": str(exc)})
    return results


def _format_monitoring_probe_output(probes: list[dict[str, Any]]) -> str:
    """把内置服务探测结果格式化给前端部署输出区域展示。"""
    if not probes:
        return ""
    lines = ["内置监控服务探测："]
    for item in probes:
        status = "online" if item.get("ok") else "offline"
        lines.append(f"- {item.get('name')}: {status} ({item.get('message')})")
    return "\n".join(lines)


def _deployment_status() -> dict[str, Any]:
    """读取内置监控套件部署状态，失败时返回可展示错误而不是抛异常。"""
    status: dict[str, Any] = {
        "compose_file": str(MONITORING_COMPOSE_FILE),
        "deployment_dir": str(MONITORING_DEPLOY_DIR),
        "compose_file_exists": MONITORING_COMPOSE_FILE.exists(),
        "docker_ok": False,
        "compose_ok": False,
        "docker_version": "",
        "compose_version": "",
        "ps_output": "",
        "last_error": "",
    }
    if not status["compose_file_exists"]:
        probes = _probe_builtin_monitoring_services()
        if any(item.get("ok") for item in probes):
            status["ps_output"] = _format_monitoring_probe_output(probes)
            status["last_error"] = "后端容器未挂载 deploy/monitoring/docker-compose.yml，但已检测到内置监控服务在线"
            return status
        status["last_error"] = "监控套件 docker-compose.yml 不存在"
        return status

    docker_version = _run_local_command(["docker", "--version"], 8)
    status["docker_ok"] = bool(docker_version["ok"])
    status["docker_version"] = (docker_version["stdout"] or docker_version["stderr"]).strip()

    compose_version = _run_local_command(["docker", "compose", "version"], 8)
    status["compose_ok"] = bool(compose_version["ok"])
    status["compose_version"] = (compose_version["stdout"] or compose_version["stderr"]).strip()

    ps_result = _run_local_command(_compose_args("ps"), 15)
    status["ps_output"] = (ps_result["stdout"] or ps_result["stderr"]).strip()
    if not ps_result["ok"]:
        probes = _probe_builtin_monitoring_services()
        probe_output = _format_monitoring_probe_output(probes)
        if any(item.get("ok") for item in probes):
            status["ps_output"] = probe_output
            status["last_error"] = ""
        else:
            status["last_error"] = str(ps_result["stderr"] or ps_result["stdout"] or "docker compose ps 执行失败").strip()
    return status


def _apply_local_monitoring_config(db: Session, external_base_url: str = "", *, force: bool = False) -> dict[str, Any]:
    """
    写入内置套件默认连接；**只补空字段**，不覆盖分体部署下用户已填的地址。

    三段地址（分体部署时往往在不同主机）：
    1. `vm_url` / `vlogs_url` —— **后端 → 存储**（如 http://192.168.3.99:8428）
    2. `remote_write_url` / `vlogs_ingest_url` —— **采集 Agent → 存储**（从业务主机可达）
    3. `vmui_url` / `grafana_url` / `vlogsui_url` —— **浏览器 → UI**（用户可达）

    一体化 Docker 部署时可用 force=True 重写为容器服务名。
    """
    base_url = str(external_base_url or "").strip().rstrip("/")
    defaults = {
        "vm_url": "http://opspilot-victoriametrics:8428",
        "vmui_url": f"{base_url}/vmui" if base_url else "http://127.0.0.1:8428/vmui",
        "grafana_url": "http://opspilot-grafana:3000/grafana",
        "vlogs_url": "http://opspilot-victorialogs:9428",
        "remote_write_url": f"{base_url}/vm/api/v1/write" if base_url else "",
        "vlogs_ingest_url": f"{base_url}/vlogs-ingest" if base_url else "",
        "vlogsui_url": f"{base_url}/vlogsui" if base_url else "http://127.0.0.1:9428/select/vmui",
        "auth_type": "none",
        "username": "",
        "secret": "",
        "verify_tls": True,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "default_step_seconds": DEFAULT_STEP_SECONDS,
    }
    row = db.query(SystemConfig).filter(SystemConfig.config_key == CONFIG_KEY).first()
    current = _safe_load_json_dict(row.config_value if row else "")
    content = dict(current) if current else {}
    for key, value in defaults.items():
        if force or not str(content.get(key) or "").strip():
            # secret/数值类在 force 时也保留用户项，避免覆盖鉴权
            if key in {"secret", "username"} and current:
                content[key] = content.get(key) or value
            else:
                content[key] = value
    updated_at = now_ts()
    if not row:
        row = SystemConfig(config_key=CONFIG_KEY, config_value=json.dumps(content, ensure_ascii=False), updated_at=updated_at)
        db.add(row)
    else:
        row.config_value = json.dumps(content, ensure_ascii=False)
        row.updated_at = updated_at
    return content


def resolve_agent_write_urls(cfg: dict[str, Any]) -> tuple[str, str]:
    """解析采集 Agent 的写入地址（分体部署时与后端查询地址可以不同）。

    优先用显式配置的 remote_write_url / vlogs_ingest_url；
    否则从 vm_url / vlogs_url 推导标准单机端点：
    - 指标：{vm_url}/api/v1/write
    - 日志：{vlogs_url}/insert/loki/api/v1/push（Alloy loki.write；
      旧 ES 写入口 insert/elasticsearch 已废弃，解析时自动改写为 Loki push）
    """
    vm = str(cfg.get("vm_url") or "").strip().rstrip("/")
    vl = str(cfg.get("vlogs_url") or "").strip().rstrip("/")
    remote_write = str(cfg.get("remote_write_url") or "").strip().rstrip("/")
    vlogs_ingest = str(cfg.get("vlogs_ingest_url") or "").strip().rstrip("/")
    if not remote_write:
        remote_write = f"{vm}/api/v1/write" if vm else ""
    if not vlogs_ingest:
        vlogs_ingest = f"{vl}/insert/loki/api/v1/push" if vl else ""
    elif vlogs_ingest.endswith("/insert/elasticsearch") or vlogs_ingest.rstrip("/").endswith("/insert/elasticsearch/"):
        base = vlogs_ingest.rstrip("/")
        if base.endswith("/insert/elasticsearch"):
            base = base[: -len("/insert/elasticsearch")]
        vlogs_ingest = f"{base.rstrip('/')}/insert/loki/api/v1/push"
    return remote_write, vlogs_ingest


def _validate_write_url_format(url: str, field_name: str) -> None:
    """校验写入地址必须是 HTTP(S)（空值表示自动推导，放行）。"""
    value = str(url or "").strip()
    if not value:
        return
    if not value.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是 HTTP(S) 地址")


def _probe_write_url(url: str, name: str, cfg: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
    """探测写入地址可达性。

    任意 HTTP 响应（含 405/400）都算「端点存在」；只有连接失败才判不可达。
    分体部署下 Agent 写入地址可能从后端不可达，这里只做提示、不阻断保存。
    """
    value = str(url or "").strip()
    if not value:
        return {
            "name": name,
            "url": "",
            "ok": False,
            "message": "未配置",
            "hint": f"请填写{name}，或配置 VictoriaLogs/VictoriaMetrics 地址以便自动推导",
        }
    try:
        resp = requests.get(
            value,
            timeout=timeout,
            verify=bool(cfg.get("verify_tls", True)),
            allow_redirects=True,
        )
        return {
            "name": name,
            "url": value,
            "ok": True,
            "message": f"HTTP {resp.status_code}（端点可达）",
            "hint": "",
        }
    except requests.RequestException as exc:
        detail = str(exc)
        if "NameResolutionError" in detail or "Failed to resolve" in detail:
            hint = "DNS 无法解析：请确认地址从后端/业务主机可达，分体部署时填业务主机能访问的入口"
        elif "Connection" in detail or "Connect" in detail:
            hint = "连接被拒绝或超时：检查端口、防火墙，以及是否应走 Nginx 18080 代理入口"
        else:
            hint = "请检查地址拼写、网络连通与 TLS 设置"
        return {
            "name": name,
            "url": value,
            "ok": False,
            "message": detail[:200],
            "hint": hint,
        }


def probe_agent_write_urls(cfg: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
    """解析并探测采集 Agent 的指标/日志写入地址。

    供 self-check 与 link-health 展示 resolve_agent_write_urls 结果与可达性。
    """
    remote_write, vlogs_ingest = resolve_agent_write_urls(cfg)
    return {
        "remote_write_url": remote_write,
        "vlogs_ingest_url": vlogs_ingest,
        "remote_write": _probe_write_url(remote_write, "指标写入（Agent）", cfg, timeout),
        "vlogs_ingest": _probe_write_url(vlogs_ingest, "日志写入（Agent）", cfg, timeout),
    }


