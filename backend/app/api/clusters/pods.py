"""Pod list/detail/diagnostics/delete, PVC/Ingress, logs/exec WS."""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from fastapi import (
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from kubernetes import client, watch
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.user import User

from ._router import router, ws_router
from ._shared import (
    _format_age,
    _normalize_k8s_limit,
    _open_pod_exec_stream,
    _page_response,
    _parse_memory_quantity,
    _pod_ready,
    _pod_restarts,
    _pod_status,
    _raise_k8s_http_exception,
    _safe_continue_token,
    _validate_k8s_selector,
    _diagnose_pod,
    logger,
)
from . import _shared
from .ws_session import _consume_k8s_ws_session, _create_k8s_ws_session

@router.get("/{cluster_id}/pods")
def list_pods(
    cluster_id: str,
    namespace: str = Query("_all", description="namespace, _all for all"),
    limit: int | None = Query(None, ge=1, le=500, description="分页大小，最大 500"),
    continue_token: str = Query("", description="Kubernetes continue token"),
    label_selector: str = Query("", description="Kubernetes labelSelector"),
    field_selector: str = Query("", description="Kubernetes fieldSelector"),
    paginated: bool = Query(False, description="true 时返回分页 envelope，false 保持旧版列表结构"),
):
    """List pods in specified namespace (or all namespaces)."""
    api_client = _shared._get_api_client(cluster_id)
    page_limit = _normalize_k8s_limit(limit)
    safe_continue_token = _safe_continue_token(continue_token)
    safe_label_selector = _validate_k8s_selector(label_selector, "label_selector")
    safe_field_selector = _validate_k8s_selector(field_selector, "field_selector")
    use_k8s_paging = paginated or limit is not None or bool(safe_continue_token)

    def _pod_list_kwargs() -> dict[str, Any]:
        kwargs: dict[str, Any] = {"_request_timeout": 6}
        if use_k8s_paging:
            kwargs["limit"] = page_limit
        if safe_continue_token:
            kwargs["_continue"] = safe_continue_token
        if safe_label_selector:
            kwargs["label_selector"] = safe_label_selector
        if safe_field_selector:
            kwargs["field_selector"] = safe_field_selector
        return kwargs

    def _load_pods() -> list[dict[str, Any]] | dict[str, Any]:
        v1 = client.CoreV1Api(api_client=api_client)
        kwargs = _pod_list_kwargs()
        if namespace and namespace != "_all":
            response = v1.list_namespaced_pod(namespace=namespace, **kwargs)
        else:
            response = v1.list_pod_for_all_namespaces(**kwargs)
        pods = response.items

        result = []
        for p in pods:
            result.append({
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "status": _pod_status(p),
                "ready": _pod_ready(p),
                "restarts": _pod_restarts(p),
                "diagnosis": _diagnose_pod(p),
                "node": p.spec.node_name or "N/A",
                "age": _format_age(p.metadata.creation_timestamp),
                "image": p.spec.containers[0].image if p.spec.containers else "",
                "containers": [cs.name for cs in (p.status.container_statuses or [])],
                "labels": p.metadata.labels or {},
                "owner_references": [{"kind": o.kind, "name": o.name, "uid": o.uid} for o in (p.metadata.owner_references or [])]
            })
            if not use_k8s_paging and len(result) >= 200:
                break
        if paginated:
            metadata = getattr(response, "metadata", None)
            next_token = getattr(metadata, "_continue", "") if metadata else ""
            return _page_response(result, next_token, page_limit)
        return result

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "pods",
            (
                namespace or "_all",
                page_limit if use_k8s_paging else 0,
                safe_continue_token,
                safe_label_selector,
                safe_field_selector,
                paginated,
            ),
            _load_pods,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"K8s API pod query failed: {str(e)}")

