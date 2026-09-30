"""监控大盘数据解析与过滤。

【复杂性来源】
 大盘不是简单的"把 VM 返回数据透传给前端"，需要做多层转换：
1. PromQL 矩阵/向量解析：VictoriaMetrics 返回的 data.result 结构随查询类型变化，
   需区分 matrix（时序折线）与 vector（瞬时打点）；
2. 范围裁剪：用户可能请求 30 天数据，但面板宽度只有 1200px，过细的时间粒度无意义且消耗带宽，
   因此 _clamp_dashboard_range 自动把 step 调粗；
3. 主机维度聚合：TopN 主机列表需要对 instance 标签做 group by，再按指标排序，
   这涉及多次查询合并与排序截断。
"""
from __future__ import annotations

import json
import re
import hashlib
from typing import Any
import requests

from datetime import datetime, timedelta
from app.db.database import SessionLocal
from app.models.monitoring_active_alert import MonitoringActiveAlert
from sqlalchemy.orm import Session


from app.api.monitoring.constants import (
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    FS_SIZE_QUERY,
    FS_USED_PCT_TOP_QUERY,
    LOAD1_AVG_QUERY,
    MAX_DASHBOARD_RANGE_MINUTES,
    MAX_DASHBOARD_STEP_SECONDS,
    MIN_DASHBOARD_RANGE_MINUTES,
    MIN_DASHBOARD_STEP_SECONDS,
    TOP_HOST_QUERIES,
)

__all__ = [
    "_clamp_dashboard_range",
    "_fetch_alerts",
    "_fetch_summary_load1",
    "_fetch_top_filesystems",
    "_fetch_top_hosts",
    "_filter_dashboard_alerts",
    "_filter_dashboard_targets",
    "_filter_logs_by_scope",
    "_filter_metric_instances",
    "_parse_instant_rows",
    "_parse_matrix_first_series",
    "_parse_up_targets",
    "_parse_vector_by_instance",
    "_safe_metric_float",
    "_split_scope_param",
    "_matches_any_scope",
    "logsql_quote",
    "prom_instance_regex",
    "build_logs_scope_clause",
]
from app.api.monitoring.services.proxy import _request_kwargs, _vm_get

def _parse_matrix_first_series(payload: dict[str, Any]) -> list[tuple[int, float]]:
    """解析 query_range 返回的第一条时间序列。"""
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list) or not result:
        return []
    values = result[0].get("values", [])
    parsed: list[tuple[int, float]] = []
    for point in values:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            parsed.append((int(float(point[0])), round(float(point[1]), 2)))
        except Exception:
            continue
    return parsed


def _safe_metric_float(raw_value: Any) -> float | None:
    """安全解析 VictoriaMetrics 返回的数值，非法值直接忽略。"""
    try:
        return round(float(raw_value), 2)
    except (TypeError, ValueError):
        return None


def _parse_vector_by_instance(payload: dict[str, Any]) -> dict[str, float]:
    """解析 instant query 结果，按 instance 聚合最新指标值。"""
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return {}

    parsed: dict[str, float] = {}
    for item in result:
        metric = item.get("metric") if isinstance(item, dict) else {}
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            continue
        instance = str(metric.get("instance") or "").strip()
        metric_value = _safe_metric_float(value[1])
        if instance and metric_value is not None:
            parsed[instance] = metric_value
    return parsed


