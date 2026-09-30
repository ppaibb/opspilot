"""Cluster CRUD, scan, refresh, health, and audit routes."""

import datetime
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.filesystem import write_sensitive_text_file
from app.core.security import require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.cluster import Cluster

from ._router import router
from ._schemas import (
    ClusterCreate,
    ClusterResponse,
    ClusterUpdate,
    KubeScanRequest,
)
from ._shared import (
    parse_k8s_cpu,
    parse_k8s_memory_to_gib,
    _clear_cluster_client_cache,
    _diagnose_node,
    _diagnose_pod,
    _diagnose_workload,
)
from . import _shared

@router.get("", response_model=List[ClusterResponse])
def get_clusters(db: Session = Depends(get_db)):
    """返回已经完成真实 Kubeconfig 纳管的 K8s 集群列表。

    历史版本曾在数据库中写入演示集群。这里以 kubeconfig 文件是否存在作为
    真实纳管边界，避免前端展示没有真实连接凭据的 mock 集群。
    """
    clusters = db.query(Cluster).order_by(Cluster.id.desc()).all()
    res = []
    for cluster in clusters:
        kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster.cluster_id}.yaml")
        if os.path.exists(kc_path):
            if not getattr(cluster, "created_at", None):
                try:
                    mtime = os.path.getmtime(kc_path)
                    cluster.created_at = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    cluster.created_at = ""
            res.append(cluster)
    return res

@router.post("/scan")
def scan_kubernetes_cluster(payload: KubeScanRequest):
    """基于真实的 Kubeconfig 探测集群核心水位配置"""
    if not payload.kubeconfig_yaml or len(payload.kubeconfig_yaml) < 10:
        raise HTTPException(status_code=400, detail="非法的 Kubeconfig 文本结构")

    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload.kubeconfig_yaml)

        # 扫描阶段还没有 cluster_id，必须直接基于临时 kubeconfig 创建独立客户端，
        # 不能进入集群缓存，避免未定义 cluster_id 和临时凭据残留。
        api_client = config.new_client_from_config(path)
        v1 = client.CoreV1Api(api_client=api_client)
        version_api = client.VersionApi(api_client=api_client)

        # 抓取基本节点骨架
        nodes = v1.list_node(_request_timeout=5).items
        version_info = version_api.get_code()

        total_cpu = 0.0
        total_mem = 0.0
        total_pods = 0

        for n in nodes:
            cap = n.status.capacity or {}
            cpu_val = cap.get("cpu", "0")
            mem_val = cap.get("memory", "0Ki")
            pods_val = cap.get("pods", "0")

            # CPU 解析
            total_cpu += parse_k8s_cpu(cpu_val)

            # Pod 容量解析
            try:
                total_pods += int(pods_val)
            except ValueError:
                pass

            # 内存解析 (Gi)
            total_mem += parse_k8s_memory_to_gib(mem_val)

        # 计算当前全生命周期存活的 Pod 载荷
        running_pods = len(v1.list_pod_for_all_namespaces(_request_timeout=5).items)

        return {
            "status": "success",
            "provider": "on-premise",
            "version": version_info.git_version,
            "node_count": len(nodes),
            "pod_capacity": total_pods,
            "pod_usage": running_pods,
            "cpu_cores": int(round(total_cpu)),
            "mem_gb": int(round(total_mem))
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"TLS/API 拒接连接: 可能是秘钥作废或网络阻断。原因: {str(e)}")
    finally:
        if os.path.exists(path):
            os.remove(path)

@router.post(
    "",
    response_model=ClusterResponse,
    dependencies=[Depends(require_permissions("action.assets.clusters.manage"))],
)
def create_cluster(cluster: ClusterCreate, db: Session = Depends(get_db)):
    """联邦纳管新 K8s 集群并将 Kubeconfig 物理落库隔离"""
    cid = f"c-{str(uuid.uuid4())[:8]}"

    # Kubeconfig 安全落盘
    if cluster.kubeconfig_yaml:
        kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cid}.yaml")
        write_sensitive_text_file(kc_path, cluster.kubeconfig_yaml)

    new_cluster = Cluster(
        cluster_id=cid,
        name=cluster.name,
        provider=cluster.provider,
        version=cluster.version,
        node_count=cluster.node_count,
        pod_capacity=cluster.pod_capacity,
        pod_usage=cluster.pod_usage,
        cpu_cores=cluster.cpu_cores,
        mem_gb=cluster.mem_gb,
        status="running",
        kubeconfig_ref=cluster.kubeconfig_ref,
        created_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    db.add(new_cluster)
    db.commit()
    db.refresh(new_cluster)
    return new_cluster

@router.put(
    "/{cluster_id}",
    response_model=ClusterResponse,
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "edit")),
    ],
)
def update_cluster(cluster_id: str, payload: ClusterUpdate, db: Session = Depends(get_db)):
    """更新集群基础信息（名称 / 提供商）。此前前端编辑调 PUT 但后端缺失该路由（405）。"""
    cluster = db.query(Cluster).filter(Cluster.cluster_id == cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="集群对象游离：未在系统中找到记录")
    cluster.name = payload.name.strip()
    if payload.provider.strip():
        cluster.provider = payload.provider.strip()
    db.commit()
    db.refresh(cluster)
    return cluster