@router.get("/{cluster_id}/pods/{namespace}/{name}")
def get_pod_detail(cluster_id: str, namespace: str, name: str):
    """Get pod detail including labels, annotations, containers, volumes, conditions."""
    api_client = _shared._get_api_client(cluster_id)
    try:
        v1 = client.CoreV1Api(api_client=api_client)
        pod = v1.read_namespaced_pod(name=name, namespace=namespace, _request_timeout=6)

        containers = []
        for c in (pod.spec.containers or []):
            containers.append({
                "name": c.name,
                "image": c.image,
                "ports": [p.container_port for p in (c.ports or [])],
                "env": [{"name": e.name, "value": e.value or ""} for e in (c.env or [])],
                "volume_mounts": [
                    {"name": vm.name, "mount_path": vm.mount_path}
                    for vm in (c.volume_mounts or [])
                ],
            })

        volumes = []
        for v in (pod.spec.volumes or []):
            vol = {"name": v.name}
            if v.config_map:
                vol["config_map"] = {"name": v.config_map.name}
            elif v.secret:
                vol["secret"] = {"secret_name": v.secret.secret_name}
            elif v.persistent_volume_claim:
                vol["pvc"] = {"claim_name": v.persistent_volume_claim.claim_name}
            elif v.empty_dir:
                vol["empty_dir"] = True
            elif v.host_path:
                vol["host_path"] = {"path": v.host_path.path}
            volumes.append(vol)

        conditions = [
            {"type": c.type, "status": c.status}
            for c in (pod.status.conditions or [])
        ]

        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": _pod_status(pod),
            "labels": pod.metadata.labels or {},
            "annotations": pod.metadata.annotations or {},
            "containers": containers,
            "volumes": volumes,
            "conditions": conditions,
            "node": pod.spec.node_name or "N/A",
            "ip": pod.status.pod_ip or "N/A",
            "age": _format_age(pod.metadata.creation_timestamp),
        }
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Pod {namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"K8s API error: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pod detail: {str(e)}")

def _container_state_payload(state: Any) -> dict[str, Any]:
    """把 V1ContainerState 拍平成 {state, reason, message, last_terminated}。

    设计权衡：诊断卡只关心 waiting/terminated 的 reason+message，
    running 状态只标识，不返回 startedAt（首屏不需要）。
    """
    if not state:
        return {"state": "Unknown", "state_reason": "", "state_message": ""}
    if state.running:
        return {"state": "Running", "state_reason": "", "state_message": ""}
    if state.waiting:
        return {
            "state": "Waiting",
            "state_reason": state.waiting.reason or "",
            "state_message": (state.waiting.message or "")[:512],
        }
    if state.terminated:
        return {
            "state": "Terminated",
            "state_reason": state.terminated.reason or "",
            "state_message": (state.terminated.message or "")[:512],
        }
    return {"state": "Unknown", "state_reason": "", "state_message": ""}

def _last_terminated_payload(last_state: Any) -> dict[str, Any] | None:
    """提取容器上一次终止信息：诊断 CrashLoopBackOff 的核心证据。"""
    if not last_state or not getattr(last_state, "terminated", None):
        return None
    term = last_state.terminated
    return {
        "exit_code": term.exit_code,
        "reason": term.reason or "",
        "signal": term.signal,
        "started_at": term.started_at.isoformat() if term.started_at else "",
        "finished_at": term.finished_at.isoformat() if term.finished_at else "",
        "message": (term.message or "")[:512],
    }

def _probe_summary(probe: Any, container: str, kind: str) -> dict[str, Any] | None:
    """把 Probe spec 拍平成一行可读摘要。

    安全：K8s Probe 是公开字段，handler.exec.command 由用户编排，
    不会包含运行时密文；这里只做截断，不做敏感字段剔除。
    """
    if not probe:
        return None
    handler_kind = ""
    config_summary = ""
    if probe.http_get:
        handler_kind = "httpGet"
        scheme = probe.http_get.scheme or "HTTP"
        host = probe.http_get.host or ""
        path = probe.http_get.path or "/"
        port = probe.http_get.port if probe.http_get.port is not None else ""
        config_summary = f"{scheme} {host}{path}:{port}".strip()
    elif probe.tcp_socket:
        handler_kind = "tcpSocket"
        config_summary = f"TCP :{probe.tcp_socket.port}"
    elif probe._exec:  # field 名是 exec，但 python 客户端用 _exec 避开关键字
        handler_kind = "exec"
        cmd = probe._exec.command or []
        config_summary = " ".join(str(p) for p in cmd)[:200]
    elif probe.grpc:
        handler_kind = "grpc"
        config_summary = f"gRPC :{probe.grpc.port}"
    return {
        "container": container,
        "type": kind,
        "handler": handler_kind,
        "config_summary": config_summary,
        "initial_delay_seconds": probe.initial_delay_seconds or 0,
        "period_seconds": probe.period_seconds or 10,
        "timeout_seconds": probe.timeout_seconds or 1,
        "failure_threshold": probe.failure_threshold or 3,
    }

