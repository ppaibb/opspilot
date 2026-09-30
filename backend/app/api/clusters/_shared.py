"""Shared caches, kubeconfig clients, YAML helpers, and diagnosis utils."""

import logging
import os
import threading
import time
from typing import Any, Callable, List, Optional

import yaml
from fastapi import HTTPException
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.stream import stream as k8s_stream
from pydantic import BaseModel, Field

from app.core.filesystem import ensure_private_directory

from ._schemas import ResourceFormPatchRequest

KUBECONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "kubeconfigs")

ensure_private_directory(KUBECONFIG_DIR)

logger = logging.getLogger(__name__)

RESOURCE_MAP = {
    "Deployment":            ("apps", "v1", "deployments"),
    "StatefulSet":           ("apps", "v1", "statefulsets"),
    "DaemonSet":             ("apps", "v1", "daemonsets"),
    "ReplicaSet":            ("apps", "v1", "replicasets"),
    "Service":               ("", "v1", "services"),
    "Endpoints":             ("", "v1", "endpoints"),
    "ConfigMap":             ("", "v1", "configmaps"),
    "Secret":                ("", "v1", "secrets"),
    "Namespace":             ("", "v1", "namespaces"),
    "Node":                  ("", "v1", "nodes"),
    "Pod":                   ("", "v1", "pods"),
    "PersistentVolumeClaim": ("", "v1", "persistentvolumeclaims"),
    "PersistentVolume":      ("", "v1", "persistentvolumes"),
    "ServiceAccount":        ("", "v1", "serviceaccounts"),
    "ResourceQuota":         ("", "v1", "resourcequotas"),
    "LimitRange":            ("", "v1", "limitranges"),
    "Ingress":               ("networking.k8s.io", "v1", "ingresses"),
    "IngressClass":          ("networking.k8s.io", "v1", "ingressclasses"),
    "NetworkPolicy":         ("networking.k8s.io", "v1", "networkpolicies"),
    "EndpointSlice":         ("discovery.k8s.io", "v1", "endpointslices"),
    "CronJob":               ("batch", "v1", "cronjobs"),
    "Job":                   ("batch", "v1", "jobs"),
    "Role":                  ("rbac.authorization.k8s.io", "v1", "roles"),
    "ClusterRole":           ("rbac.authorization.k8s.io", "v1", "clusterroles"),
    "RoleBinding":           ("rbac.authorization.k8s.io", "v1", "rolebindings"),
    "ClusterRoleBinding":    ("rbac.authorization.k8s.io", "v1", "clusterrolebindings"),
    "HorizontalPodAutoscaler": ("autoscaling", "v2", "horizontalpodautoscalers"),
    "PodDisruptionBudget":   ("policy", "v1", "poddisruptionbudgets"),
    "StorageClass":          ("storage.k8s.io", "v1", "storageclasses"),
    "PriorityClass":         ("scheduling.k8s.io", "v1", "priorityclasses"),
    "Event":                 ("events.k8s.io", "v1", "events"),
    "CustomResourceDefinition": ("apiextensions.k8s.io", "v1", "customresourcedefinitions"),
    "APIService":            ("apiregistration.k8s.io", "v1", "apiservices"),
    "CertificateSigningRequest": ("certificates.k8s.io", "v1", "certificatesigningrequests"),
    "MutatingWebhookConfiguration": ("admissionregistration.k8s.io", "v1", "mutatingwebhookconfigurations"),
    "ValidatingWebhookConfiguration": ("admissionregistration.k8s.io", "v1", "validatingwebhookconfigurations"),
    "RuntimeClass":          ("node.k8s.io", "v1", "runtimeclasses"),
    "Lease":                 ("coordination.k8s.io", "v1", "leases"),
}

def parse_k8s_cpu(cpu_str: str) -> float:
    if not cpu_str:
        return 0.0
    cpu_str = str(cpu_str).strip()
    if cpu_str.endswith("m"):
        try:
            return float(cpu_str[:-1]) / 1000.0
        except ValueError:
            return 0.0
    try:
        return float(cpu_str)
    except ValueError:
        return 0.0

def parse_k8s_memory_to_gib(mem_str: str) -> float:
    if not mem_str:
        return 0.0
    mem_str = str(mem_str).strip()
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "Pi": 1024 ** 5,
        "Ei": 1024 ** 6,
        "k": 1000,
        "m": 1000 ** 2,
        "g": 1000 ** 3,
        "t": 1000 ** 4,
        "p": 1000 ** 5,
        "e": 1000 ** 6,
    }
    for unit, multiplier in units.items():
        if mem_str.endswith(unit):
            try:
                val = float(mem_str[:-len(unit)].strip())
                bytes_val = val * multiplier
                return bytes_val / (1024 ** 3)
            except ValueError:
                return 0.0
    try:
        return float(mem_str) / (1024 ** 3)
    except ValueError:
        return 0.0

def _parse_cpu_quantity(value: Any) -> int:
    """把 Kubernetes cpu 配额字符串转成 millicores。

    metrics-server 最常见的两种：
    - `1234n` = 1234 纳核 = 1234/1_000_000 millicores；
    - `1234u` = 1234 微核 = 1234/1000 millicores；
    - `1234m` = 1234 millicores；
    - `1` / `1.5` = 核心数（整数或小数）。
    失败返回 0，不抛异常。
    """
    if value in (None, ""):
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        if text.endswith("n"):
            return int(float(text[:-1]) / 1_000_000)
        if text.endswith("u"):
            return int(float(text[:-1]) / 1_000)
        if text.endswith("m"):
            return int(float(text[:-1]))
        if text.endswith("k"):
            return int(float(text[:-1]) * 1_000_000)
        return int(float(text) * 1000)
    except (TypeError, ValueError):
        return 0

