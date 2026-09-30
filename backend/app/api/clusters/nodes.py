"""Node listing, metrics, labels/taints, and drain routes."""

import os
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query
from kubernetes import client
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from app.core.security import require_permissions, require_risk_ack

from ._router import router
from ._schemas import (
    NodeDrainRequest,
    PatchNodeLabelsRequest,
    PatchNodeTaintsRequest,
)
from ._shared import (
    parse_k8s_cpu,
    parse_k8s_memory_to_gib,
    _diagnose_node,
    _metrics_not_available,
    _parse_cpu_quantity,
    _parse_memory_quantity,
    _raise_k8s_http_exception,
    _sum_container_metrics,
)
from . import _shared

@router.get("/{cluster_id}/nodes")
def get_cluster_nodes(cluster_id: str):
    """[实时拉取] 返回集群的物理计算节点拓扑层级"""
    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        raise HTTPException(status_code=404, detail="该集群未找到真实 Kubeconfig，请重新纳管后再查看节点")

    def _load_nodes() -> list[dict[str, Any]]:
        api_client = _shared._get_api_client(cluster_id)
        v1 = client.CoreV1Api(api_client=api_client)
        nodes = v1.list_node(_request_timeout=6).items

        result = []
        for n in nodes:
            name = n.metadata.name

            # 探测网络 IP
            ip = "Unknown"
            for addr in n.status.addresses:
                if addr.type == "InternalIP":
                    ip = addr.address

            # 判定集群角色
            role = "Worker"
            for label in (n.metadata.labels or {}):
                if "control-plane" in label or "master" in label:
                    role = "ControlPlane"

            # 过滤探针状态
            status = "Unknown"
            for c in n.status.conditions:
                if c.type == "Ready":
                    status = "Ready" if c.status == "True" else "NotReady"

            capacity = getattr(n.status, "capacity", {}) or {}
            cpu_cap = parse_k8s_cpu(capacity.get("cpu", ""))
            mem_cap = parse_k8s_memory_to_gib(capacity.get("memory", ""))

            result.append({
                "name": name,
                "ip": ip,
                "role": role,
                "status": status,
                "unschedulable": bool(getattr(getattr(n, "spec", None), "unschedulable", False)),
                "diagnosis": _diagnose_node(n),
                "cpu_usage": 0, # Metrics 端点缺失退化预备
                "mem_usage": 0,
                "age": str(n.metadata.creation_timestamp)[:10],
                "cpu_capacity": cpu_cap,
                "mem_capacity": mem_cap,
            })
        return result

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "nodes",
            ("_all",),
            _load_nodes,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"K8s API 节点查询失败: {str(e)}")

@router.get("/{cluster_id}/nodes/metrics")
def get_cluster_node_metrics(cluster_id: str):
    """返回集群节点的实时 CPU / 内存用量。

    依赖 `metrics.k8s.io/v1beta1`（metrics-server）。未安装或暂不可用时，
    best-effort 降级为 `{available: false}`，不返回 5xx，避免总览页因为 metrics 问题整体不可用。
    """
    api_client = _shared._get_api_client(cluster_id)

    def _load_metrics() -> dict[str, Any]:
        custom = client.CustomObjectsApi(api_client=api_client)
        try:
            resp = custom.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
                _request_timeout=6,
            )
        except ApiException as exc:
            if exc.status in (404, 503):
                return _metrics_not_available(f"metrics.k8s.io 不可达: HTTP {exc.status}")
            return _metrics_not_available(f"metrics.k8s.io 查询失败: {exc.reason}")
        except Exception as exc:  # noqa: BLE001
            return _metrics_not_available(f"metrics.k8s.io 查询异常: {exc}")

        items = resp.get("items") if isinstance(resp, dict) else []
        rows: list[dict[str, Any]] = []
        for item in items or []:
            metadata = item.get("metadata") or {}
            usage = item.get("usage") or {}
            rows.append({
                "name": metadata.get("name") or "",
                "cpu_millicores": _parse_cpu_quantity(usage.get("cpu")),
                "memory_bytes": _parse_memory_quantity(usage.get("memory")),
                "timestamp": item.get("timestamp") or "",
                "window": item.get("window") or "",
            })
        return {"available": True, "items": rows}

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "node-metrics",
            ("_all",),
            _load_metrics,
            ttl_seconds=15,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return _metrics_not_available(str(exc))