def _parse_instant_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 instant query 为「标签 + 数值」行列表，供 TopN / 摘要拼装使用。"""
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in result:
        metric = item.get("metric") if isinstance(item, dict) else {}
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            continue
        parsed_value = _safe_metric_float(value[1])
        if parsed_value is None:
            continue
        rows.append({"metric": metric, "value": parsed_value})
    return rows


def _fetch_summary_load1(cfg: dict[str, Any]) -> float | None:
    """全局 Load1 均值；查询失败返回 None（前端显示占位，不 500）。"""
    try:
        payload = _vm_get(cfg, "/query", {"query": LOAD1_AVG_QUERY, "round_digits": 2})
        rows = _parse_instant_rows(payload)
        return rows[0]["value"] if rows else None
    except Exception:
        return None


def _fetch_top_filesystems(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """返回挂载点占用率 Top5（含总容量 / 已用%），PromQL 失败时返回空列表。"""
    try:
        used_payload = _vm_get(cfg, "/query", {"query": FS_USED_PCT_TOP_QUERY, "round_digits": 2})
    except Exception:
        return []

    used_rows = _parse_instant_rows(used_payload)
    used_rows.sort(key=lambda row: float(row.get("value") or 0), reverse=True)
    used_rows = used_rows[:5]

    sizes: dict[tuple[str, str, str], float] = {}
    try:
        size_payload = _vm_get(cfg, "/query", {"query": FS_SIZE_QUERY, "round_digits": 0})
        for row in _parse_instant_rows(size_payload):
            metric = row["metric"]
            key = (
                str(metric.get("instance") or ""),
                str(metric.get("mountpoint") or ""),
                str(metric.get("device") or ""),
            )
            sizes[key] = float(row["value"])
    except Exception:
        sizes = {}

    top_rows: list[dict[str, Any]] = []
    for row in used_rows:
        metric = row["metric"]
        instance = str(metric.get("instance") or "")
        mountpoint = str(metric.get("mountpoint") or "")
        device = str(metric.get("device") or "")
        used_pct = float(row["value"])
        size_val = sizes.get((instance, mountpoint, device))
        size_bytes = int(size_val) if size_val is not None else None
        used_bytes = int(size_val * used_pct / 100) if size_val is not None else None
        top_rows.append(
            {
                "instance": instance,
                "host": instance.split(":", maxsplit=1)[0],
                "mountpoint": mountpoint,
                "device": device,
                "fstype": str(metric.get("fstype") or ""),
                "used_pct": used_pct,
                "size_bytes": size_bytes,
                "used_bytes": used_bytes,
            }
        )
    return top_rows


# 将用户请求的时间范围与粒度裁剪到合理区间。
# 业务逻辑：面板像素有限，若请求 24h 数据却用 5s step，返回的点数 = 17280，远超 SVG 渲染能力。
# 因此强制 step >= max(30, range_minutes/120)，保证最多约 240 个点，兼顾精度与性能。
def _clamp_dashboard_range(range_minutes: int, step_seconds: int) -> tuple[int, int]:
    """钳制大盘查询窗口，避免前端误传超大范围拖垮 VictoriaMetrics。"""
    safe_range = max(MIN_DASHBOARD_RANGE_MINUTES, min(int(range_minutes), MAX_DASHBOARD_RANGE_MINUTES))
    safe_step = max(MIN_DASHBOARD_STEP_SECONDS, min(int(step_seconds), MAX_DASHBOARD_STEP_SECONDS))
    return safe_range, safe_step


def _parse_up_targets(payload: dict[str, Any], ip_to_asset: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """把 up 查询结果转换为页面服务状态，支持从数据库资产中匹配主机名称、云厂商与地域。"""
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return []
    targets: list[dict[str, Any]] = []
    for item in result:
        metric = item.get("metric") if isinstance(item, dict) else {}
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            continue
        instance = str(metric.get("instance") or "-")
        job = str(metric.get("job") or "target")
        ip = instance.split(":", maxsplit=1)[0]
        
        name = None
        provider = "unknown"
        region = "default"
        if ip_to_asset and ip in ip_to_asset:
            asset_meta = ip_to_asset[ip]
            name = asset_meta.get("name")
            provider = asset_meta.get("provider") or "unknown"
            region = asset_meta.get("region") or "default"
            
        if not name:
            name = metric.get("host") or job
            
        try:
            is_up = float(value[1]) >= 1
        except Exception:
            is_up = False
        targets.append(
            {
                "name": str(name),
                "status": "healthy" if is_up else "offline",
                "latency": "-",
                "qps": 0,
                "instance": instance,
                "provider": provider,
                "region": region,
            }
        )
    return targets


def _split_scope_param(raw_value: Any) -> list[str]:
    """解析应用跳转传入的逗号分隔过滤条件。"""
    # 该函数会被路由和内部聚合函数同时调用。内部调用时如果遗漏关键字参数，
    # FastAPI 的 Query 默认对象可能透传进来；这里兜底为空，避免大盘整体报错。
    if not isinstance(raw_value, str):
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


_IP_LIKE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _matches_any_scope(value: str, candidates: list[str]) -> bool:
    """判断文本是否命中任意过滤值，大小写不敏感。

    IP 按数字/点号边界精确匹配，避免 192.168.1.1 误命中 192.168.1.10；
    主机名/服务名仍用子串匹配，保留模糊筛选能力。
    """
    normalized = (value or "").lower()
    if not normalized:
        return False
    for item in candidates:
        needle = str(item or "").lower().strip()
        if not needle:
            continue
        if _IP_LIKE.match(needle):
            if re.search(rf"(?<![0-9.]){re.escape(needle)}(?![0-9.])", normalized):
                return True
        elif needle in normalized:
            return True
    return False


def prom_instance_regex(*values: str) -> str:
    """构造 PromQL instance=~ 的正则：精确匹配主机，可选 ``:port`` 后缀。

    - 点号用 ``[.]`` 字符类而非 ``\\.``（本环境 VictoriaMetrics 对反斜杠转义返回 422）；
    - 后缀用 ``(:.*)?`` 而不是 ``.*``，避免 ``192.168.1.1`` 误匹配 ``192.168.1.10``；
    - 其余正则元字符走 ``re.escape``，防止主机名注入破坏表达式。
    """
    parts: list[str] = []
    for raw in values:
        s = str(raw or "").strip()
        if not s:
            continue
        parts.append(re.escape(s).replace(r"\.", "[.]"))
    if not parts:
        return ""
    alt = parts[0] if len(parts) == 1 else f"({'|'.join(parts)})"
    return f"{alt}(:.*)?"


def logsql_quote(value: str) -> str:
    """把单个取值安全写入 LogsQL ``field:value`` 片段。"""
    s = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_logs_scope_clause(host_ips: list[str], host_names: list[str]) -> str:
    """把主机 IP / 名字编成 LogsQL 作用域子句（OR 连接）。空范围返回空串。"""
    parts: list[str] = []
    for raw in list(host_ips or []) + list(host_names or []):
        s = str(raw or "").strip()
        if not s:
            continue
        token = logsql_quote(s)
        parts.append(f"(instance:{token} OR host:{token} OR hostname:{token})")
    return " OR ".join(parts)


def _filter_dashboard_targets(
    targets: list[dict[str, Any]],
    host_ips: list[str],
    host_names: list[str],
) -> list[dict[str, Any]]:
    """按应用绑定主机过滤实时大盘 target。"""
    if not host_ips and not host_names:
        return targets
    return [
        item
        for item in targets
        if _matches_any_scope(str(item.get("instance") or ""), host_ips + host_names)
        or _matches_any_scope(str(item.get("name") or ""), host_names)
    ]


def _filter_metric_instances(
    instances: list[str],
    host_ips: list[str],
    host_names: list[str],
) -> list[str]:
    """按应用范围过滤指标实例，保持和 target 过滤逻辑一致。"""
    if not host_ips and not host_names:
        return instances
    return [
        instance
        for instance in instances
        if _matches_any_scope(instance, host_ips + host_names)
        or _matches_any_scope(instance.split(":", maxsplit=1)[0], host_names)
    ]


def _fetch_top_hosts(
    cfg: dict[str, Any],
    host_ips: list[str],
    host_names: list[str],
) -> list[dict[str, Any]]:
    """查询 CPU / 内存 / 磁盘消耗的 Top 主机，返回扁平行列表。"""
    metrics_by_kind: dict[str, dict[str, float]] = {}
    for kind, query in TOP_HOST_QUERIES.items():
        try:
            payload = _vm_get(cfg, "/query", {"query": query, "round_digits": 2})
            metrics_by_kind[kind] = _parse_vector_by_instance(payload)
        except Exception:
            metrics_by_kind[kind] = {}

    instances = sorted({instance for values in metrics_by_kind.values() for instance in values.keys()})
    instances = _filter_metric_instances(instances, host_ips, host_names)
    
    cpu_rows: list[dict[str, Any]] = []
    memory_rows: list[dict[str, Any]] = []
    disk_rows: list[dict[str, Any]] = []
    
    for instance in instances:
        cpu = metrics_by_kind.get("cpu", {}).get(instance, 0)
        memory = metrics_by_kind.get("memory", {}).get(instance, 0)
        disk = metrics_by_kind.get("disk", {}).get(instance, 0)
        network = metrics_by_kind.get("network", {}).get(instance, 0)
        
        name = instance.split(":", maxsplit=1)[0]
        
        cpu_rows.append({"instance": instance, "name": name, "value": round(cpu, 2), "network": round(network, 2)})
        memory_rows.append({"instance": instance, "name": name, "value": round(memory, 2), "network": round(network, 2)})
        disk_rows.append({"instance": instance, "name": name, "value": round(disk, 2), "network": round(network, 2)})

    # 扁平合并为「Top 主机」列表（前端 Table 直接吃数组）：
    # 按实例聚合 cpu/memory/disk，按最高占用排序取前 8
    merged: dict[str, dict[str, Any]] = {}
    for row in cpu_rows:
        merged[row["instance"]] = {
            "name": row["name"],
            "ip": row["instance"].split(":", maxsplit=1)[0],
            "cpu": row["value"],
            "memory": 0.0,
            "disk": 0.0,
        }
    for row in memory_rows:
        item = merged.setdefault(row["instance"], {
            "name": row["name"], "ip": row["instance"].split(":", maxsplit=1)[0],
            "cpu": 0.0, "memory": 0.0, "disk": 0.0,
        })
        item["memory"] = row["value"]
    for row in disk_rows:
        item = merged.setdefault(row["instance"], {
            "name": row["name"], "ip": row["instance"].split(":", maxsplit=1)[0],
            "cpu": 0.0, "memory": 0.0, "disk": 0.0,
        })
        item["disk"] = row["value"]

    ranked = sorted(
        merged.values(),
        key=lambda x: max(float(x.get("cpu") or 0), float(x.get("memory") or 0), float(x.get("disk") or 0)),
        reverse=True,
    )[:8]
    return ranked


def _filter_dashboard_alerts(
    alerts: list[dict[str, Any]],
    host_ips: list[str],
    host_names: list[str],
) -> list[dict[str, Any]]:
    """按应用绑定主机过滤活动告警。

    只匹配 host 字段（包含 instance IP 和主机名），
    不匹配 value（description 文本），避免 job 名等关键词导致误命中。
    """
    if not host_ips and not host_names:
        return alerts
    return [
        item
        for item in alerts
        if _matches_any_scope(str(item.get("host") or ""), host_ips + host_names)
    ]


def _filter_logs_by_scope(
    logs: list[dict[str, Any]],
    host_ips: list[str],
    host_names: list[str],
    services: list[str],
) -> list[dict[str, Any]]:
    """按应用观测上下文过滤日志结果。"""
    if not host_ips and not host_names and not services:
        return logs
    result: list[dict[str, Any]] = []
    for item in logs:
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        host_text = " ".join(
            [
                str(item.get("host") or ""),
                str(fields.get("host") or ""),
                str(fields.get("hostname") or ""),
                str(fields.get("instance") or ""),
                str(fields.get("asset_id") or ""),
            ]
        )
        service_text = " ".join([str(item.get("service") or ""), str(fields.get("service") or ""), str(fields.get("app") or "")])
        if _matches_any_scope(host_text, host_ips + host_names) or _matches_any_scope(service_text, services):
            result.append(item)
    return result


def _calculate_fingerprint(name: str, instance: str, labels: dict[str, str]) -> str:
    """计算告警的唯一哈希指纹，用于在数据库中去重。"""
    # 过滤掉变化字段，保证指纹稳定
    core_labels = sorted([(k, str(v)) for k, v in labels.items() if k not in ("alertstate", "activeAt", "id")])
    label_str = json.dumps(core_labels)
    src = f"{name}:{instance}:{label_str}"
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:32]


def _fetch_alerts(cfg: dict[str, Any], db: Session | None = None) -> list[dict[str, Any]]:
    """读取活动告警。

    【自建告警引擎架构】活动告警(firing/pending)由内置告警引擎
    (app/services/alert_engine/)在每个评估周期写入 monitoring_active_alert 表,
    此处只需直接读库即可,不再发起任何外部 HTTP 查询。
    """
    # 准备数据库 Session，若未传入则自动新建并关闭
    local_session = False
    if db is None:
        db = SessionLocal()
        local_session = True

    try:
        cached_alerts = (
            db.query(MonitoringActiveAlert)
            .order_by(MonitoringActiveAlert.first_seen_at.desc())
            .all()
        )
        alerts: list[dict[str, Any]] = []
        for db_alert in cached_alerts:
            labels_dict = {}
            try:
                labels_dict = json.loads(db_alert.labels or "{}")
            except Exception:
                pass
            hostname = str(labels_dict.get("host") or "")
            host_display = (
                f"{db_alert.instance} ({hostname})"
                if hostname and hostname != db_alert.instance
                else db_alert.instance
            )
            alerts.append(
                {
                    "id": db_alert.fingerprint,
                    "rule": db_alert.name,
                    # 只增不减：补齐「最近告警」卡片要的 name/instance 规范字段
                    "name": db_alert.name,
                    "instance": db_alert.instance,
                    "host": host_display,
                    "value": db_alert.description,
                    "time": db_alert.active_at,
                    "severity": db_alert.severity,
                    "status": db_alert.state,
                }
            )
        return alerts
    except Exception:
        if db:
            db.rollback()
        return []
    finally:
        if local_session:
            db.close()