def _parse_memory_quantity(value: Any) -> int:
    """把 Kubernetes memory quantity 字符串转成字节数。

    支持 `Ki/Mi/Gi/Ti/Pi/Ei` 以及 `k/M/G/T/P/E` 和纯数字。
    失败返回 0。
    """
    if value in (None, ""):
        return 0
    text = str(value).strip()
    if not text:
        return 0
    binary_units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6}
    decimal_units = {"k": 1_000, "M": 1_000_000, "G": 1_000_000_000, "T": 1_000_000_000_000, "P": 1_000_000_000_000_000, "E": 1_000_000_000_000_000_000}
    try:
        for suffix, multiplier in binary_units.items():
            if text.endswith(suffix):
                return int(float(text[: -len(suffix)]) * multiplier)
        if len(text) >= 2 and text[-1] in decimal_units:
            return int(float(text[:-1]) * decimal_units[text[-1]])
        return int(float(text))
    except (TypeError, ValueError):
        return 0

def _sum_container_metrics(containers: list[dict[str, Any]]) -> tuple[int, int]:
    """把 PodMetrics 的 containers 数组聚合为总 cpu_millicores / memory_bytes。"""
    cpu = 0
    memory = 0
    for container in containers or []:
        usage = container.get("usage") or {}
        cpu += _parse_cpu_quantity(usage.get("cpu"))
        memory += _parse_memory_quantity(usage.get("memory"))
    return cpu, memory

def _metrics_not_available(message: str = "") -> dict[str, Any]:
    return {"available": False, "message": message or "metrics-server 未安装或暂不可用", "items": []}