@router.get("/{cluster_id}/pods/metrics")
def get_cluster_pod_metrics(cluster_id: str, namespace: str = Query("_all", description="namespace, _all for all")):
    """返回 Pod 的实时 CPU / 内存用量，按容器聚合到 Pod 级。"""
    api_client = _shared._get_api_client(cluster_id)
    ns = (namespace or "_all").strip() or "_all"

    def _load_metrics() -> dict[str, Any]:
        custom = client.CustomObjectsApi(api_client=api_client)
        try:
            if ns == "_all":
                resp = custom.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="pods",
                    _request_timeout=8,
                )
            else:
                resp = custom.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="pods",
                    _request_timeout=8,
                )
        except ApiException as exc:
            if exc.status in (404, 503):
                return _metrics_not_available(f"metrics.k8s.io 不可达: HTTP {exc.status}")
            return _metrics_not_available(f"metrics.k8s.io 查询失败: {exc.reason}")
        except Exception as exc:  # noqa: BLE001
            return _metrics_not_available(f"metrics.k8s.io 查询异常: {exc}")

        items = resp.get("items") if isinstance(resp, dict) else []
        rows: list[dict[str, Any]] = []
        for item in items or []:
            metadata = item.get("metadata") or {}
            containers = item.get("containers") or []
            cpu, memory = _sum_container_metrics(containers)
            rows.append({
                "name": metadata.get("name") or "",
                "namespace": metadata.get("namespace") or "",
                "cpu_millicores": cpu,
                "memory_bytes": memory,
                "container_count": len(containers),
                "timestamp": item.get("timestamp") or "",
                "window": item.get("window") or "",
            })
        return {"available": True, "items": rows}

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "pod-metrics",
            (ns,),
            _load_metrics,
            ttl_seconds=15,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return _metrics_not_available(str(exc))

