"""Namespace CRUD, labels, and usage routes."""

from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query
from kubernetes import client
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import require_permissions, require_risk_ack
from app.db.database import get_db

from ._router import router
from ._schemas import CreateNamespaceRequest, NamespaceLabelsPatch
from ._shared import (
    _format_age,
    _parse_cpu_quantity,
    _parse_memory_quantity,
    _raise_k8s_http_exception,
)
from . import _shared

@router.get("/{cluster_id}/namespaces")
def list_namespaces(cluster_id: str):
    """List all namespaces with status and age."""
    api_client = _shared._get_api_client(cluster_id)

    def _load_namespaces() -> list[dict[str, Any]]:
        v1 = client.CoreV1Api(api_client=api_client)
        nss = v1.list_namespace(_request_timeout=6).items
        return [
            {
                "name": ns.metadata.name,
                "status": ns.status.phase,
                "age": _format_age(ns.metadata.creation_timestamp),
            }
            for ns in sorted(nss, key=lambda x: x.metadata.name)
        ]

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "namespaces",
            ("_all",),
            _load_namespaces,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"K8s API namespace query failed: {str(e)}")

_QUOTA_PRESETS: dict[str, dict[str, str]] = {
    "small": {"requests.cpu": "2", "requests.memory": "4Gi", "pods": "20"},
    "medium": {"requests.cpu": "8", "requests.memory": "16Gi", "pods": "50"},
    "large": {"requests.cpu": "32", "requests.memory": "64Gi", "pods": "200"},
}

_LIMIT_RANGE_DEFAULTS: dict[str, dict[str, str]] = {
    "small": {"cpu": "200m", "memory": "256Mi"},
    "medium": {"cpu": "500m", "memory": "512Mi"},
    "large": {"cpu": "1", "memory": "1Gi"},
}

@router.post(
    "/{cluster_id}/namespaces",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "create_namespace")),
    ],
)
def create_namespace(cluster_id: str, payload: CreateNamespaceRequest):
    """创建 Namespace，可选附带 ResourceQuota 和 LimitRange。"""
    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)

    # 1. 创建 Namespace
    ns_body = client.V1Namespace(
        metadata=client.V1ObjectMeta(name=payload.name, labels=payload.labels or None),
    )
    try:
        v1.create_namespace(body=ns_body, _request_timeout=6)
    except ApiException as exc:
        if exc.status == 409:
            raise HTTPException(status_code=409, detail=f"Namespace {payload.name} 已存在") from exc
        raise _raise_k8s_http_exception(exc, "创建 Namespace")

    # 2. 创建 ResourceQuota（如果有预设）
    quota_hard: dict[str, str] | None = None
    if payload.quota_preset == "custom" and payload.custom_quota:
        quota_hard = payload.custom_quota
    elif payload.quota_preset in _QUOTA_PRESETS:
        quota_hard = _QUOTA_PRESETS[payload.quota_preset]

    if quota_hard:
        quota_body = {
            "apiVersion": "v1", "kind": "ResourceQuota",
            "metadata": {"name": f"{payload.name}-quota", "namespace": payload.name},
            "spec": {"hard": quota_hard},
        }
        try:
            v1.create_namespaced_resource_quota(namespace=payload.name, body=quota_body, _request_timeout=6)
        except Exception:
            pass  # 降级：Namespace 已创建，Quota 失败不回滚

    # 3. 创建 LimitRange（如果有预设）
    if payload.quota_preset in _LIMIT_RANGE_DEFAULTS:
        lr_defaults = _LIMIT_RANGE_DEFAULTS[payload.quota_preset]
        # default limit = 2x defaultRequest
        lr_limits = {"cpu": {"small": "400m", "medium": "1", "large": "2"}[payload.quota_preset],
                     "memory": {"small": "512Mi", "medium": "1Gi", "large": "2Gi"}[payload.quota_preset]}
        lr_body = {
            "apiVersion": "v1", "kind": "LimitRange",
            "metadata": {"name": f"{payload.name}-limits", "namespace": payload.name},
            "spec": {"limits": [{"type": "Container", "defaultRequest": lr_defaults, "default": lr_limits}]},
        }
        try:
            v1.create_namespaced_limit_range(namespace=payload.name, body=lr_body, _request_timeout=6)
        except Exception:
            pass

    _shared._clear_k8s_read_cache(cluster_id)
    return {"success": True, "message": f"Namespace {payload.name} 创建成功", "name": payload.name}

PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}