def _collect_probes(pod: Any) -> list[dict[str, Any]]:
    """聚合 Pod 全部容器的 liveness/readiness/startup probe 摘要。"""
    rows: list[dict[str, Any]] = []
    for c in (pod.spec.containers or []):
        for kind, probe in (
            ("liveness", c.liveness_probe),
            ("readiness", c.readiness_probe),
            ("startup", c.startup_probe),
        ):
            row = _probe_summary(probe, c.name, kind)
            if row:
                rows.append(row)
    return rows

def _safe_tail_logs(
    api_client: Any,
    namespace: str,
    name: str,
    container: str,
    tail_lines: int,
    previous: bool,
) -> dict[str, Any]:
    """单容器日志 tail，best-effort 降级；任何异常都包成 error 字段而不抛。"""
    v1 = client.CoreV1Api(api_client=api_client)
    try:
        raw = v1.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
            _request_timeout=4,
        )
        lines = (raw or "").splitlines()[-tail_lines:]
        return {
            "container": container,
            "previous": previous,
            "lines": lines,
            "error": None,
        }
    except ApiException as exc:
        # 400: previous container 不存在；404: 容器还没起来；皆视为正常降级
        return {
            "container": container,
            "previous": previous,
            "lines": [],
            "error": f"{exc.status} {exc.reason}".strip() or "log unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "container": container,
            "previous": previous,
            "lines": [],
            "error": str(exc)[:200],
        }

_DIAGNOSTIC_HINTS: dict[str, list[str]] = {
    "OOMKilled": [
        "容器被 OOM 终止，建议提升 resources.limits.memory 至当前 RSS 峰值的 1.5x 以上",
        "确认是否存在内存泄漏：观察重启间隔与 RSS 增长曲线",
    ],
    "CrashLoopBackOff": [
        "容器持续崩溃重启，建议查看下方日志尾部与 lastTerminated.exitCode 定位崩溃原因",
        "若 exitCode 为 0，多是探针配置不当导致主动重启",
    ],
    "ImagePullBackOff": [
        "镜像拉取失败，确认镜像名/仓库认证（imagePullSecrets）/网络可达",
    ],
    "ErrImagePull": [
        "镜像拉取错误，常见为镜像名拼写错误或私有仓库未配置 imagePullSecrets",
    ],
    "CreateContainerConfigError": [
        "容器配置错误，常见为引用了不存在的 ConfigMap/Secret 键",
    ],
    "CreateContainerError": [
        "容器创建失败，确认 securityContext / volumeMounts 是否合法",
    ],
    "RunContainerError": [
        "容器启动失败，多为镜像 entrypoint/command 不可执行，或缺少依赖动态库",
    ],
    "InvalidImageName": [
        "镜像名格式非法，检查 image 字段拼写与 tag",
    ],
    "Unschedulable": [
        "调度失败，查看 Warning 事件中的 FailedScheduling 原因（资源不足 / 亲和性 / 污点）",
    ],
    "FailedMount": [
        "Volume 挂载失败，确认 PVC 已 Bound 且 StorageClass/CSI 驱动健康",
    ],
    "FailedAttachVolume": [
        "Volume 挂接失败，检查节点与存储后端连通性",
    ],
}