@router.get("/{cluster_id}/nodes/{name}")
def get_node_detail(cluster_id: str, name: str):
    """获取单个节点的完整详情：元数据、状态、Pod 列表、资源分配。"""
    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)

    def _load_node_detail():
        node = v1.read_node(name=name, _request_timeout=6)
        # 节点上的 Pod
        pods_on_node = v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={name}", _request_timeout=10
        ).items

        # 汇总 Pod requests
        cpu_requests_m = 0
        mem_requests_bytes = 0
        pod_list = []
        for p in pods_on_node:
            cpu_req = 0
            mem_req = 0
            for c in (p.spec.containers or []):
                reqs = getattr(c, "resources", None)
                reqs = getattr(reqs, "requests", None) or {}
                cpu_req += parse_k8s_cpu(reqs.get("cpu", "0"))
                mem_req += parse_k8s_memory_to_gib(reqs.get("memory", "0"))
            cpu_requests_m += cpu_req
            mem_requests_bytes += mem_req

            pod_status = "Unknown"
            if p.status and p.status.phase:
                pod_status = p.status.phase
            pod_list.append({
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "status": pod_status,
                "cpu_request": round(cpu_req, 3),
                "mem_request": round(mem_req, 3),
            })

        capacity = node.status.capacity or {}
        allocatable = node.status.allocatable or {}
        cpu_cap = parse_k8s_cpu(capacity.get("cpu", "0"))
        mem_cap = parse_k8s_memory_to_gib(capacity.get("memory", "0"))

        # nodeInfo
        ni = node.status.node_info
        node_info = {
            "os": getattr(ni, "os_image", ""),
            "kernel": getattr(ni, "kernel_version", ""),
            "kubelet": getattr(ni, "kubelet_version", ""),
            "container_runtime": getattr(ni, "container_runtime_version", ""),
            "architecture": getattr(ni, "architecture", ""),
        } if ni else {}

        # conditions
        conditions = []
        for c in (node.status.conditions or []):
            conditions.append({
                "type": c.type,
                "status": c.status,
                "reason": getattr(c, "reason", ""),
                "message": getattr(c, "message", ""),
                "last_transition": str(getattr(c, "last_transition_time", ""))[:19],
            })

        # taints
        taints = []
        for t in (node.spec.taints or []):
            taints.append({
                "key": t.key,
                "value": getattr(t, "value", "") or "",
                "effect": t.effect,
            })

        # addresses
        addresses = [{"type": a.type, "address": a.address} for a in (node.status.addresses or [])]

        # role
        role = "Worker"
        for label in (node.metadata.labels or {}):
            if "control-plane" in label or "master" in label:
                role = "ControlPlane"

        return {
            "metadata": {
                "name": node.metadata.name,
                "labels": dict(node.metadata.labels or {}),
                "annotations": {k: v for k, v in (node.metadata.annotations or {}).items() if "kubectl" not in k},
                "creationTimestamp": str(node.metadata.creation_timestamp)[:19],
            },
            "spec": {
                "taints": taints,
                "unschedulable": bool(node.spec.unschedulable),
            },
            "status": {
                "conditions": conditions,
                "node_info": node_info,
                "addresses": addresses,
                "capacity": {"cpu": cpu_cap, "memory": mem_cap, "pods": capacity.get("pods", "")},
                "allocatable": {"cpu": parse_k8s_cpu(allocatable.get("cpu", "0")), "memory": parse_k8s_memory_to_gib(allocatable.get("memory", "0")), "pods": allocatable.get("pods", "")},
            },
            "role": role,
            "pods": pod_list,
            "allocation": {
                "cpu_requests": round(cpu_requests_m, 3),
                "cpu_capacity": cpu_cap,
                "cpu_percent": round(cpu_requests_m / cpu_cap * 100, 1) if cpu_cap > 0 else 0,
                "mem_requests": round(mem_requests_bytes, 3),
                "mem_capacity": mem_cap,
                "mem_percent": round(mem_requests_bytes / mem_cap * 100, 1) if mem_cap > 0 else 0,
                "pod_count": len(pod_list),
                "pod_capacity": int(capacity.get("pods", 110)),
            },
        }

    try:
        return _shared._get_or_set_k8s_read_cache(cluster_id, "node_detail", (name,), _load_node_detail, ttl_seconds=5)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"节点 {name} 不存在")
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取节点详情失败: {str(e)}")

_NODE_DRAIN_MAX_PODS = 100

_NODE_DRAIN_EVICTION_TIMEOUT = 5

def _pod_is_daemonset_managed(pod: Any) -> bool:
    """判断 Pod 是否由 DaemonSet 管理。DaemonSet Pod 在 drain 时默认跳过。"""
    for owner in getattr(pod.metadata, "owner_references", None) or []:
        if getattr(owner, "kind", "") == "DaemonSet":
            return True
    return False

def _pod_is_mirror(pod: Any) -> bool:
    """判断 Pod 是否为静态 Pod 的 mirror（在 API Server 上无法驱逐）。"""
    annotations = getattr(pod.metadata, "annotations", None) or {}
    return bool(annotations.get("kubernetes.io/config.mirror"))

def _pod_has_local_emptydir(pod: Any) -> bool:
    """判断 Pod 是否挂载了本地 emptyDir，drain 时会连带丢失数据。"""
    for volume in getattr(pod.spec, "volumes", None) or []:
        if getattr(volume, "empty_dir", None) is not None:
            return True
    return False