@router.post(
    "/{cluster_id}/refresh",
    response_model=ClusterResponse,
    dependencies=[Depends(require_permissions("action.assets.clusters.manage"))],
)
def refresh_cluster(cluster_id: str, db: Session = Depends(get_db)):
    """基于已落盘的 kubeconfig 重新探测集群，刷新版本/节点/水位等静态快照。

    纳管时写入的数字是瞬时快照，随时间漂移；本接口用与 /scan 相同的探测逻辑
    原地更新记录，供详情页「刷新同步」按钮调用。
    """
    cluster = db.query(Cluster).filter(Cluster.cluster_id == cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="集群对象游离：未在系统中找到记录")

    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        raise HTTPException(status_code=404, detail="kubeconfig 文件缺失，无法刷新（请解除后重新纳管）")

    try:
        api_client = config.new_client_from_config(kc_path)
        v1 = client.CoreV1Api(api_client=api_client)
        version_api = client.VersionApi(api_client=api_client)

        nodes = v1.list_node(_request_timeout=8).items
        version_info = version_api.get_code()

        total_cpu = 0.0
        total_mem = 0.0
        total_pods = 0
        for n in nodes:
            cap = n.status.capacity or {}
            total_cpu += parse_k8s_cpu(cap.get("cpu", "0"))
            try:
                total_pods += int(cap.get("pods", "0"))
            except ValueError:
                pass
            total_mem += parse_k8s_memory_to_gib(cap.get("memory", "0Ki"))
        running_pods = len(v1.list_pod_for_all_namespaces(_request_timeout=8).items)

        cluster.version = version_info.git_version or cluster.version
        cluster.node_count = len(nodes)
        cluster.pod_capacity = total_pods
        cluster.pod_usage = running_pods
        cluster.cpu_cores = int(round(total_cpu))
        cluster.mem_gb = int(round(total_mem))
        cluster.status = "running"
        db.commit()
        db.refresh(cluster)
        return cluster
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # 探测失败时把状态标记为 disconnected，保留旧快照供参考。
        cluster.status = "disconnected"
        db.commit()
        raise HTTPException(status_code=400, detail=f"刷新失败（集群不可达或凭据失效）: {str(e)[:200]}")

@router.delete(
    "/{cluster_id}",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "delete")),
    ],
)
def delete_cluster(cluster_id: str, db: Session = Depends(get_db)):
    """卸载并注销集群资源"""
    cluster = db.query(Cluster).filter(Cluster.cluster_id == cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="集群对象游离：未在系统中找到记录")

    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if os.path.exists(kc_path):
        os.remove(kc_path)
    _clear_cluster_client_cache(cluster_id)

    # 停止该集群的所有 informer
    try:
        from app.services.k8s_informer import InformerManager
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(InformerManager.get_instance().remove_cluster(cluster_id))
    except Exception:
        pass

    db.delete(cluster)
    db.commit()
    return {"message": "success"}