def _build_diagnostic_summary(
    pod: Any,
    container_statuses: list[dict[str, Any]],
    init_container_statuses: list[dict[str, Any]],
    recent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """根据 containerStatuses + events 规则化输出 summary.headline 与 hints。

    设计权衡：不调 LLM。规则覆盖 80% 高频异常，未覆盖时退化为
    "Pod 处于 X 阶段，最近 N 条警告事件" 的中性描述，避免误导。
    """
    phase = pod.status.phase or "Unknown"
    if phase == "Running" and all(cs["state"] == "Running" and cs["ready"] for cs in container_statuses):
        return {"level": "ok", "headline": "Pod 正常运行", "hints": []}

    # 优先级：waiting reason > terminated reason > pending unschedulable > generic
    hints: list[str] = []
    headline = ""
    level = "warning"

    # 1) 从主容器 + init 容器中找第一个异常 waiting reason
    abnormal_cs = None
    for cs in init_container_statuses + container_statuses:
        reason = cs.get("state_reason") or ""
        if cs["state"] == "Waiting" and reason and reason not in ("ContainerCreating", "PodInitializing"):
            abnormal_cs = cs
            break

    if abnormal_cs:
        reason = abnormal_cs["state_reason"]
        last_term = abnormal_cs.get("last_terminated") or {}
        exit_info = ""
        if last_term:
            ec = last_term.get("exit_code")
            term_reason = last_term.get("reason") or ""
            if term_reason == "OOMKilled":
                exit_info = f"，上次因 OOMKilled 终止"
                # OOM 的引导话术优先级高于 CrashLoop
                hints = list(_DIAGNOSTIC_HINTS.get("OOMKilled", []))
            elif ec is not None:
                exit_info = f"，上次退出码 {ec}"
        headline = (
            f"容器 {abnormal_cs['name']} 状态 {reason}"
            f"{exit_info}，重启 {abnormal_cs.get('restart_count', 0)} 次"
        )
        if not hints:
            hints = list(_DIAGNOSTIC_HINTS.get(reason, []))
        level = "critical" if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") else "warning"

    # 2) Pending 且没有容器状态：调度阶段
    elif phase == "Pending" and not container_statuses:
        # 从 events 找 FailedScheduling
        sched_event = next(
            (e for e in recent_events if e.get("reason") == "FailedScheduling"),
            None,
        )
        if sched_event:
            headline = f"Pod 处于 Pending：调度失败（{sched_event.get('message', '')[:120]}）"
            hints = list(_DIAGNOSTIC_HINTS["Unschedulable"])
            level = "critical"
        else:
            headline = "Pod 处于 Pending，等待调度或资源就绪"
            hints = ["查看下方事件了解阻塞点（节点资源、PVC、亲和性等）"]

    # 3) 兜底：基于 phase + 警告事件数量
    else:
        warn_count = sum(1 for e in recent_events if e.get("type") == "Warning")
        headline = f"Pod 处于 {phase}，最近 {warn_count} 条 Warning 事件"
        if warn_count > 0:
            level = "warning"

    return {"level": level, "headline": headline, "hints": hints}

_DIAGNOSTICS_LOG_TAIL = 50  # 每容器返回的日志行数硬上限

_DIAGNOSTICS_EVENT_LIMIT = 30

_DIAGNOSTICS_CACHE_TTL = 3  # 比通用读缓存(5s)更短，诊断要更实时

@router.get("/{cluster_id}/pods/{namespace}/{name}/diagnostics")
def get_pod_diagnostics(
    cluster_id: str,
    namespace: str,
    name: str,
    include_logs: bool = Query(True, description="是否包含容器日志尾部，关闭可加速"),
):
    """Pod 异常一键诊断聚合接口。

    一次请求返回：containerStatuses + 近期 events + 日志尾部 + probe 摘要 + summary。
    所有分片 best-effort：单一来源失败不影响整体响应。

    安全边界：
    - 仅读，不执行任何写操作；
    - 不返回 Pod env 中的 secret valueFrom 实际值（K8s read_namespaced_pod 本身就不解密 secret）；
    - 日志体积硬上限 = 容器数 × 50 行；events 上限 30 条；
    - 复用现有 `_get_or_set_k8s_read_cache`，避免崩溃 Pod 被反复点击造成 API 压力。
    """
    api_client = _shared._get_api_client(cluster_id)

    def _load_diagnostics() -> dict[str, Any]:
        v1 = client.CoreV1Api(api_client=api_client)
        try:
            pod = v1.read_namespaced_pod(name=name, namespace=namespace, _request_timeout=6)
        except ApiException as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404, detail=f"Pod {namespace}/{name} 不存在")
            raise _raise_k8s_http_exception(exc, "Pod 诊断")

        # —— containerStatuses & initContainerStatuses ——
        def _flatten_statuses(statuses: list[Any] | None) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for cs in statuses or []:
                state_payload = _container_state_payload(cs.state)
                rows.append({
                    "name": cs.name,
                    "ready": bool(cs.ready),
                    "restart_count": cs.restart_count or 0,
                    "image": cs.image or "",
                    "image_id": (cs.image_id or "").split("@")[-1][:64],
                    **state_payload,
                    "last_terminated": _last_terminated_payload(cs.last_state),
                })
            return rows

        container_statuses = _flatten_statuses(pod.status.container_statuses)
        init_container_statuses = _flatten_statuses(pod.status.init_container_statuses)

        # —— events ——
        recent_events: list[dict[str, Any]] = []
        try:
            field_selector = (
                f"involvedObject.name={name},"
                f"involvedObject.namespace={namespace},"
                f"involvedObject.kind=Pod"
            )
            ev_resp = v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
                limit=_DIAGNOSTICS_EVENT_LIMIT,
                _request_timeout=4,
            )
            for ev in ev_resp.items:
                last_ts = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
                recent_events.append({
                    "type": ev.type or "",
                    "reason": ev.reason or "",
                    "message": (ev.message or "")[:512],
                    "count": ev.count or 1,
                    "last_timestamp": last_ts.isoformat() if last_ts else "",
                    "source": (ev.source.component if ev.source else "") or "",
                })
            recent_events.sort(key=lambda e: e["last_timestamp"], reverse=True)
        except ApiException:
            recent_events = []
        except Exception:  # noqa: BLE001
            recent_events = []

        # —— logs：每容器最多 50 行；只对状态非 Running 的容器拉 previous 兜底 ——
        recent_logs: list[dict[str, Any]] = []
        if include_logs:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                # 主容器 + init 容器都拉一次当前 logs（previous 仅在异常时尝试）
                for cs in container_statuses + init_container_statuses:
                    futures.append(
                        executor.submit(
                            _safe_tail_logs,
                            api_client, namespace, name, cs["name"],
                            _DIAGNOSTICS_LOG_TAIL, False,
                        )
                    )
                    if cs.get("restart_count", 0) > 0 or (cs.get("last_terminated") or {}).get("exit_code", 0) != 0:
                        futures.append(
                            executor.submit(
                                _safe_tail_logs,
                                api_client, namespace, name, cs["name"],
                                _DIAGNOSTICS_LOG_TAIL, True,
                            )
                        )
                for f in futures:
                    try:
                        recent_logs.append(f.result(timeout=6))
                    except Exception as exc:  # noqa: BLE001
                        recent_logs.append({
                            "container": "?",
                            "previous": False,
                            "lines": [],
                            "error": str(exc)[:200],
                        })

        # —— probes ——
        probes = _collect_probes(pod)

        # —— summary ——
        summary = _build_diagnostic_summary(pod, container_statuses, init_container_statuses, recent_events)

        return {
            "phase": pod.status.phase or "Unknown",
            "node": pod.spec.node_name or "",
            "pod_ip": pod.status.pod_ip or "",
            "container_statuses": container_statuses,
            "init_container_statuses": init_container_statuses,
            "recent_events": recent_events,
            "recent_logs": recent_logs,
            "probes": probes,
            "summary": summary,
        }

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "pod_diagnostics",
            (namespace, name, bool(include_logs)),
            _load_diagnostics,
            ttl_seconds=_DIAGNOSTICS_CACHE_TTL,
        )
    except HTTPException:
        raise
    except ApiException as e:
        raise _raise_k8s_http_exception(e, "Pod 诊断")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Pod 诊断失败: {str(e)}")