def _pod_is_standalone(pod: Any) -> bool:
    """判断 Pod 是否没有任何控制器接管（裸 Pod）。"""
    return not (getattr(pod.metadata, "owner_references", None) or [])

def _pod_is_terminal(pod: Any) -> bool:
    """处于 Succeeded / Failed 的 Pod 无需驱逐。"""
    phase = str(getattr(pod.status, "phase", "") or "").lower()
    return phase in {"succeeded", "failed"}

def _list_pods_on_node(api_client: Any, node_name: str) -> list[Any]:
    core_v1 = client.CoreV1Api(api_client=api_client)
    field_selector = f"spec.nodeName={node_name}"
    return core_v1.list_pod_for_all_namespaces(
        field_selector=field_selector,
        _request_timeout=8,
    ).items

def _summarize_pod_for_drain(pod: Any) -> dict[str, Any]:
    """把 Pod 精简成 drain 预检视图所需的最小字段。"""
    owners = [
        {"kind": getattr(o, "kind", ""), "name": getattr(o, "name", "")}
        for o in (pod.metadata.owner_references or [])
    ]
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": getattr(pod.status, "phase", "") or "Unknown",
        "controller": owners[0]["kind"] if owners else "",
        "owner": owners[0]["name"] if owners else "",
        "has_local_storage": _pod_has_local_emptydir(pod),
        "is_daemonset": _pod_is_daemonset_managed(pod),
        "is_mirror": _pod_is_mirror(pod),
        "is_standalone": _pod_is_standalone(pod),
    }

def _patch_node_unschedulable(api_client: Any, node_name: str, unschedulable: bool) -> None:
    """修改 Node spec.unschedulable。用于 cordon / uncordon。"""
    core_v1 = client.CoreV1Api(api_client=api_client)
    core_v1.patch_node(
        name=node_name,
        body={"spec": {"unschedulable": unschedulable}},
        _request_timeout=6,
    )

def _node_exists_or_404(api_client: Any, node_name: str) -> Any:
    core_v1 = client.CoreV1Api(api_client=api_client)
    try:
        return core_v1.read_node(name=node_name, _request_timeout=6)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Node {node_name} 不存在") from exc
        raise _raise_k8s_http_exception(exc, "读取节点信息")

_PROTECTED_LABEL_PREFIXES = (
    "kubernetes.io/",
    "node-role.kubernetes.io/",
    "node.kubernetes.io/",
)

@router.patch(
    "/{cluster_id}/nodes/{name}/labels",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "patch_labels")),
    ],
)
def patch_node_labels(cluster_id: str, name: str, payload: PatchNodeLabelsRequest):
    """Strategic merge patch 节点 labels。禁止修改核心 kubernetes.io 前缀的 label。"""
    # 安全校验：拒绝修改核心 label
    for key in payload.labels:
        if any(key.startswith(p) for p in _PROTECTED_LABEL_PREFIXES):
            raise HTTPException(status_code=403, detail=f"禁止修改核心 label: {key}")

    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)
    core_v1 = client.CoreV1Api(api_client=api_client)
    try:
        # value 为 None 的 key 在 strategic merge patch 中会被删除
        core_v1.patch_node(name=name, body={"metadata": {"labels": payload.labels}}, _request_timeout=6)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"Node {name} labels 已更新"}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "更新节点 labels")

@router.patch(
    "/{cluster_id}/nodes/{name}/taints",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "patch_taints")),
    ],
)
def patch_node_taints(cluster_id: str, name: str, payload: PatchNodeTaintsRequest):
    """替换节点 spec.taints 数组。"""
    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)
    core_v1 = client.CoreV1Api(api_client=api_client)
    try:
        core_v1.patch_node(name=name, body={"spec": {"taints": payload.taints or []}}, _request_timeout=6)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"Node {name} taints 已更新"}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "更新节点 taints")

@router.post(
    "/{cluster_id}/nodes/{name}/cordon",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "cordon")),
    ],
)
def cordon_node(cluster_id: str, name: str):
    """标记节点不可调度，但保留已有 Pod。

    相当于 `kubectl cordon`：只 PATCH `spec.unschedulable=true`，不驱逐任何 Pod。
    """
    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)
    try:
        _patch_node_unschedulable(api_client, name, True)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"Node {name} 已标记为不可调度"}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "节点 cordon")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"节点 cordon 失败: {exc}")

@router.post(
    "/{cluster_id}/nodes/{name}/uncordon",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "uncordon")),
    ],
)
def uncordon_node(cluster_id: str, name: str):
    """恢复节点可调度，相当于 `kubectl uncordon`。"""
    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)
    try:
        _patch_node_unschedulable(api_client, name, False)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"Node {name} 已恢复可调度"}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "节点 uncordon")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"节点 uncordon 失败: {exc}")

@router.get("/{cluster_id}/nodes/{name}/drain-preview")
def preview_node_drain(cluster_id: str, name: str):
    """drain 预检：列出节点上的 Pod 并按类型分桶。

    前端据此展示"将驱逐的 N 个 Pod、跳过的 M 个 DaemonSet Pod、阻断原因"，
    让用户在真实 drain 前能明确影响面。
    """
    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)
    try:
        pods = _list_pods_on_node(api_client, name)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "读取节点 Pod")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取节点 Pod 失败: {exc}")

    evictable: list[dict[str, Any]] = []
    daemonset_skipped: list[dict[str, Any]] = []
    mirror_skipped: list[dict[str, Any]] = []
    completed_skipped: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for pod in pods:
        summary = _summarize_pod_for_drain(pod)
        if _pod_is_mirror(pod):
            mirror_skipped.append(summary)
            continue
        if _pod_is_terminal(pod):
            completed_skipped.append(summary)
            continue
        if _pod_is_daemonset_managed(pod):
            daemonset_skipped.append(summary)
            continue
        evictable.append(summary)
        if summary["is_standalone"]:
            blockers.append({"pod": summary, "reason": "该 Pod 没有 Deployment/StatefulSet 等控制器接管，驱逐后不会自动重建"})
        if summary["has_local_storage"]:
            warnings.append({"pod": summary, "reason": "该 Pod 挂载了 emptyDir，驱逐后本地数据会丢失"})

    # 超出批量驱逐上限也视为 blocker，避免误点把整个集群拉爆。
    over_limit = len(evictable) > _NODE_DRAIN_MAX_PODS

    return {
        "node": name,
        "evictable_count": len(evictable),
        "daemonset_skipped_count": len(daemonset_skipped),
        "mirror_skipped_count": len(mirror_skipped),
        "completed_skipped_count": len(completed_skipped),
        "evictable": evictable,
        "daemonset_skipped": daemonset_skipped,
        "mirror_skipped": mirror_skipped,
        "completed_skipped": completed_skipped,
        "blockers": blockers,
        "warnings": warnings,
        "max_evict_pods": _NODE_DRAIN_MAX_PODS,
        "over_limit": over_limit,
    }

@router.post(
    "/{cluster_id}/nodes/{name}/drain",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "drain")),
    ],
)
def drain_node(cluster_id: str, name: str, payload: NodeDrainRequest):
    """驱逐节点上的 Pod，为节点维护做准备。

    流程对齐 `kubectl drain` 的主流程：
    1. 先 cordon 节点（`spec.unschedulable=true`）；
    2. 读节点上的 Pod，跳过 DaemonSet、mirror、已完成 Pod；
    3. 若存在 emptyDir 或无控制器的裸 Pod，只有在显式 `delete_emptydir_data` /
       `force` 时才驱逐，否则把该 Pod 列为失败项；
    4. 逐个调用 `create_namespaced_pod_eviction`，单次上限 100 个 Pod，
       每个驱逐请求独立超时，收集 success / skipped / failed 列表返回。

    安全边界：
    - 需要 `action.assets.clusters.manage` + `drain` 风险确认；
    - 所有动作只用固定白名单 API 调用，不下发任意 yaml；
    - 超上限直接拒绝而不是静默截断，避免误判"看起来成功"；
    - 失败不 rollback cordon：让运维决定是继续排查还是 uncordon 恢复。
    """
    api_client = _shared._get_api_client(cluster_id)
    _node_exists_or_404(api_client, name)

    # Step 1: cordon
    try:
        _patch_node_unschedulable(api_client, name, True)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "drain 前 cordon")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"drain 前 cordon 失败: {exc}")

    # Step 2: list pods
    try:
        pods = _list_pods_on_node(api_client, name)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "读取节点 Pod")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取节点 Pod 失败: {exc}")

    # 规划驱逐清单
    to_evict: list[Any] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for pod in pods:
        summary = _summarize_pod_for_drain(pod)
        if _pod_is_mirror(pod):
            skipped.append({"pod": summary, "reason": "mirror pod，无法驱逐"})
            continue
        if _pod_is_terminal(pod):
            skipped.append({"pod": summary, "reason": "Pod 已处于终结态"})
            continue
        if _pod_is_daemonset_managed(pod):
            if payload.ignore_daemonsets:
                skipped.append({"pod": summary, "reason": "DaemonSet 管控 Pod，按照默认策略跳过"})
                continue
            # 显式要求不忽略 DaemonSet 时仍然不直接删除：K8s 不允许驱逐 DaemonSet Pod。
            failed.append({"pod": summary, "reason": "DaemonSet Pod 无法被 eviction 驱逐，请先删除对应 DaemonSet"})
            continue
        if summary["is_standalone"] and not payload.force:
            failed.append({"pod": summary, "reason": "该 Pod 无控制器接管，需要 force=true 才能驱逐"})
            continue
        if summary["has_local_storage"] and not payload.delete_emptydir_data:
            failed.append({"pod": summary, "reason": "该 Pod 挂载了 emptyDir，需要 delete_emptydir_data=true 才能驱逐"})
            continue
        to_evict.append(pod)

    if len(to_evict) > _NODE_DRAIN_MAX_PODS:
        raise HTTPException(
            status_code=400,
            detail=f"待驱逐 Pod 数量 {len(to_evict)} 超过单次上限 {_NODE_DRAIN_MAX_PODS}，请分批执行",
        )

    core_v1 = client.CoreV1Api(api_client=api_client)
    evicted: list[dict[str, Any]] = []
    for pod in to_evict:
        summary = _summarize_pod_for_drain(pod)
        body = client.V1Eviction(
            metadata=client.V1ObjectMeta(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
            ),
            delete_options=client.V1DeleteOptions(
                grace_period_seconds=payload.grace_period_seconds,
            ),
        )
        try:
            core_v1.create_namespaced_pod_eviction(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                body=body,
                _request_timeout=_NODE_DRAIN_EVICTION_TIMEOUT,
            )
            evicted.append(summary)
        except ApiException as exc:
            # 429: PDB 阻断；409: 资源状态冲突。两者都记为 failed，让用户决定下一步。
            failed.append({
                "pod": summary,
                "reason": f"驱逐失败: HTTP {exc.status} {exc.reason or ''}".strip(),
            })
        except Exception as exc:  # noqa: BLE001
            failed.append({"pod": summary, "reason": f"驱逐失败: {exc}"})

    _shared._clear_k8s_read_cache(cluster_id)

    return {
        "success": len(failed) == 0,
        "node": name,
        "cordoned": True,
        "requested": len(to_evict),
        "evicted_count": len(evicted),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "evicted": evicted,
        "skipped": skipped,
        "failed": failed,
    }

