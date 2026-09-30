"""VictoriaMetrics / VictoriaLogs 查询代理。

【安全架构：后端代理模式】
 前端从不直接访问 VictoriaMetrics / VictoriaLogs / Grafana，所有查询走后端代理，原因：
1. 隐藏内网地址与认证密钥：vm_url、secret 只保存在后端 SystemConfig 中，前端不可见；
2. 统一鉴权：后端可利用现有 RBAC 体系拦截非法查询，防止普通用户直接 curl /api/v1/query 拉全量数据；
3. 请求归一化：不同 VM 发行版（VictoriaMetrics、Prometheus、Mimir）的 API 路径略有差异，
   后端通过 _normalize_api_base 统一抹平差异，前端无感知。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import requests
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException

from app.core.crypto import decrypt_secret
from app.api.monitoring.constants import (
    AUTH_TYPES,
    CONFIG_KEY,
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_LOG_LIMIT,
)

# 兼容多种 VM 部署形态的 URL 规范化。
# 用户可能填写：http://vm:8428、http://vm:8428/api/v1、http://vm:8428/select/0/prometheus
# 不规范的 URL 会导致查询 404，因此统一补齐为 /api/v1 或 /select/.../prometheus/api/v1。
def _normalize_api_base(vm_url: str) -> str:
    """
    规范化 VictoriaMetrics 查询 API 基地址。

    兼容：
    - 单机版：http://host:8428
    - 单机版显式 prometheus 前缀：http://host:8428/prometheus
    - 集群版 vmselect：http://vmselect:8481/select/0/prometheus
    - 已填完整 API：http://host:8428/api/v1
    """
    base = (vm_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def _normalize_vlogs_base(vlogs_url: str) -> str:
    """规范化 VictoriaLogs 查询基地址。"""
    base = (vlogs_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/select/logsql/query"):
        return base[: -len("/query")]
    if base.endswith("/select/logsql"):
        return base
    return f"{base}/select/logsql"


def _normalize_vlogs_ingest_base(vlogs_ingest_url: str) -> str:
    """
    规范化 VictoriaLogs 写入基地址。

    约定前端填写“主机可访问的写入入口”：
    - http://host:9428
    - http://host:9428/insert/loki/api/v1/push（Alloy loki.write，产品默认）

    旧 ES 入口 insert/elasticsearch 在此改写为 Loki push，避免存量配置把
    Agent 指到已废弃的 Vector sink。
    """
    base = (vlogs_ingest_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/insert/elasticsearch"):
        base = base[: -len("/insert/elasticsearch")].rstrip("/")
        return f"{base}/insert/loki/api/v1/push"
    if base.endswith("/insert/loki/api/v1/push"):
        return f"{base}"
    return f"{base}/insert/loki/api/v1/push"


def _response_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回前端可见配置，不返回密文与明文密钥。

    remote_write_url 允许为空：空表示由 vm_url 推导（见 resolve_agent_write_urls）。
    """
    from app.api.monitoring.services.deployment import resolve_agent_write_urls

    remote_write_url, vlogs_ingest = resolve_agent_write_urls(cfg)
    vm_url = str(cfg.get("vm_url") or "").strip().rstrip("/")
    vlogs_url = str(cfg.get("vlogs_url") or "").strip().rstrip("/")
    return {
        "vm_url": vm_url,
        "vmui_url": str(cfg.get("vmui_url") or "").strip().rstrip("/") or (f"{vm_url}/vmui" if vm_url else ""),
        "grafana_url": str(cfg.get("grafana_url") or ""),
        "vlogs_url": vlogs_url,
        "vlogs_ingest_url": vlogs_ingest,
        "remote_write_url": remote_write_url,
        "agent_write_urls": {
            "remote_write_url": remote_write_url,
            "vlogs_ingest_url": vlogs_ingest,
        },
        "vlogsui_url": str(cfg.get("vlogsui_url") or "").strip().rstrip("/")
        or (f"{vlogs_url}/select/vmui" if vlogs_url else ""),
        "auth_type": str(cfg.get("auth_type") or "none"),
        "username": str(cfg.get("username") or ""),
        "verify_tls": bool(cfg.get("verify_tls", True)),
        "timeout_seconds": int(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        "default_step_seconds": int(cfg.get("default_step_seconds") or DEFAULT_STEP_SECONDS),
        "has_secret": bool(str(cfg.get("secret") or "").strip()),
    }


# 根据监控连接配置生成 requests 参数。
# 为什么把认证逻辑集中在这里？因为 VM 代理、VLogs 代理、Dashboard 服务、Alert Center
# 都需要发起 HTTP 请求，分散在各处会导致密钥处理不一致（如某处漏了解密）。
# 集中后，所有出口请求统一走 decrypt_secret + 认证头构造，降低泄露风险。
def _request_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
    """根据配置生成 requests 参数。"""
    headers: dict[str, str] = {}
    kwargs: dict[str, Any] = {
        "timeout": max(3, int(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)),
        "verify": bool(cfg.get("verify_tls", True)),
        "headers": headers,
    }
    auth_type = str(cfg.get("auth_type") or "none").lower()
    secret = decrypt_secret(str(cfg.get("secret") or ""))
    if auth_type == "basic":
        username = str(cfg.get("username") or "")
        kwargs["auth"] = (username, secret)
    elif auth_type == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    return kwargs


def _build_authorization_header(cfg: dict[str, Any]) -> str:
    """为 Vector 写入目标生成 Authorization 请求头。"""
    auth_type = str(cfg.get("auth_type") or "none").lower()
    secret = decrypt_secret(str(cfg.get("secret") or ""))
    if auth_type == "basic" and str(cfg.get("username") or "").strip():
        raw = f"{str(cfg.get('username') or '').strip()}:{secret}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('ascii')}"
    if auth_type == "bearer" and secret:
        return f"Bearer {secret}"
    return ""


def _vm_get(cfg: dict[str, Any], api_path: str, params: dict[str, Any]) -> dict[str, Any]:
    """调用 VictoriaMetrics 查询接口并返回 JSON。"""
    api_base = _normalize_api_base(str(cfg.get("vm_url") or ""))
    if not api_base:
        raise HTTPException(status_code=400, detail="VictoriaMetrics 查询地址未配置")

    url = f"{api_base}{api_path}"
    try:
        resp = requests.get(url, params=params, **_request_kwargs(cfg))
    except requests.RequestException as exc:
        detail = str(exc)
        # 不把 urllib3 的 DNS / 连接池堆栈直接暴露给前端用户。
        # 如果配置里使用的是 OpsPilot 自带监控容器名，最常见原因是应用后端
        # 重建后没有重新接入 `opspilot-monitoring_default` 网络。
        if "NameResolutionError" in detail or "Failed to resolve" in detail:
            detail = "VictoriaMetrics 连接失败：应用后端无法解析监控服务地址，请确认应用容器已接入监控网络或改用可达的监控网关地址"
        else:
            detail = f"VictoriaMetrics 连接失败：{detail}"
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"VictoriaMetrics 返回非法响应（HTTP {resp.status_code}）")

    if resp.status_code >= 400 or payload.get("status") == "error":
        message = str(payload.get("error") or payload.get("message") or "").strip()
        raise HTTPException(status_code=502, detail=message or f"VictoriaMetrics 查询失败（HTTP {resp.status_code}）")
    return payload