@router.get("/{cluster_id}/health-summary")
def get_cluster_health_summary(cluster_id: str):
    """集群健康总览。

    一屏聚合 API 连通性、节点就绪、工作负载未就绪、Pod 异常、近期 Warning Events
    等常见运维信号，作为进入集群后的第一张信息看板。

    安全边界：
    - 仅读取 K8s API，不执行任何 kubectl apply / delete / exec；
    - 不返回 Secret 内容、kubeconfig、token 等敏感字段；
    - 聚合出错时降级为 `api_reachable=False` + 错误摘要，不抛 500。
    """
    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        raise HTTPException(status_code=404, detail="该集群未找到真实 Kubeconfig，请重新纳管后再查看总览")

    def _load_summary() -> dict[str, Any]:
        # 逐块 best-effort，每个分片失败都不应让整体 503。
        issues: list[dict[str, str]] = []  # [{level, message}]

        started_at = time.perf_counter()
        try:
            api_client = _shared._get_api_client(cluster_id)
            core_v1 = client.CoreV1Api(api_client=api_client)
            core_v1.get_api_resources(_request_timeout=4)
            api_reachable = True
            api_latency_ms = int((time.perf_counter() - started_at) * 1000)
            api_error = ""
        except ApiException as exc:
            return {
                "api_reachable": False,
                "api_latency_ms": 0,
                "api_error": f"{exc.status or ''} {exc.reason or ''}".strip() or "K8s API 无响应",
                "node_stats": {"total": 0, "ready": 0, "not_ready": 0, "unschedulable": 0},
                "workload_stats": {
                    "total": 0,
                    "degraded": 0,
                    "deployments": {"total": 0, "degraded": 0},
                    "statefulsets": {"total": 0, "degraded": 0},
                    "daemonsets": {"total": 0, "degraded": 0},
                },
                "pod_stats": {"total": 0, "running": 0, "abnormal": 0, "pending": 0, "crash_loop": 0},
                "namespace_count": 0,
                "recent_warning_events": [],
                "top_issues": [],
                "issues": [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "api_reachable": False,
                "api_latency_ms": 0,
                "api_error": f"K8s API 连接失败: {exc}",
                "node_stats": {"total": 0, "ready": 0, "not_ready": 0, "unschedulable": 0},
                "workload_stats": {
                    "total": 0,
                    "degraded": 0,
                    "deployments": {"total": 0, "degraded": 0},
                    "statefulsets": {"total": 0, "degraded": 0},
                    "daemonsets": {"total": 0, "degraded": 0},
                },
                "pod_stats": {"total": 0, "running": 0, "abnormal": 0, "pending": 0, "crash_loop": 0},
                "namespace_count": 0,
                "recent_warning_events": [],
                "top_issues": [],
                "issues": [],
            }

        apps_v1 = client.AppsV1Api(api_client=api_client)

        def _list_nodes():
            return core_v1.list_node(_request_timeout=6).items

        def _list_namespaces():
            return core_v1.list_namespace(_request_timeout=6).items

        def _list_pods():
            return core_v1.list_pod_for_all_namespaces(_request_timeout=8).items

        def _list_deployments():
            return apps_v1.list_deployment_for_all_namespaces(_request_timeout=8).items

        def _list_stateful_sets():
            return apps_v1.list_stateful_set_for_all_namespaces(_request_timeout=8).items

        def _list_daemon_sets():
            return apps_v1.list_daemon_set_for_all_namespaces(_request_timeout=8).items

        def _list_warning_events():
            # Warning 事件只取最近 50 条，后端再排序取 10 条给前端。
            return core_v1.list_event_for_all_namespaces(
                field_selector="type=Warning",
                limit=50,
                _request_timeout=6,
            ).items

        with ThreadPoolExecutor(max_workers=7) as executor:
            futures = {
                "nodes": executor.submit(_list_nodes),
                "namespaces": executor.submit(_list_namespaces),
                "pods": executor.submit(_list_pods),
                "deployments": executor.submit(_list_deployments),
                "statefulsets": executor.submit(_list_stateful_sets),
                "daemonsets": executor.submit(_list_daemon_sets),
                "events": executor.submit(_list_warning_events),
            }
            results: dict[str, Any] = {}
            for key, future in futures.items():
                try:
                    results[key] = future.result()
                except Exception as exc:  # noqa: BLE001
                    results[key] = None
                    issues.append({"level": "warning", "message": f"读取 {key} 失败: {exc}"})

        nodes = results.get("nodes") or []
        namespaces = results.get("namespaces") or []
        pods = results.get("pods") or []
        deployments = results.get("deployments") or []
        statefulsets = results.get("statefulsets") or []
        daemonsets = results.get("daemonsets") or []
        events = results.get("events") or []

        # ---- 节点统计 ----
        ready_nodes = 0
        not_ready_nodes = 0
        unschedulable = 0
        for n in nodes:
            if getattr(getattr(n, "spec", None), "unschedulable", False):
                unschedulable += 1
            is_ready = False
            for cond in getattr(n.status, "conditions", None) or []:
                if cond.type == "Ready":
                    is_ready = cond.status == "True"
                    break
            if is_ready:
                ready_nodes += 1
            else:
                not_ready_nodes += 1

        # ---- 工作负载统计 ----
        def _count_workload(rows: list[Any], get_ready, get_desired) -> tuple[int, int]:
            total = len(rows)
            degraded = 0
            for row in rows:
                ready = int(get_ready(row) or 0)
                desired = int(get_desired(row) or 0)
                if desired > 0 and ready < desired:
                    degraded += 1
            return total, degraded

        dep_total, dep_degraded = _count_workload(
            deployments,
            lambda r: getattr(r.status, "ready_replicas", 0),
            lambda r: getattr(r.spec, "replicas", 0),
        )
        sts_total, sts_degraded = _count_workload(
            statefulsets,
            lambda r: getattr(r.status, "ready_replicas", 0),
            lambda r: getattr(r.spec, "replicas", 0),
        )
        ds_total, ds_degraded = _count_workload(
            daemonsets,
            lambda r: getattr(r.status, "number_ready", 0),
            lambda r: getattr(r.status, "desired_number_scheduled", 0),
        )

        # ---- Pod 统计 + Top Issues ----
        running_pods = 0
        pending_pods = 0
        crash_loop_pods = 0
        abnormal_pods = 0
        pod_top_issues: list[dict[str, Any]] = []
        for p in pods:
            phase = getattr(p.status, "phase", "") or "Unknown"
            if phase == "Running":
                running_pods += 1
            elif phase == "Pending":
                pending_pods += 1
                abnormal_pods += 1
            elif phase in ("Failed", "Unknown"):
                abnormal_pods += 1

            diag = _diagnose_pod(p)
            if diag.get("reason") == "容器反复崩溃":
                crash_loop_pods += 1
            level = diag.get("level") or ""
            if level in ("warning", "critical") and len(pod_top_issues) < 20:
                pod_top_issues.append({
                    "kind": "Pod",
                    "namespace": p.metadata.namespace,
                    "name": p.metadata.name,
                    "level": level,
                    "reason": diag.get("reason", ""),
                    "detail": diag.get("detail", ""),
                    "suggestion": diag.get("suggestion", ""),
                })

        # ---- 工作负载异常 TopIssues ----
        def _collect_workload_issues(rows: list[Any], kind: str, get_ready, get_desired) -> None:
            for row in rows:
                ready = int(get_ready(row) or 0)
                desired = int(get_desired(row) or 0)
                diag = _diagnose_workload(kind, row, ready_replicas=ready, desired_replicas=desired)
                level = diag.get("level") or ""
                if level in ("warning", "critical"):
                    pod_top_issues.append({
                        "kind": kind,
                        "namespace": row.metadata.namespace,
                        "name": row.metadata.name,
                        "level": level,
                        "reason": diag.get("reason", ""),
                        "detail": diag.get("detail", ""),
                        "suggestion": diag.get("suggestion", ""),
                    })

        _collect_workload_issues(
            deployments,
            "Deployment",
            lambda r: getattr(r.status, "ready_replicas", 0),
            lambda r: getattr(r.spec, "replicas", 0),
        )
        _collect_workload_issues(
            statefulsets,
            "StatefulSet",
            lambda r: getattr(r.status, "ready_replicas", 0),
            lambda r: getattr(r.spec, "replicas", 0),
        )
        _collect_workload_issues(
            daemonsets,
            "DaemonSet",
            lambda r: getattr(r.status, "number_ready", 0),
            lambda r: getattr(r.status, "desired_number_scheduled", 0),
        )

        # ---- 节点异常 ----
        for n in nodes:
            diag = _diagnose_node(n)
            level = diag.get("level") or ""
            if level in ("warning", "critical"):
                pod_top_issues.append({
                    "kind": "Node",
                    "namespace": "",
                    "name": n.metadata.name,
                    "level": level,
                    "reason": diag.get("reason", ""),
                    "detail": diag.get("detail", ""),
                    "suggestion": diag.get("suggestion", ""),
                })

        # 按严重级别和 kind 排序（critical 在前，Node 问题优先）。
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        kind_rank = {"Node": 0, "Deployment": 1, "StatefulSet": 2, "DaemonSet": 3, "Pod": 4}
        pod_top_issues.sort(key=lambda x: (severity_rank.get(x["level"], 3), kind_rank.get(x["kind"], 9), x["namespace"], x["name"]))
        top_issues = pod_top_issues[:10]

        # ---- 最近 Warning Events（按 lastTimestamp 倒序） ----
        def _event_time(e: Any):
            return e.last_timestamp or e.event_time or e.creation_timestamp or datetime.datetime.min

        events_sorted = sorted(events, key=_event_time, reverse=True)
        recent_warning_events = []
        for e in events_sorted[:10]:
            involved = getattr(e, "involved_object", None)
            recent_warning_events.append({
                "type": e.type,
                "reason": e.reason or "",
                "message": (e.message or "")[:400],
                "count": e.count or 1,
                "kind": getattr(involved, "kind", "") if involved else "",
                "namespace": getattr(involved, "namespace", "") if involved else "",
                "name": getattr(involved, "name", "") if involved else "",
                "last_timestamp": str(e.last_timestamp) if e.last_timestamp else (
                    str(e.event_time) if e.event_time else ""
                ),
            })

        return {
            "api_reachable": api_reachable,
            "api_latency_ms": api_latency_ms,
            "api_error": api_error,
            "node_stats": {
                "total": len(nodes),
                "ready": ready_nodes,
                "not_ready": not_ready_nodes,
                "unschedulable": unschedulable,
            },
            "workload_stats": {
                "total": dep_total + sts_total + ds_total,
                "degraded": dep_degraded + sts_degraded + ds_degraded,
                "deployments": {"total": dep_total, "degraded": dep_degraded},
                "statefulsets": {"total": sts_total, "degraded": sts_degraded},
                "daemonsets": {"total": ds_total, "degraded": ds_degraded},
            },
            "pod_stats": {
                "total": len(pods),
                "running": running_pods,
                "abnormal": abnormal_pods,
                "pending": pending_pods,
                "crash_loop": crash_loop_pods,
            },
            "namespace_count": len(namespaces),
            "recent_warning_events": recent_warning_events,
            "top_issues": top_issues,
            "issues": issues,
        }

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "health-summary",
            ("_all",),
            _load_summary,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"K8s 集群总览查询失败: {exc}")