@router.delete(
    "/{cluster_id}/pods/{namespace}/{name}",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "delete")),
    ],
)
def delete_pod(cluster_id: str, namespace: str, name: str):
    """Delete pod (managed pods will be auto-recreated, equivalent to restart)."""
    api_client = _shared._get_api_client(cluster_id)
    try:
        v1 = client.CoreV1Api(api_client=api_client)
        v1.delete_namespaced_pod(name=name, namespace=namespace, _request_timeout=6)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"Pod {name} deleted"}
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Pod {namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"Failed to delete pod: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete pod: {str(e)}")

@router.patch(
    "/{cluster_id}/pvc/{namespace}/{name}/resize",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "resize_pvc")),
    ],
)
def resize_pvc(cluster_id: str, namespace: str, name: str, new_size: str = Query(..., description="新容量，如 10Gi")):
    """PVC 扩容（只能往上）。"""
    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)
    try:
        pvc = v1.read_namespaced_persistent_volume_claim(name=name, namespace=namespace, _request_timeout=6)
        current = pvc.spec.resources.requests.get("storage", "0")
        if _parse_memory_quantity(new_size) <= _parse_memory_quantity(current):
            raise HTTPException(status_code=400, detail=f"新容量 {new_size} 必须大于当前 {current}")
        v1.patch_namespaced_persistent_volume_claim(
            name=name, namespace=namespace,
            body={"spec": {"resources": {"requests": {"storage": new_size}}}},
            _request_timeout=6,
        )
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"PVC {name} 已扩容至 {new_size}"}
    except HTTPException:
        raise
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "PVC 扩容")