def _load_cluster_kubeconfig(cluster_id: str) -> str:
    """加载集群 kubeconfig 文件路径，不存在则抛 404。"""
    kc_path = os.path.join(KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        raise HTTPException(status_code=404, detail=f"集群 [{cluster_id}] 的 kubeconfig 不存在，请重新纳管")
    return kc_path

CLIENT_CACHE_TTL_SECONDS = 300

K8S_READ_CACHE_TTL_SECONDS = 5

_client_cache: dict[str, dict[str, Any]] = {}

_dynamic_client_cache: dict[str, dict[str, Any]] = {}

_k8s_read_cache: dict[tuple[str, str, tuple[Any, ...]], dict[str, Any]] = {}

_k8s_read_inflight: dict[tuple[str, str, tuple[Any, ...]], threading.Event] = {}

_client_cache_lock = threading.Lock()

_k8s_read_cache_lock = threading.Lock()

def _clear_cluster_client_cache(cluster_id: str | None = None) -> None:
    """清理 Kubernetes 客户端缓存。

    删除集群、重新纳管或测试结束时必须清理缓存，避免继续使用旧 kubeconfig
    创建的连接对象。cluster_id 为空时清理全部缓存。
    """
    with _client_cache_lock:
        if cluster_id is None:
            _client_cache.clear()
            _dynamic_client_cache.clear()
            _clear_k8s_read_cache()
            return
        _client_cache.pop(cluster_id, None)
        _dynamic_client_cache.pop(cluster_id, None)
    _clear_k8s_read_cache(cluster_id)

def _cache_entry_is_valid(entry: dict[str, Any] | None, mtime: float) -> bool:
    """判断缓存是否仍可用。mtime 变化或 TTL 过期都会触发重建。"""
    if not entry:
        return False
    if entry.get("mtime") != mtime:
        return False
    return float(entry.get("expires_at") or 0) > time.time()

def _get_kubeconfig_mtime(cluster_id: str) -> float:
    """读取 kubeconfig 修改时间，作为缓存隔离版本。

    说明：
    1. 正常 API 路径会先通过 `_load_cluster_kubeconfig` 校验文件存在；
    2. 单元测试可直接调用缓存工具，因此这里对不存在文件返回 0。
    """
    kc_path = os.path.join(KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        return 0
    return os.path.getmtime(kc_path)

def _clear_k8s_read_cache(cluster_id: str | None = None) -> None:
    """清理 Kubernetes 只读结果缓存。

    写操作、kubeconfig 变化或测试清理时调用。cluster_id 为空时清理全部缓存；
    指定集群时仅删除该集群的读缓存，避免影响其他集群页面。
    """
    with _k8s_read_cache_lock:
        if cluster_id is None:
            _k8s_read_cache.clear()
            _k8s_read_inflight.clear()
            return
        stale_keys = [key for key in _k8s_read_cache if key[0] == cluster_id]
        for key in stale_keys:
            _k8s_read_cache.pop(key, None)
        stale_inflight_keys = [key for key in _k8s_read_inflight if key[0] == cluster_id]
        for key in stale_inflight_keys:
            _k8s_read_inflight.pop(key, None)

def _validate_k8s_selector(raw_value: str | None, field_name: str) -> str:
    """校验 Kubernetes selector 查询参数，避免控制字符和超长输入进入 API Server。"""
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if len(value) > 512:
        raise HTTPException(status_code=400, detail=f"{field_name} 长度不能超过 512")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HTTPException(status_code=400, detail=f"{field_name} 包含非法控制字符")
    return value

def _normalize_k8s_limit(raw_limit: int | None) -> int:
    """归一化 K8s 分页大小，默认 100，最大 500。"""
    if raw_limit is None:
        return 100
    return max(1, min(int(raw_limit), 500))

def _safe_continue_token(raw_value: str | None) -> str:
    """校验 Kubernetes continue token，避免异常长参数进入后端日志和缓存键。"""
    value = str(raw_value or "").strip()
    if len(value) > 2048:
        raise HTTPException(status_code=400, detail="continue_token 长度不能超过 2048")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HTTPException(status_code=400, detail="continue_token 包含非法控制字符")
    return value

def _page_response(items: list[dict[str, Any]], continue_token: str, limit: int, cached: bool = False) -> dict[str, Any]:
    """统一分页响应结构。旧接口默认不使用该结构，避免破坏前端兼容性。"""
    return {
        "items": items,
        "continue_token": continue_token or "",
        "limit": limit,
        "cached": cached,
    }

def _mark_cache_hit(value: Any) -> Any:
    """缓存命中时只给分页 envelope 标记 cached=true，旧列表结构保持不变。"""
    if isinstance(value, dict) and "items" in value and "cached" in value:
        marked = dict(value)
        marked["cached"] = True
        return marked
    return value

def _extract_continue_token(response: Any) -> str:
    """兼容 typed client 对象和 dynamic client dict 的 continue token。"""
    metadata = getattr(response, "metadata", None)
    if metadata is not None:
        return str(getattr(metadata, "_continue", "") or "")
    if isinstance(response, dict):
        raw_metadata = response.get("metadata") or {}
        if isinstance(raw_metadata, dict):
            return str(raw_metadata.get("continue") or raw_metadata.get("_continue") or "")
    return ""

def _read_cache_entry_is_valid(entry: dict[str, Any] | None, mtime: float) -> bool:
    """判断只读结果缓存是否仍可用。"""
    if not entry:
        return False
    if entry.get("mtime") != mtime:
        return False
    return float(entry.get("expires_at") or 0) > time.time()

def _log_k8s_read_metric(
    cluster_id: str,
    operation: str,
    namespace: str,
    elapsed_ms: float,
    item_count: int,
    cache_hit: bool,
) -> None:
    """记录 K8s 只读调用耗时，用于定位慢查询和缓存命中率。"""
    logger.info(
        "k8s_read cluster_id=%s operation=%s namespace=%s elapsed_ms=%.1f items=%s cache_hit=%s",
        cluster_id,
        operation,
        namespace,
        elapsed_ms,
        item_count,
        cache_hit,
    )

def _get_or_set_k8s_read_cache(
    cluster_id: str,
    operation: str,
    params: tuple[Any, ...],
    loader: Callable[[], Any],
    ttl_seconds: int = K8S_READ_CACHE_TTL_SECONDS,
) -> Any:
    """获取或写入 K8s 只读结果短缓存。

    安全边界：
    1. 缓存键包含 cluster_id、operation 和查询参数，避免跨集群串数据；
    2. Secret 列表仍由调用方负责裁剪，禁止把 data/stringData 放进缓存；
    3. TTL 很短，写操作会主动清理对应集群缓存。
    """
    cache_key = (cluster_id, operation, params)
    mtime = _get_kubeconfig_mtime(cluster_id)
    started_at = time.perf_counter()
    is_loader = False
    event: threading.Event | None = None

    with _k8s_read_cache_lock:
        entry = _k8s_read_cache.get(cache_key)
        if _read_cache_entry_is_valid(entry, mtime):
            value = _mark_cache_hit(entry["value"])
            _log_k8s_read_metric(
                cluster_id,
                operation,
                str(params[0]) if params else "-",
                (time.perf_counter() - started_at) * 1000,
                len(value) if isinstance(value, list) else 1,
                True,
            )
            return value
        event = _k8s_read_inflight.get(cache_key)
        if event is None:
            event = threading.Event()
            _k8s_read_inflight[cache_key] = event
            is_loader = True

    if not is_loader and event is not None:
        event.wait(timeout=10)
        with _k8s_read_cache_lock:
            entry = _k8s_read_cache.get(cache_key)
            if _read_cache_entry_is_valid(entry, mtime):
                value = _mark_cache_hit(entry["value"])
                _log_k8s_read_metric(
                    cluster_id,
                    operation,
                    str(params[0]) if params else "-",
                    (time.perf_counter() - started_at) * 1000,
                    len(value) if isinstance(value, list) else 1,
                    True,
                )
                return value

    try:
        value = loader()
        with _k8s_read_cache_lock:
            _k8s_read_cache[cache_key] = {
                "value": value,
                "mtime": mtime,
                "expires_at": time.time() + ttl_seconds,
            }
        _log_k8s_read_metric(
            cluster_id,
            operation,
            str(params[0]) if params else "-",
            (time.perf_counter() - started_at) * 1000,
            len(value) if isinstance(value, list) else 1,
            False,
        )
        return value
    finally:
        if is_loader and event is not None:
            with _k8s_read_cache_lock:
                _k8s_read_inflight.pop(cache_key, None)
                event.set()

def _get_api_client(cluster_id: str):
    """获取带生命周期控制的 Kubernetes ApiClient。

    这里用 `new_client_from_config` 避免污染 kubernetes 全局默认配置；
    缓存只用于减少重复解析 kubeconfig 和重复 discovery 前的连接成本。
    """
    kc_path = _load_cluster_kubeconfig(cluster_id)
    mtime = os.path.getmtime(kc_path)
    with _client_cache_lock:
        entry = _client_cache.get(cluster_id)
        if _cache_entry_is_valid(entry, mtime):
            return entry["client"]

        api_client = config.new_client_from_config(kc_path)
        _client_cache[cluster_id] = {
            "client": api_client,
            "mtime": mtime,
            "expires_at": time.time() + CLIENT_CACHE_TTL_SECONDS,
        }
        # ApiClient 变化后 DynamicClient 的 discovery 缓存也必须失效。
        _dynamic_client_cache.pop(cluster_id, None)
        return api_client

def _get_dynamic_client(cluster_id: str) -> DynamicClient:
    """获取带 TTL 和 kubeconfig mtime 校验的 DynamicClient。"""
    kc_path = _load_cluster_kubeconfig(cluster_id)
    mtime = os.path.getmtime(kc_path)
    with _client_cache_lock:
        entry = _dynamic_client_cache.get(cluster_id)
        if _cache_entry_is_valid(entry, mtime):
            return entry["client"]

    api_client = _get_api_client(cluster_id)
    dynamic_client = DynamicClient(api_client)
    with _client_cache_lock:
        _dynamic_client_cache[cluster_id] = {
            "client": dynamic_client,
            "mtime": mtime,
            "expires_at": time.time() + CLIENT_CACHE_TTL_SECONDS,
        }
    return dynamic_client

def _parse_multi_docs(yaml_content: str) -> list:
    """解析多文档 YAML，校验基本结构，返回文档列表。"""
    if not yaml_content or len(yaml_content.strip()) < 10:
        raise HTTPException(status_code=400, detail="YAML 内容为空或过短")
    if len(yaml_content.encode("utf-8")) > 512 * 1024 * 20:
        raise HTTPException(status_code=400, detail="YAML 内容超出总大小限制（10MB）")
    docs = list(yaml.safe_load_all(yaml_content))
    docs = [d for d in docs if d is not None]
    if len(docs) == 0:
        raise HTTPException(status_code=400, detail="未解析到有效的资源文档")
    if len(docs) > 20:
        raise HTTPException(status_code=400, detail=f"单次操作最多 20 个资源文档，当前 {len(docs)} 个")
    for i, doc in enumerate(docs):
        for field in ("apiVersion", "kind", "metadata"):
            if field not in doc:
                raise HTTPException(status_code=400, detail=f"第 {i + 1} 个文档缺少必填字段: {field}")
        if "name" not in doc.get("metadata", {}):
            raise HTTPException(status_code=400, detail=f"第 {i + 1} 个文档缺少 metadata.name")
    return docs

def _resolve_resource(dyn, doc: dict):
    """根据文档的 apiVersion + kind 定位 dynamic client 的 Resource 对象。"""
    kind = doc["kind"]
    api_version = doc["apiVersion"]
    if kind in RESOURCE_MAP:
        group, version, plural = RESOURCE_MAP[kind]
        return dyn.resources.get(api_version=f"{group}/{version}" if group else version, kind=kind)
    try:
        return dyn.resources.get(api_version=api_version, kind=kind)
    except Exception:
        raise HTTPException(status_code=400, detail=f"无法定位资源类型 [{kind}]（apiVersion: {api_version}）")

def _normalize_kind(kind: str) -> str:
    """把各种大小写形式的 kind (如 'service', 'deployment') 规范化为正确的驼峰形式 (如 'Service', 'Deployment')"""
    if not kind:
        return kind
    kind_lower = kind.lower()
    for k in RESOURCE_MAP:
        if k.lower() == kind_lower:
            return k
    return kind[0].upper() + kind[1:] if len(kind) > 1 else kind.upper()

def _resolve_dynamic_resource(dyn, kind: str):
    """按 Kind 获取 dynamic resource，集中处理资源定位错误。"""
    kind = _normalize_kind(kind)
    if kind in RESOURCE_MAP:
        group, version, _ = RESOURCE_MAP[kind]
        api_ver = f"{group}/{version}" if group else version
        try:
            return dyn.resources.get(api_version=api_ver, kind=kind)
        except Exception:
            raise HTTPException(status_code=400, detail=f"无法定位资源类型: {kind}")
    try:
        return dyn.resources.get(kind=kind)
    except Exception:
        raise HTTPException(status_code=400, detail=f"不支持的资源类型: {kind}")

def _read_dynamic_resource(dyn, namespace: str, kind: str, name: str) -> tuple[Any, Any, dict]:
    """读取单个 K8s 资源对象，返回 resource、对象实例和 dict 表示。"""
    resource = _resolve_dynamic_resource(dyn, kind)
    kwargs = {"name": name}
    if namespace and namespace != "_all":
        kwargs["namespace"] = namespace
    obj = resource.get(**kwargs)
    return resource, obj, obj.to_dict()

def _safe_text_map(raw: Any, *, max_items: int = 80) -> dict[str, str]:
    """把 labels/annotations 规范为安全的字符串字典，限制大小避免滥用。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="labels/annotations 必须是键值对象")
    result: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text:
            continue
        if len(key_text) > 253 or len(value_text) > 2048:
            raise HTTPException(status_code=400, detail="labels/annotations 单项长度超限")
        result[key_text] = value_text
        if len(result) > max_items:
            raise HTTPException(status_code=400, detail=f"labels/annotations 最多允许 {max_items} 项")
    return result

def _resource_edit_payload(kind: str, obj: dict, namespace: str, name: str) -> dict:
    """把 K8s 原始对象转换为前端表单可编辑结构。

    只返回低风险字段；Secret 不返回 data/stringData，避免敏感信息进入浏览器，
    并以 secret_redacted 标记提示前端内容已被脱敏。
    """
    metadata = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec") or {})
    containers = template_spec.get("containers") or []
    is_secret = _normalize_kind(kind) == "Secret"
    service_ports = []
    for port in spec.get("ports") or []:
        target_port = port.get("targetPort")
        service_ports.append({
            "name": port.get("name") or "",
            "port": port.get("port") or 80,
            "target_port": str(target_port) if target_port is not None else "",
            "protocol": port.get("protocol") or "TCP",
            "node_port": port.get("nodePort") or "",
        })
    payload = {
        "kind": kind,
        "name": name,
        "namespace": namespace if namespace != "_all" else "",
        "editable": True,
        "labels": metadata.get("labels") or {},
        "annotations": metadata.get("annotations") or {},
        "fields": {
            "metadata": True,
            "replicas": kind in ("Deployment", "StatefulSet"),
            "containers": kind in ("Deployment", "StatefulSet", "DaemonSet"),
            "service": kind == "Service",
            # Secret 的 data 一律不回传（写入走表单提交，读取被脱敏）。
            "data": kind == "ConfigMap",
        },
        "replicas": spec.get("replicas") if kind in ("Deployment", "StatefulSet") else None,
        "containers": [
            {
                "name": item.get("name") or "",
                "image": item.get("image") or "",
                "ports": item.get("ports") or [],
                "env": item.get("env") or [],
            }
            for item in containers
            if item.get("name")
        ],
        "service": {
            "type": spec.get("type") or "ClusterIP",
            "ports": service_ports,
        } if kind == "Service" else None,
        "data": (obj.get("data") or {}) if kind == "ConfigMap" else None,
    }
    if is_secret:
        payload["secret_redacted"] = True
    return payload

def _patch_workload_form(
    api_client: client.ApiClient,
    kind: str,
    namespace: str,
    name: str,
    payload: ResourceFormPatchRequest,
    raw_obj: dict,
) -> dict:
    """使用战略合并补丁更新工作负载，避免替换完整 containers 数组。"""
    apps_v1 = client.AppsV1Api(api_client=api_client)
    body: dict[str, Any] = {"metadata": {}, "spec": {}}
    if payload.labels is not None:
        body["metadata"]["labels"] = _safe_text_map(payload.labels)
    if payload.annotations is not None:
        body["metadata"]["annotations"] = _safe_text_map(payload.annotations)
    if payload.replicas is not None and kind in ("Deployment", "StatefulSet"):
        body["spec"]["replicas"] = payload.replicas
    if payload.containers is not None:
        existing_containers = (((raw_obj.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        allowed_names = {str(item.get("name") or "") for item in existing_containers}
        for item in payload.containers:
            if item.name.strip() not in allowed_names:
                raise HTTPException(status_code=400, detail=f"不允许通过表单新增容器: {item.name}")
            if not item.image.strip():
                raise HTTPException(status_code=400, detail=f"容器 {item.name} 的镜像不能为空")
            if len(item.image.strip()) > 512:
                raise HTTPException(status_code=400, detail="镜像地址长度超限")
        body_containers = []
        for item in payload.containers:
            c_body = {"name": item.name.strip(), "image": item.image.strip()}
            if item.ports is not None:
                c_body["ports"] = [
                    {
                        "containerPort": int(p.get("containerPort") or p.get("container_port") or p.get("port") or 80),
                        "name": p.get("name") or None,
                        "protocol": p.get("protocol") or "TCP"
                    }
                    for p in item.ports
                    if p.get("containerPort") or p.get("container_port") or p.get("port")
                ]
            if item.env is not None:
                c_body["env"] = [
                    {"name": e.get("name").strip(), "value": str(e.get("value") or "")}
                    for e in item.env
                    if e.get("name") and e.get("name").strip()
                ]
            body_containers.append(c_body)
        body["spec"].setdefault("template", {}).setdefault("spec", {})["containers"] = body_containers
    if not body["metadata"]:
        body.pop("metadata")
    if not body["spec"]:
        body.pop("spec")
    if not body:
        raise HTTPException(status_code=400, detail="没有可提交的变更")
    kwargs = {
        "name": name,
        "namespace": namespace,
        "body": body,
        "_content_type": "application/strategic-merge-patch+json",
        "_request_timeout": 8,
    }
    if kind == "Deployment":
        apps_v1.patch_namespaced_deployment(**kwargs)
    elif kind == "StatefulSet":
        apps_v1.patch_namespaced_stateful_set(**kwargs)
    elif kind == "DaemonSet":
        apps_v1.patch_namespaced_daemon_set(**kwargs)
    else:
        raise HTTPException(status_code=400, detail=f"{kind} 暂不支持表单编辑")
    return {"success": True, "message": f"{kind} {name} 已更新"}

def _patch_service_form(api_client: client.ApiClient, namespace: str, name: str, payload: ResourceFormPatchRequest) -> dict:
    """更新 Service 的基础属性。端口数组会按表单内容整体提交。"""
    body: dict[str, Any] = {"metadata": {}, "spec": {}}
    if payload.labels is not None:
        body["metadata"]["labels"] = _safe_text_map(payload.labels)
    if payload.annotations is not None:
        body["metadata"]["annotations"] = _safe_text_map(payload.annotations)
    if payload.service_type:
        if payload.service_type not in ("ClusterIP", "NodePort", "LoadBalancer", "ExternalName"):
            raise HTTPException(status_code=400, detail="Service 类型不合法")
        body["spec"]["type"] = payload.service_type
    if payload.ports is not None:
        body["spec"]["ports"] = []
        for item in payload.ports:
            port_item = {
                "name": item.name.strip() or None,
                "port": item.port,
                "targetPort": item.target_port.strip() or item.port,
                "protocol": item.protocol if item.protocol in ("TCP", "UDP", "SCTP") else "TCP",
            }
            if port_item["name"] is None:
                port_item.pop("name")
            if payload.service_type == "NodePort" and item.node_port:
                port_item["nodePort"] = item.node_port
            body["spec"]["ports"].append(port_item)
    if not body["metadata"]:
        body.pop("metadata")
    if not body["spec"]:
        body.pop("spec")
    if not body:
        raise HTTPException(status_code=400, detail="没有可提交的变更")
    client.CoreV1Api(api_client=api_client).patch_namespaced_service(
        name=name,
        namespace=namespace,
        body=body,
        _request_timeout=8,
    )
    return {"success": True, "message": f"Service {name} 已更新"}

def _patch_metadata_form(resource: Any, namespace: str, name: str, payload: ResourceFormPatchRequest) -> dict:
    """通用资源只允许表单编辑 metadata，复杂 spec 必须走 YAML 高级模式。"""
    body: dict[str, Any] = {"metadata": {}}
    if payload.labels is not None:
        body["metadata"]["labels"] = _safe_text_map(payload.labels)
    if payload.annotations is not None:
        body["metadata"]["annotations"] = _safe_text_map(payload.annotations)
    if not body["metadata"]:
        raise HTTPException(status_code=400, detail="没有可提交的 metadata 变更")
    kwargs = {"name": name, "body": body, "content_type": "application/merge-patch+json"}
    if namespace and namespace != "_all" and getattr(resource, "namespaced", False):
        kwargs["namespace"] = namespace
    resource.patch(**kwargs)
    return {"success": True, "message": f"{name} 元数据已更新"}

def _patch_config_form(resource: Any, namespace: str, name: str, payload: ResourceFormPatchRequest) -> dict:
    """ConfigMap 和 Secret 专用的表单更新接口，支持 data/stringData"""
    body: dict[str, Any] = {"metadata": {}}
    if payload.labels is not None:
        body["metadata"]["labels"] = _safe_text_map(payload.labels)
    if payload.annotations is not None:
        body["metadata"]["annotations"] = _safe_text_map(payload.annotations)
    if not body["metadata"]:
        body.pop("metadata")
        
    if payload.data is not None:
        body["data"] = payload.data
    if payload.stringData is not None:
        body["stringData"] = payload.stringData
        
    if not body:
        raise HTTPException(status_code=400, detail="没有可提交的变更")
        
    kwargs = {"name": name, "body": body, "content_type": "application/merge-patch+json"}
    if namespace and namespace != "_all" and getattr(resource, "namespaced", False):
        kwargs["namespace"] = namespace
    resource.patch(**kwargs)
    return {"success": True, "message": f"{name} 配置已更新"}

def _open_pod_exec_stream(
    *,
    api_client: client.ApiClient,
    namespace: str,
    name: str,
    command: str,
    container: str,
    timeout_seconds: int,
):
    """打开 Pod exec 交互流。

    Kubernetes Python 客户端要求通过 `CoreV1Api.connect_get_namespaced_pod_exec`
    建立 exec 通道；底层 ApiClient 只负责 HTTP 连接，不能直接执行该方法。
    `_preload_content=False` 会返回可读写的 WSClient，供后续 stdin/stdout 转发。
    """
    core_v1 = client.CoreV1Api(api_client=api_client)
    exec_cmd = [command or "/bin/sh", "-i"]
    return k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        name=name,
        namespace=namespace,
        command=exec_cmd,
        container=container or "",
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
        _request_timeout=timeout_seconds,
    )

def _preserve_secret_data_before_apply(dyn, doc: dict) -> str | None:
    """Secret 提交未携带 data/stringData 时，先读现有对象并保留其原密文。

    背景：server-side apply 会移除"同字段管理方未再声明"的字段——用户在 YAML
    工作台里看到的是脱敏后的 Secret（无 data），直接 Apply 会把线上密钥抹成空。
    这里在 apply/dry-run 之前把现有 data/stringData 合并回提交文档。

    Returns:
        None 表示无需保留或已成功合并；返回错误文案时调用方必须阻止本次提交
        （读取现有对象失败时宁可拒绝，也不能静默把密钥抹掉）。
    """
    kind = _normalize_kind(str(doc.get("kind") or ""))
    if kind != "Secret":
        return None
    if "data" in doc or "stringData" in doc:
        # 提交自带内容：按用户提交执行。
        return None
    metadata = doc.get("metadata") or {}
    name = str(metadata.get("name") or "").strip()
    if not name:
        return None
    try:
        resource = _resolve_resource(dyn, doc)
        kwargs = {"name": name}
        namespace = metadata.get("namespace")
        if namespace:
            kwargs["namespace"] = namespace
        existing = resource.get(**kwargs).to_dict()
    except ApiException as e:
        if e.status == 404:
            return None  # 新建 Secret：不存在历史密文，无需保留
        return f"读取现有 Secret 失败（HTTP {e.status}），为防止密钥被空白覆盖，已阻止本次提交"
    except Exception as e:  # noqa: BLE001
        return f"读取现有 Secret 失败（{str(e)[:120]}），为防止密钥被空白覆盖，已阻止本次提交"
    if existing.get("data"):
        doc["data"] = existing["data"]
    if existing.get("stringData"):
        doc["stringData"] = existing["stringData"]
    return None

def _apply_single_doc(dyn, doc: dict, dry_run: bool = False) -> dict:
    """对单个文档执行 server-side apply。"""
    resource = _resolve_resource(dyn, doc)
    name = doc["metadata"]["name"]
    namespace = doc["metadata"].get("namespace")
    kind = doc["kind"]
    # Secret 内容边界：脱敏后的 YAML 回写不带 data，先从集群保留原密文再提交。
    preserve_error = _preserve_secret_data_before_apply(dyn, doc)
    if preserve_error:
        return {
            "kind": kind,
            "name": name,
            "namespace": namespace or "(cluster-scoped)",
            "status": "error",
            "error": preserve_error,
        }
    body = {
        "apiVersion": doc["apiVersion"],
        "kind": kind,
        "metadata": doc["metadata"],
        "spec": doc.get("spec"),
        "data": doc.get("data"),
        "stringData": doc.get("stringData"),
        "rules": doc.get("rules"),
        "subjects": doc.get("subjects"),
        "roleRef": doc.get("roleRef"),
    }
    body = {k: v for k, v in body.items() if v is not None}
    try:
        kwargs = {"body": body}
        if dry_run:
            kwargs["dry_run"] = "All"
        if namespace:
            kwargs["namespace"] = namespace
        resource.server_side_apply(**kwargs)
        return {"kind": kind, "name": name, "namespace": namespace or "(cluster-scoped)", "status": "valid"}
    except ApiException as e:
        error_msg = e.reason or str(e)
        if e.body:
            try:
                error_body = yaml.safe_load(e.body)
                error_msg = error_body.get("message", error_msg)
            except Exception:
                pass
        return {"kind": kind, "name": name, "namespace": namespace or "(cluster-scoped)", "status": "error", "error": error_msg}
    except Exception as e:
        return {"kind": kind, "name": name, "namespace": namespace or "(cluster-scoped)", "status": "error", "error": str(e)}

def _raise_k8s_http_exception(exc: ApiException, action: str) -> HTTPException:
    """把 Kubernetes ApiException 分类成稳定、可诊断的 HTTPException。"""
    status_code = int(getattr(exc, "status", 0) or 502)
    reason = str(getattr(exc, "reason", "") or "").strip()
    message_map = {
        401: "K8s 集群认证失败，请检查 kubeconfig 凭据是否过期",
        403: "K8s 集群权限不足，请检查 kubeconfig 对目标资源的 RBAC 授权",
        404: "K8s 资源不存在或当前 API 版本不可用",
        408: "K8s API 请求超时，请检查 API Server 或网络状态",
        429: "K8s API Server 限流，请降低查询频率或稍后重试",
    }
    if status_code >= 500:
        detail = "K8s API Server 返回服务端错误"
    else:
        detail = message_map.get(status_code, "K8s API 请求失败")
    if reason:
        detail = f"{detail}: {reason}"
    return HTTPException(status_code=status_code if status_code >= 400 else 502, detail=f"{action}失败，{detail}")

def _model_to_dict(item: Any) -> dict[str, Any]:
    """把 typed client 模型转为 dict，兼容测试替身和 Kubernetes 原生模型。"""
    if isinstance(item, dict):
        return item
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return {}

def _extract_workload_image(kind: str, item: dict) -> str:
    """从工作负载或 Pod 的 spec 中提取镜像名称，用逗号连接返回。"""
    spec = item.get("spec") or {}
    containers = []
    if kind == "Pod":
        containers = spec.get("containers") or []
    elif kind == "CronJob":
        job_template = spec.get("jobTemplate") or {}
        job_spec = job_template.get("spec") or {}
        template = job_spec.get("template") or {}
        containers = (template.get("spec") or {}).get("containers") or []
    elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        template = spec.get("template") or {}
        containers = (template.get("spec") or {}).get("containers") or []
    
    images = [c.get("image") for c in containers if c.get("image")]
    return ", ".join(images) if images else "-"

def _extract_workload_ready(kind: str, item: dict) -> str | None:
    """从工作负载的 spec 和 status 中提取副本就绪状态 (格式例如 '3/3' 或 '1/1')"""
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        ready_reps = status.get("readyReplicas")
        if ready_reps is None:
            ready_reps = status.get("ready_replicas") or 0
        req_reps = spec.get("replicas")
        if req_reps is None:
            req_reps = 1
        return f"{ready_reps}/{req_reps}"
    elif kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled")
        if desired is None:
            desired = status.get("desired_number_scheduled") or 0
        ready = status.get("numberReady")
        if ready is None:
            ready = status.get("number_ready") or 0
        return f"{ready}/{desired}"
    elif kind == "Pod":
        container_statuses = status.get("containerStatuses") or status.get("container_statuses") or []
        if container_statuses:
            ready_count = sum(1 for c in container_statuses if c.get("ready"))
            total_count = len(container_statuses)
            return f"{ready_count}/{total_count}"
        return "0/0"
    elif kind == "Job":
        succeeded = status.get("succeeded") or 0
        completions = spec.get("completions")
        if completions is None:
            completions = 1
        return f"{succeeded}/{completions}"
    return None

def _labels_to_selector(labels: dict[str, Any]) -> str:
    """把 K8s selector labels 转为 labelSelector 字符串。

    这里只处理等值选择器，覆盖 Service selector 与 workload matchLabels
    的最高频场景；复杂 matchExpressions 后续再单独建模，避免误匹配。
    """
    if not labels:
        return ""
    return ",".join(f"{key}={value}" for key, value in labels.items() if key and value is not None)

def _format_age(creation_timestamp) -> str:
    """Convert K8s creation_timestamp to human-readable relative time."""
    if not creation_timestamp:
        return "Unknown"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    ct = creation_timestamp
    if isinstance(ct, str):
        ct = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc)
    delta = now - ct
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"

def _empty_diagnosis() -> dict[str, str]:
    """返回统一的无异常诊断结构，方便前端按字段渲染。"""
    return {
        "level": "ok",
        "reason": "",
        "detail": "",
        "suggestion": "",
    }

def _diagnose_pod(pod: Any) -> dict[str, str]:
    """基于 Pod 状态生成可读诊断。

    该函数只使用 Kubernetes status 摘要，不读取 env、volume、secret 等敏感字段。
    """
    metadata = getattr(pod, "metadata", None)
    status = getattr(pod, "status", None)
    if getattr(metadata, "deletion_timestamp", None):
        return {
            "level": "warning",
            "reason": "正在终止",
            "detail": "Pod 已进入删除流程，如果长时间停留可能存在 finalizer 或节点不可达问题。",
            "suggestion": "先查看事件与节点状态；确认无业务影响后再考虑强制删除。",
        }

    phase = getattr(status, "phase", "") or "Unknown"
    reason = getattr(status, "reason", "") or ""
    container_statuses = getattr(status, "container_statuses", None) or []
    for container_status in container_statuses:
        state = getattr(container_status, "state", None)
        waiting = getattr(state, "waiting", None) if state else None
        terminated = getattr(state, "terminated", None) if state else None
        restart_count = int(getattr(container_status, "restart_count", 0) or 0)
        if waiting:
            wait_reason = getattr(waiting, "reason", "") or "Waiting"
            message = getattr(waiting, "message", "") or ""
            if wait_reason in ("ImagePullBackOff", "ErrImagePull"):
                return {
                    "level": "warning",
                    "reason": "镜像拉取失败",
                    "detail": message or wait_reason,
                    "suggestion": "检查镜像地址、镜像仓库凭证、网络连通性和 imagePullSecret。",
                }
            if wait_reason == "CrashLoopBackOff":
                return {
                    "level": "critical",
                    "reason": "容器反复崩溃",
                    "detail": message or f"容器已重启 {restart_count} 次",
                    "suggestion": "优先查看容器日志、启动命令、配置文件和探针配置。",
                }
            if wait_reason in ("CreateContainerConfigError", "CreateContainerError"):
                return {
                    "level": "critical",
                    "reason": "容器创建失败",
                    "detail": message or wait_reason,
                    "suggestion": "检查 ConfigMap、Secret、挂载路径、启动参数与运行时权限。",
                }
        if terminated:
            term_reason = getattr(terminated, "reason", "") or "Terminated"
            message = getattr(terminated, "message", "") or ""
            if term_reason == "OOMKilled":
                return {
                    "level": "critical",
                    "reason": "内存超限退出",
                    "detail": message or "容器被 OOMKilled",
                    "suggestion": "检查内存 limit/request 与应用内存使用，必要时扩容或优化内存。",
                }

    if phase == "Pending":
        return {
            "level": "warning",
            "reason": "调度等待",
            "detail": reason or "Pod 仍处于 Pending",
            "suggestion": "查看事件，重点检查资源不足、节点选择器、污点容忍和 PVC 绑定。",
        }
    if phase == "Failed":
        return {
            "level": "critical",
            "reason": "Pod 已失败",
            "detail": reason or "Pod phase=Failed",
            "suggestion": "查看事件和上一次容器日志，确认退出码与失败原因。",
        }
    return _empty_diagnosis()

def _diagnose_workload(
    kind: str,
    workload: Any,
    *,
    ready_replicas: int,
    desired_replicas: int,
) -> dict[str, str]:
    """基于工作负载副本状态生成诊断建议。"""
    if desired_replicas == 0:
        return {
            "level": "info",
            "reason": "副本数为 0",
            "detail": f"{kind} 当前期望副本为 0。",
            "suggestion": "如果不是预期停服，请执行伸缩或检查发布配置。",
        }
    if ready_replicas < desired_replicas:
        status = getattr(workload, "status", None)
        updated = int(getattr(status, "updated_replicas", 0) or 0)
        available = int(getattr(status, "available_replicas", 0) or 0)
        return {
            "level": "warning" if ready_replicas > 0 else "critical",
            "reason": "副本未就绪",
            "detail": f"Ready {ready_replicas}/{desired_replicas}，Updated {updated}，Available {available}。",
            "suggestion": "查看关联 Pod 与 Events，优先排查镜像、探针、资源不足和调度失败。",
        }
    return _empty_diagnosis()

def _diagnose_node(node: Any) -> dict[str, str]:
    """基于 Node conditions 生成诊断建议。"""
    spec = getattr(node, "spec", None)
    status = getattr(node, "status", None)
    conditions = getattr(status, "conditions", None) or []
    pressure_reasons: list[str] = []
    ready_condition = None
    for condition in conditions:
        condition_type = getattr(condition, "type", "")
        condition_status = getattr(condition, "status", "")
        if condition_type == "Ready":
            ready_condition = condition
        if condition_status == "True" and condition_type in ("DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"):
            pressure_reasons.append(condition_type)

    if ready_condition and getattr(ready_condition, "status", "") != "True":
        detail_parts = []
        reason = getattr(ready_condition, "reason", "") or "Ready=False"
        message = getattr(ready_condition, "message", "") or ""
        detail_parts.append(f"{reason}: {message}".strip(": "))
        if pressure_reasons:
            pressure_text = "、".join({
                "DiskPressure": "磁盘压力",
                "MemoryPressure": "内存压力",
                "PIDPressure": "PID 压力",
                "NetworkUnavailable": "网络不可用",
            }.get(item, item) for item in pressure_reasons)
            detail_parts.append(pressure_text)
        return {
            "level": "critical",
            "reason": "节点不可用",
            "detail": "；".join(part for part in detail_parts if part),
            "suggestion": "检查 kubelet、容器运行时、节点网络和磁盘/内存水位。",
        }

    if pressure_reasons:
        pressure_text = "、".join({
            "DiskPressure": "磁盘压力",
            "MemoryPressure": "内存压力",
            "PIDPressure": "PID 压力",
            "NetworkUnavailable": "网络不可用",
        }.get(item, item) for item in pressure_reasons)
        return {
            "level": "warning",
            "reason": "节点存在压力",
            "detail": pressure_text,
            "suggestion": "检查节点资源水位、Pod 驱逐事件和磁盘清理策略。",
        }

    if getattr(spec, "unschedulable", False):
        return {
            "level": "info",
            "reason": "不可调度",
            "detail": "节点已被 cordon，新 Pod 不会调度到该节点。",
            "suggestion": "如果维护已完成，可解除不可调度；否则保持观察现有 Pod 状态。",
        }
    return _empty_diagnosis()

def _pod_status(pod) -> str:
    """Extract pod phase status, append reason if Failed."""
    phase = pod.status.phase or "Unknown"
    if phase == "Failed" and pod.status.reason:
        return f"Failed({pod.status.reason})"
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or "Waiting"
                return reason
    return phase

def _pod_ready(pod) -> str:
    """Calculate pod ready container ratio like '1/2'."""
    if not pod.status.container_statuses:
        return "0/0"
    ready = sum(1 for cs in pod.status.container_statuses if cs.ready)
    total = len(pod.status.container_statuses)
    return f"{ready}/{total}"

def _pod_restarts(pod) -> int:
    """Sum restart count across all containers in the pod."""
    if not pod.status.container_statuses:
        return 0
    return sum(cs.restart_count or 0 for cs in pod.status.container_statuses)