@router.get("/{cluster_id}/components/health")
def get_cluster_components_health(cluster_id: str):
    """检查集群核心组件健康状态（kube-system Pod + componentstatuses）。"""
    api_client = _shared._get_api_client(cluster_id)

    def _load():
        v1 = client.CoreV1Api(api_client=api_client)
        components = []

        # 检查 kube-system 核心 Pod
        core_names = ["apiserver", "etcd", "scheduler", "controller-manager", "kube-proxy", "coredns"]
        try:
            pods = v1.list_namespaced_pod(namespace="kube-system", _request_timeout=6).items
            for target in core_names:
                matched = [p for p in pods if target in p.metadata.name]
                if matched:
                    for p in matched[:2]:
                        ready = any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))
                        components.append({
                            "name": p.metadata.name,
                            "component": target,
                            "status": "healthy" if ready and p.status.phase == "Running" else "unhealthy",
                            "phase": p.status.phase,
                        })
                else:
                    components.append({"name": target, "component": target, "status": "not_found", "phase": "-"})
        except Exception:
            pass

        # 兼容老集群 componentstatuses API
        try:
            cs_list = v1.list_component_status(_request_timeout=5).items
            for cs in cs_list:
                healthy = all(c.status == "True" for c in (cs.conditions or []))
                if not any(c["component"] == cs.metadata.name for c in components):
                    components.append({
                        "name": cs.metadata.name,
                        "component": cs.metadata.name,
                        "status": "healthy" if healthy else "unhealthy",
                        "phase": "OK" if healthy else "Degraded",
                    })
        except Exception:
            pass

        return components

    try:
        return _shared._get_or_set_k8s_read_cache(cluster_id, "components_health", ("_all",), _load, ttl_seconds=15)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取组件健康失败: {e}")

@router.get("/{cluster_id}/audit-logs")
def get_cluster_audit_logs(
    cluster_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """获取该集群相关的操作审计日志。"""
    from app.models.audit import AuditLog
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.resource.contains(f"/clusters/{cluster_id}"))
        .order_by(AuditLog.time.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": log.id,
            "time": log.time.isoformat() if log.time else "",
            "user": log.user,
            "action": log.action,
            "resource": log.resource,
            "result": log.result,
            "risk_level": log.risk_level or "low",
        }
        for log in logs
    ]