@router.get("/{cluster_id}/ingress/{namespace}/{name}/tls-status")
def get_ingress_tls_status(cluster_id: str, namespace: str, name: str):
    """获取 Ingress TLS 证书状态（有效期）。"""
    import base64
    from datetime import datetime, timezone
    from cryptography import x509

    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)
    networking_v1 = client.NetworkingV1Api(api_client=api_client)

    try:
        ingress = networking_v1.read_namespaced_ingress(name=name, namespace=namespace, _request_timeout=6)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "读取 Ingress")

    tls_list = ingress.spec.tls or []
    results = []
    for tls_entry in tls_list:
        secret_name = tls_entry.secret_name
        hosts = tls_entry.hosts or []
        entry = {"secret_name": secret_name, "hosts": hosts, "status": "unknown", "not_after": None, "days_remaining": None}
        if not secret_name:
            entry["status"] = "no_secret"
            results.append(entry)
            continue
        try:
            secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace, _request_timeout=5)
            cert_data = (secret.data or {}).get("tls.crt")
            if not cert_data:
                entry["status"] = "no_cert_data"
                results.append(entry)
                continue
            cert_bytes = base64.b64decode(cert_data)
            cert = x509.load_pem_x509_certificate(cert_bytes)
            not_after = cert.not_valid_after_utc
            now = datetime.now(timezone.utc)
            days_remaining = (not_after - now).days
            entry["not_after"] = not_after.isoformat()
            entry["days_remaining"] = days_remaining
            entry["status"] = "expired" if days_remaining < 0 else "expiring_soon" if days_remaining < 30 else "valid"
        except Exception:
            entry["status"] = "error"
        results.append(entry)

    return {"tls": results}

@router.post("/{cluster_id}/pods/{namespace}/{name}/logs/session")
def create_pod_logs_ws_session(
    cluster_id: str,
    namespace: str,
    name: str,
    container: str = Query(""),
    tail: int = Query(100, ge=1, le=1000),
    previous: bool = Query(False),
    current_user: User = Depends(get_current_user),
):
    """创建 Pod 日志 WebSocket 一次性会话。"""
    session_id, token, expire_epoch_ms = _create_k8s_ws_session(
        cluster_id=cluster_id,
        namespace=namespace,
        pod_name=name,
        action="logs",
        created_by=current_user.name or current_user.username,
        container=container,
        tail=tail,
        previous=previous,
    )
    return {
        "success": True,
        "session_id": session_id,
        "session_token": token,
        "expires_at": expire_epoch_ms,
        "ws_path": f"/api/clusters/{cluster_id}/pods/{namespace}/{name}/logs",
    }

@router.post(
    "/{cluster_id}/pods/{namespace}/{name}/exec/session",
    dependencies=[Depends(require_permissions("action.assets.clusters.manage"))],
)
def create_pod_exec_ws_session(
    cluster_id: str,
    namespace: str,
    name: str,
    container: str = Query(""),
    command: str = Query("/bin/sh"),
    current_user: User = Depends(get_current_user),
):
    """创建 Pod exec WebSocket 一次性会话。"""
    session_id, token, expire_epoch_ms = _create_k8s_ws_session(
        cluster_id=cluster_id,
        namespace=namespace,
        pod_name=name,
        action="exec",
        created_by=current_user.name or current_user.username,
        container=container,
        command=command,
    )
    return {
        "success": True,
        "session_id": session_id,
        "session_token": token,
        "expires_at": expire_epoch_ms,
        "ws_path": f"/api/clusters/{cluster_id}/pods/{namespace}/{name}/exec",
    }