def _vlogs_get(cfg: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    """调用 VictoriaLogs 查询接口，兼容 JSON 与 JSON Lines 响应。"""
    api_base = _normalize_vlogs_base(str(cfg.get("vlogs_url") or ""))
    if not api_base:
        raise HTTPException(status_code=400, detail="VictoriaLogs 查询地址未配置")
    try:
        resp = requests.get(f"{api_base}/query", params=params, **_request_kwargs(cfg))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs 连接失败: {str(exc)}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs 查询失败（HTTP {resp.status_code}）")

    try:
        payload = resp.json()
    except Exception:
        payload = None

    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("data") or payload.get("result") or payload.get("hits") or []
        rows = raw_rows if isinstance(raw_rows, list) else []
    else:
        rows = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                rows.append({"_msg": line})

    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[:MAX_LOG_LIMIT], start=1):
        item = row if isinstance(row, dict) else {"_msg": str(row)}
        raw_message = str(item.get("_msg") or "").strip()
        if raw_message.startswith("missing _msg field") and item.get("log"):
            raw_message = str(item.get("log") or "").strip()
        normalized.append(
            {
                "id": str(item.get("_stream_id") or item.get("id") or f"log-{idx}"),
                "time": str(item.get("_time") or item.get("timestamp") or item.get("time") or "-"),
                "level": str(item.get("level") or item.get("severity") or item.get("log_level") or "INFO").upper(),
                "service": str(item.get("service") or item.get("app") or item.get("job") or "-"),
                "host": str(item.get("host") or item.get("hostname") or item.get("instance") or "-"),
                # Alloy 采集 Docker JSON 日志时，历史数据可能仍以 log 字段保存。
                # 查询页优先展示真实日志正文，避免把 VictoriaLogs 的 missing _msg 提示当业务日志展示。
                "message": str(raw_message or item.get("message") or item.get("msg") or item.get("log") or item),
                "fields": item,
            }
        )
    return normalized