def _check_namespace_deletion_allowed(name: str) -> str | None:
    """校验命名空间是否允许删除，返回拒绝原因；None 表示允许。

    保护名单独立成纯函数，便于单测覆盖（删除路由本身依赖真实集群连接）。
    """
    if name in PROTECTED_NAMESPACES:
        return f"命名空间 {name} 是集群核心命名空间，禁止删除"
    return None

@router.delete(
    "/{cluster_id}/namespaces/{ns}",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "delete")),
    ],
)
def delete_namespace(cluster_id: str, ns: str, db: Session = Depends(get_db)):
    """删除命名空间（K8s 级联删除其下全部资源，异步进入 Terminating）。

    安全边界：
    1. 保护名单（kube-system/default 等）直接拒绝；
    2. 集群级管理操作，落审计日志由装饰路由的 require_risk_ack 网关记录。
    """
    denied = _check_namespace_deletion_allowed(ns)
    if denied:
        raise HTTPException(status_code=403, detail=denied)

    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)
    try:
        v1.delete_namespace(name=ns, _request_timeout=10)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"命名空间 {ns} 不存在")
        raise HTTPException(status_code=502, detail=f"删除失败: {exc.reason or str(exc)}")
    return {
        "message": f"命名空间 {ns} 已进入 Terminating，其下资源将被级联删除",
        "namespace": ns,
    }

@router.patch(
    "/{cluster_id}/namespaces/{ns}/labels",
    response_model=None,
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "patch_labels")),
    ],
)
def patch_namespace_labels(cluster_id: str, ns: str, payload: NamespaceLabelsPatch):
    """增量更新命名空间标签（合并语义；kubernetes.io 前缀受保护）。"""
    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)
    try:
        ns_obj = v1.read_namespace(name=ns, _request_timeout=8)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"命名空间 {ns} 不存在")
        raise HTTPException(status_code=502, detail=f"读取失败: {exc.reason or str(exc)}")

    existing = dict(ns_obj.metadata.labels or {})
    applied: dict[str, str] = {}
    for key, value in payload.labels.items():
        # 保护系统前缀：与节点标签同一套约定，避免改坏调度/网络插件依赖。
        if key.startswith(("kubernetes.io/", "k8s.io/")):
            continue
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value
            applied[key] = value
    ns_obj.metadata.labels = existing
    v1.patch_namespace(name=ns, body=ns_obj, _request_timeout=8)
    return {"message": "标签已更新", "applied": applied}

@router.get("/{cluster_id}/namespaces/{ns}/usage")
def get_namespace_usage(cluster_id: str, ns: str):
    """获取命名空间资源水位：Pod 数、CPU/Mem requests 汇总、Quota 状态、Top 10 Pod。"""
    api_client = _shared._get_api_client(cluster_id)

    def _load():
        v1 = client.CoreV1Api(api_client=api_client)
        pods = v1.list_namespaced_pod(namespace=ns, _request_timeout=8).items

        cpu_total = 0.0
        mem_total = 0.0
        pod_usage: list[dict] = []

        for pod in pods:
            pod_cpu = 0.0
            pod_mem = 0.0
            if pod.spec and pod.spec.containers:
                for c in pod.spec.containers:
                    req = (c.resources.requests or {}) if c.resources else {}
                    pod_cpu += _parse_cpu_quantity(req.get("cpu", "0"))
                    pod_mem += _parse_memory_quantity(req.get("memory", "0"))
            cpu_total += pod_cpu
            mem_total += pod_mem
            pod_usage.append({
                "name": pod.metadata.name,
                "status": pod.status.phase if pod.status else "Unknown",
                "cpu_request": round(pod_cpu / 1000, 3),
                "mem_request": round(pod_mem / (1024**3), 3),
            })

        # ResourceQuota
        quota_info = None
        try:
            quotas = v1.list_namespaced_resource_quota(namespace=ns, _request_timeout=5).items
            if quotas:
                q = quotas[0]
                quota_info = {
                    "name": q.metadata.name,
                    "hard": dict(q.status.hard) if q.status and q.status.hard else {},
                    "used": dict(q.status.used) if q.status and q.status.used else {},
                }
        except Exception:
            pass

        # Top 10 by CPU
        top_pods = sorted(pod_usage, key=lambda x: x["cpu_request"], reverse=True)[:10]

        return {
            "pod_count": len(pods),
            "cpu_requests_total": round(cpu_total / 1000, 3),
            "mem_requests_total_gi": round(mem_total / (1024**3), 3),
            "quota": quota_info,
            "top_pods": top_pods,
        }

    try:
        return _shared._get_or_set_k8s_read_cache(cluster_id, f"ns_usage_{ns}", (ns,), _load, ttl_seconds=5)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取命名空间用量失败: {e}")