@ws_router.websocket("/{cluster_id}/pods/{namespace}/{name}/logs")
async def pod_logs_ws(websocket: WebSocket, cluster_id: str, namespace: str, name: str,
                      container: str = Query(""), tail: int = Query(100, ge=1, le=1000)):
    """Streaming log WebSocket endpoint. Max duration 1 hour."""
    await websocket.accept()
    session_id = (websocket.query_params.get("session_id") or "").strip()
    token = (websocket.query_params.get("token") or "").strip()
    ok, message, session_context = _consume_k8s_ws_session(
        session_id,
        token,
        cluster_id=cluster_id,
        namespace=namespace,
        pod_name=name,
        action="logs",
    )
    if not ok or session_context is None:
        logger.warning(
            "K8s logs WebSocket session rejected: cluster=%s namespace=%s pod=%s reason=%s",
            cluster_id,
            namespace,
            name,
            message,
        )
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=4401, reason=message)
        return
    container = session_context.container
    tail = session_context.tail
    use_previous = session_context.previous
    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        await websocket.send_json({"type": "error", "message": f"Cluster {cluster_id} kubeconfig not found"})
        await websocket.close(code=1008)
        return

    await websocket.send_json({"type": "system", "message": "Log stream connected"})

    log_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    loop = asyncio.get_event_loop()
    MAX_DURATION = 3600

    def _watch_logs():
        """Run K8s watch in background thread, push log lines to queue."""
        try:
            api_client = _shared._get_api_client(cluster_id)
            v1 = client.CoreV1Api(api_client=api_client)
            kwargs = {
                "name": name,
                "namespace": namespace,
                "tail_lines": tail,
            }
            if container:
                kwargs["container"] = container
            if use_previous:
                # previous 日志是有限的，不走 follow/watch 流式
                kwargs["previous"] = True
                kwargs["_request_timeout"] = 30
                log_text = v1.read_namespaced_pod_log(**kwargs)
                for line in (log_text or "").split("\n"):
                    loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "log", "data": line})
                loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "system", "message": "Previous log stream ended"})
            else:
                kwargs["follow"] = True
                kwargs["_request_timeout"] = MAX_DURATION
                w = watch.Watch()
                for line in w.stream(v1.read_namespaced_pod_log, **kwargs):
                    try:
                        loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "log", "data": line})
                    except asyncio.QueueFull:
                        try:
                            loop.call_soon_threadsafe(log_queue.get_nowait)
                            loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "log", "data": line})
                        except Exception:
                            pass
                loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "system", "message": "Log stream ended"})
        except ApiException as e:
            msg = f"Container not found or logs unavailable"
            if e.status == 404:
                msg = f"Pod {namespace}/{name} not found"
            elif e.status == 400:
                msg = "上一次容器日志不可用（容器可能未重启过）"
            elif e.reason:
                msg = e.reason
            loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "error", "message": msg})
        except Exception as e:
            loop.call_soon_threadsafe(log_queue.put_nowait, {"type": "error", "message": str(e)})
        finally:
            loop.call_soon_threadsafe(log_queue.put_nowait, None)

    import threading
    thread = threading.Thread(target=_watch_logs, daemon=True)
    thread.start()

    start_time = time.time()
    try:
        while True:
            if time.time() - start_time > MAX_DURATION:
                await websocket.send_json({"type": "system", "message": "Log stream timed out (1 hour)"})
                break

            try:
                msg = await asyncio.wait_for(log_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if msg is None:
                break

            await websocket.send_json(msg)
            if msg.get("type") == "error":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

@ws_router.websocket("/{cluster_id}/pods/{namespace}/{name}/exec")
async def pod_exec_ws(websocket: WebSocket, cluster_id: str, namespace: str, name: str,
                      container: str = Query(""), command: str = Query("/bin/sh")):
    """Interactive terminal WebSocket endpoint. Idle timeout 5 minutes."""
    await websocket.accept()
    session_id = (websocket.query_params.get("session_id") or "").strip()
    token = (websocket.query_params.get("token") or "").strip()
    ok, message, session_context = _consume_k8s_ws_session(
        session_id,
        token,
        cluster_id=cluster_id,
        namespace=namespace,
        pod_name=name,
        action="exec",
    )
    if not ok or session_context is None:
        logger.warning(
            "K8s exec WebSocket session rejected: cluster=%s namespace=%s pod=%s reason=%s",
            cluster_id,
            namespace,
            name,
            message,
        )
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=4401, reason=message)
        return
    container = session_context.container
    command = session_context.command
    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        await websocket.send_json({"type": "error", "message": f"Cluster {cluster_id} kubeconfig not found"})
        await websocket.close(code=1008)
        return

    try:
        api_client = _shared._get_api_client(cluster_id)
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Failed to connect: {str(e)}"})
        await websocket.close(code=1011)
        return

    await websocket.send_json({"type": "system", "message": "Connecting to terminal..."})

    # Determine target container if not specified
    target_container = container
    if not target_container:
        try:
            v1 = client.CoreV1Api(api_client=api_client)
            pod = v1.read_namespaced_pod(name=name, namespace=namespace, _request_timeout=6)
            if pod.spec.containers:
                target_container = pod.spec.containers[0].name
        except Exception:
            pass

    exec_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    loop = asyncio.get_event_loop()
    IDLE_TIMEOUT = 300
    ws_process = None

    def _run_exec():
        """Run exec connection in background thread."""
        nonlocal ws_process
        try:
            resp = _open_pod_exec_stream(
                api_client=api_client,
                namespace=namespace,
                name=name,
                command=command,
                container=target_container or "",
                timeout_seconds=IDLE_TIMEOUT + 60,
            )
            ws_process = resp

            loop.call_soon_threadsafe(exec_queue.put_nowait, {
                "type": "system",
                "message": f"Connected to {namespace}/{name}" + (f" ({target_container})" if target_container else "")
            })

            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    data = resp.read_stdout()
                    loop.call_soon_threadsafe(exec_queue.put_nowait, {"type": "output", "data": data})
                if resp.peek_stderr():
                    data = resp.read_stderr()
                    loop.call_soon_threadsafe(exec_queue.put_nowait, {"type": "output", "data": data})

            loop.call_soon_threadsafe(exec_queue.put_nowait, {"type": "system", "message": "Terminal session ended"})

        except ApiException as e:
            msg = f"Exec failed: {e.reason or str(e)}"
            if e.status == 404:
                msg = f"Pod {namespace}/{name} not found"
            loop.call_soon_threadsafe(exec_queue.put_nowait, {"type": "error", "message": msg})
        except Exception as e:
            loop.call_soon_threadsafe(exec_queue.put_nowait, {"type": "error", "message": str(e)})
        finally:
            loop.call_soon_threadsafe(exec_queue.put_nowait, None)

    import threading
    exec_thread = threading.Thread(target=_run_exec, daemon=True)
    exec_thread.start()

    last_input_time = time.time()

    try:
        while True:
            if time.time() - last_input_time > IDLE_TIMEOUT:
                await websocket.send_json({"type": "system", "message": "Idle timeout (5 min), disconnecting"})
                break

            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(websocket.receive_json()),
                        asyncio.create_task(exec_queue.get()),
                    ],
                    timeout=2.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()

                for task in done:
                    msg = task.result()
                    if msg is None:
                        return

                    if isinstance(msg, dict) and "type" in msg:
                        if msg["type"] == "input":
                            last_input_time = time.time()
                            if ws_process and ws_process.is_open():
                                ws_process.write_stdin(msg["data"])
                        elif msg["type"] == "resize":
                            if ws_process and ws_process.is_open():
                                try:
                                    ws_process.resize_pty(msg.get("cols", 120), msg.get("rows", 40))
                                except Exception:
                                    pass
                        elif msg["type"] in ("output", "system", "error"):
                            await websocket.send_json(msg)
                            if msg["type"] == "error":
                                return

            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                return

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws_process and ws_process.is_open():
            try:
                ws_process.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass

