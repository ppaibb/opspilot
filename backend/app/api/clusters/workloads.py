"""Workload list/ops, rollout, cronjob, and HPA routes."""

import datetime
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query
from kubernetes import client
from kubernetes.client.rest import ApiException

from app.core.security import require_permissions, require_risk_ack

from ._router import router
from ._schemas import (
    RolloutUndoRequest,
    ScaleRequest,
    SetImageRequest,
)
from ._shared import (
    _labels_to_selector,
    _raise_k8s_http_exception,
)
from . import _shared

@router.get("/{cluster_id}/workloads")
def get_cluster_workloads(cluster_id: str, namespace: str = Query("_all", description="namespace, _all for all")):
    """[实时拉取] 获取 Deployment/StatefulSet/DaemonSet 工作负载状态，支持 namespace 过滤。"""
    kc_path = os.path.join(_shared.KUBECONFIG_DIR, f"{cluster_id}.yaml")
    if not os.path.exists(kc_path):
        raise HTTPException(status_code=404, detail="该集群未找到真实 Kubeconfig，请重新纳管后再查看工作负载")

    def _load_workloads() -> list[dict[str, Any]]:
        api_client = _shared._get_api_client(cluster_id)
        apps_v1 = client.AppsV1Api(api_client=api_client)
        batch_v1 = client.BatchV1Api(api_client=api_client)

        def _list_deployments():
            if namespace and namespace != "_all":
                return apps_v1.list_namespaced_deployment(namespace=namespace, _request_timeout=6).items
            return apps_v1.list_deployment_for_all_namespaces(_request_timeout=6).items

        def _list_stateful_sets():
            if namespace and namespace != "_all":
                return apps_v1.list_namespaced_stateful_set(namespace=namespace, _request_timeout=6).items
            return apps_v1.list_stateful_set_for_all_namespaces(_request_timeout=6).items

        def _list_daemon_sets():
            if namespace and namespace != "_all":
                return apps_v1.list_namespaced_daemon_set(namespace=namespace, _request_timeout=6).items
            return apps_v1.list_daemon_set_for_all_namespaces(_request_timeout=6).items

        def _list_replica_sets():
            if namespace and namespace != "_all":
                return apps_v1.list_namespaced_replica_set(namespace=namespace, _request_timeout=6).items
            return apps_v1.list_replica_set_for_all_namespaces(_request_timeout=6).items

        def _list_jobs():
            if namespace and namespace != "_all":
                return batch_v1.list_namespaced_job(namespace=namespace, _request_timeout=6).items
            return batch_v1.list_job_for_all_namespaces(_request_timeout=6).items

        def _list_cron_jobs():
            if namespace and namespace != "_all":
                return batch_v1.list_namespaced_cron_job(namespace=namespace, _request_timeout=6).items
            return batch_v1.list_cron_job_for_all_namespaces(_request_timeout=6).items

        from app.services.k8s_summarizers import summarize_workload
        with ThreadPoolExecutor(max_workers=6) as executor:
            dep_future = executor.submit(_list_deployments)
            sts_future = executor.submit(_list_stateful_sets)
            ds_future = executor.submit(_list_daemon_sets)
            rs_future = executor.submit(_list_replica_sets)
            job_future = executor.submit(_list_jobs)
            cj_future = executor.submit(_list_cron_jobs)
            
            deps = dep_future.result()
            stss = sts_future.result()
            dss = ds_future.result()
            rss = rs_future.result()
            jobs = job_future.result()
            cjs = cj_future.result()

        result = []
        for d in deps: result.append(summarize_workload("Deployment", d))
        for s in stss: result.append(summarize_workload("StatefulSet", s))
        for ds in dss: result.append(summarize_workload("DaemonSet", ds))
        for rs in rss: result.append(summarize_workload("ReplicaSet", rs))
        for j in jobs: result.append(summarize_workload("Job", j))
        for c in cjs: result.append(summarize_workload("CronJob", c))

        # 按 namespace+name 排序并取前 100 个
        result.sort(key=lambda x: (x["namespace"], x["name"]))
        return result[:100]

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "workloads",
            (namespace or "_all",),
            _load_workloads,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"K8s API 工作负载查询失败: {str(e)}")

@router.patch(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}/scale",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "edit")),
    ],
)
def scale_workload(cluster_id: str, kind: str, namespace: str, name: str, body: ScaleRequest):
    """Scale workload replicas. kind supports deployment, statefulset."""
    api_client = _shared._get_api_client(cluster_id)
    kind_lower = kind.lower()
    try:
        apps_v1 = client.AppsV1Api(api_client=api_client)
        if kind_lower == "deployment":
            dep = apps_v1.read_namespaced_deployment(name=name, namespace=namespace, _request_timeout=6)
            dep.spec.replicas = body.replicas
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=dep, _request_timeout=6)
        elif kind_lower == "statefulset":
            sts = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace, _request_timeout=6)
            sts.spec.replicas = body.replicas
            apps_v1.patch_namespaced_stateful_set(name=name, namespace=namespace, body=sts, _request_timeout=6)
        elif kind_lower == "daemonset":
            raise HTTPException(status_code=400, detail="DaemonSet does not support scaling")
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported workload type: {kind}")
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "kind": kind.capitalize(), "name": name, "replicas": body.replicas}
    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{kind}/{namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"Scale failed: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scale failed: {str(e)}")

@router.patch(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}/restart",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "restart")),
    ],
)
def restart_workload(cluster_id: str, kind: str, namespace: str, name: str):
    """Rollout restart workload (deployment, statefulset, daemonset)."""
    api_client = _shared._get_api_client(cluster_id)
    kind_lower = kind.lower()
    try:
        apps_v1 = client.AppsV1Api(api_client=api_client)
        now = datetime.datetime.utcnow().isoformat() + "Z"
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }

        if kind_lower == "deployment":
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body, _request_timeout=6)
        elif kind_lower == "statefulset":
            apps_v1.patch_namespaced_stateful_set(name=name, namespace=namespace, body=body, _request_timeout=6)
        elif kind_lower == "daemonset":
            apps_v1.patch_namespaced_daemon_set(name=name, namespace=namespace, body=body, _request_timeout=6)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported workload type for restart: {kind}")

        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "kind": kind.capitalize(), "name": name, "restartedAt": now}
    except HTTPException:
        raise
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{kind}/{namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"Restart failed: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart failed: {str(e)}")

SUPPORTED_ROLLOUT_KINDS = {"deployment", "statefulset", "daemonset"}

MAX_ROLLOUT_HISTORY = 20

def _read_workload(api_client: Any, kind_lower: str, namespace: str, name: str) -> Any:
    """按 kind 读取 workload。统一 404 / 502 错误形态。"""
    apps_v1 = client.AppsV1Api(api_client=api_client)
    try:
        if kind_lower == "deployment":
            return apps_v1.read_namespaced_deployment(name=name, namespace=namespace, _request_timeout=6)
        if kind_lower == "statefulset":
            return apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace, _request_timeout=6)
        if kind_lower == "daemonset":
            return apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace, _request_timeout=6)
        raise HTTPException(status_code=400, detail=f"不支持的工作负载类型: {kind_lower}")
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"{kind_lower}/{namespace}/{name} 不存在")
        raise _raise_k8s_http_exception(exc, "读取工作负载")

def _rollout_status_summary(kind_lower: str, workload: Any) -> dict[str, Any]:
    """把 rollout 状态归一化为前端展示字段。"""
    metadata = getattr(workload, "metadata", None)
    spec = getattr(workload, "spec", None)
    status = getattr(workload, "status", None)
    observed_generation = int(getattr(status, "observed_generation", 0) or 0)
    generation = int(getattr(metadata, "generation", 0) or 0)

    conditions = [
        {
            "type": getattr(c, "type", ""),
            "status": getattr(c, "status", ""),
            "reason": getattr(c, "reason", "") or "",
            "message": (getattr(c, "message", "") or "")[:300],
            "last_update": str(getattr(c, "last_update_time", "") or ""),
        }
        for c in (getattr(status, "conditions", None) or [])
    ]

    summary: dict[str, Any] = {
        "kind": kind_lower.capitalize(),
        "namespace": metadata.namespace if metadata else "",
        "name": metadata.name if metadata else "",
        "generation": generation,
        "observed_generation": observed_generation,
        "progressing": observed_generation < generation,
        "conditions": conditions,
    }

    if kind_lower in {"deployment", "statefulset"}:
        desired = int(getattr(spec, "replicas", 0) or 0)
        ready = int(getattr(status, "ready_replicas", 0) or 0)
        updated = int(getattr(status, "updated_replicas", 0) or 0)
        available = int(getattr(status, "available_replicas", 0) or 0)
        summary.update({
            "desired_replicas": desired,
            "ready_replicas": ready,
            "updated_replicas": updated,
            "available_replicas": available,
            "unavailable_replicas": max(desired - available, 0),
        })
        if desired == 0:
            summary["rollout_status"] = "stopped"
            summary["rollout_message"] = "期望副本为 0，当前无实例"
        elif observed_generation < generation:
            summary["rollout_status"] = "progressing"
            summary["rollout_message"] = "控制器尚未观察到最新期望，滚动仍在进行"
        elif ready < desired:
            summary["rollout_status"] = "progressing"
            summary["rollout_message"] = f"Ready {ready}/{desired}，滚动更新未完成"
        else:
            summary["rollout_status"] = "completed"
            summary["rollout_message"] = "所有副本已滚动到最新版本"
    else:
        # DaemonSet 用 desired_number_scheduled / number_ready
        desired = int(getattr(status, "desired_number_scheduled", 0) or 0)
        ready = int(getattr(status, "number_ready", 0) or 0)
        updated = int(getattr(status, "updated_number_scheduled", 0) or 0)
        available = int(getattr(status, "number_available", 0) or 0)
        summary.update({
            "desired_replicas": desired,
            "ready_replicas": ready,
            "updated_replicas": updated,
            "available_replicas": available,
            "unavailable_replicas": max(desired - available, 0),
        })
        if desired == 0:
            summary["rollout_status"] = "stopped"
            summary["rollout_message"] = "节点上没有 DaemonSet 目标"
        elif observed_generation < generation or updated < desired or ready < desired:
            summary["rollout_status"] = "progressing"
            summary["rollout_message"] = f"Ready {ready}/{desired}，Updated {updated}/{desired}"
        else:
            summary["rollout_status"] = "completed"
            summary["rollout_message"] = "所有节点已就绪"

    return summary

def _collect_deployment_history(api_client: Any, deployment: Any) -> list[dict[str, Any]]:
    """基于 ReplicaSet 汇总 Deployment revision history。

    匹配规则：
    - 同 namespace 下 ReplicaSet，且 ownerReferences 指向当前 Deployment；
    - revision 取 `deployment.kubernetes.io/revision` annotation；
    - 按 revision 降序，限制前 MAX_ROLLOUT_HISTORY 条。
    """
    apps_v1 = client.AppsV1Api(api_client=api_client)
    namespace = deployment.metadata.namespace
    name = deployment.metadata.name
    uid = deployment.metadata.uid

    rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, _request_timeout=6).items
    rows: list[dict[str, Any]] = []
    for rs in rs_list:
        owners = [o for o in (rs.metadata.owner_references or []) if getattr(o, "kind", "") == "Deployment"]
        if not owners or all(getattr(o, "uid", None) != uid for o in owners):
            if all(getattr(o, "name", "") != name for o in owners):
                continue
        annotations = rs.metadata.annotations or {}
        revision_raw = annotations.get("deployment.kubernetes.io/revision", "")
        try:
            revision = int(revision_raw)
        except (TypeError, ValueError):
            revision = 0
        if revision <= 0:
            continue

        containers = getattr(rs.spec.template.spec, "containers", None) or []
        images = [
            {"container": c.name, "image": c.image}
            for c in containers
        ]
        change_cause = annotations.get("kubernetes.io/change-cause", "") or ""
        rows.append({
            "revision": revision,
            "name": rs.metadata.name,
            "creation_timestamp": str(rs.metadata.creation_timestamp or ""),
            "images": images,
            "ready": int(getattr(rs.status, "ready_replicas", 0) or 0),
            "replicas": int(getattr(rs.status, "replicas", 0) or 0),
            "change_cause": change_cause,
        })

    rows.sort(key=lambda r: r["revision"], reverse=True)
    return rows[:MAX_ROLLOUT_HISTORY]

def _collect_controller_revision_history(
    api_client: Any,
    namespace: str,
    selector_labels: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """读取 StatefulSet / DaemonSet 的 ControllerRevision 作为历史。"""
    if not selector_labels:
        return []
    apps_v1 = client.AppsV1Api(api_client=api_client)
    selector = _labels_to_selector(selector_labels)
    if not selector:
        return []
    revisions = apps_v1.list_namespaced_controller_revision(
        namespace=namespace,
        label_selector=selector,
        _request_timeout=6,
    ).items
    rows: list[dict[str, Any]] = []
    for rev in revisions:
        annotations = rev.metadata.annotations or {}
        rows.append({
            "revision": int(getattr(rev, "revision", 0) or 0),
            "name": rev.metadata.name,
            "creation_timestamp": str(rev.metadata.creation_timestamp or ""),
            "change_cause": annotations.get("kubernetes.io/change-cause", "") or "",
            # ControllerRevision 本身只是 Raw data，镜像信息没有解析；留给未来扩展。
            "images": [],
        })
    rows.sort(key=lambda r: r["revision"], reverse=True)
    return rows[:MAX_ROLLOUT_HISTORY]

@router.get("/{cluster_id}/workloads/{kind}/{namespace}/{name}/rollout-status")
def get_workload_rollout_status(cluster_id: str, kind: str, namespace: str, name: str):
    """返回工作负载滚动发布状态。用于发布页展示 rollout 进度。"""
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROLLOUT_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的 rollout 类型: {kind}")
    api_client = _shared._get_api_client(cluster_id)
    workload = _read_workload(api_client, kind_lower, namespace, name)
    return _rollout_status_summary(kind_lower, workload)

@router.get("/{cluster_id}/workloads/{kind}/{namespace}/{name}/rollout-history")
def get_workload_rollout_history(cluster_id: str, kind: str, namespace: str, name: str):
    """列出工作负载滚动历史 revision，最多 20 条。"""
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROLLOUT_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的 rollout 类型: {kind}")
    api_client = _shared._get_api_client(cluster_id)
    workload = _read_workload(api_client, kind_lower, namespace, name)
    if kind_lower == "deployment":
        history = _collect_deployment_history(api_client, workload)
    else:
        selector = getattr(workload.spec.selector, "match_labels", None) if getattr(workload.spec, "selector", None) else None
        history = _collect_controller_revision_history(api_client, namespace, selector)
    return {"kind": kind.capitalize(), "namespace": namespace, "name": name, "history": history}

def _validate_image_ref(image: str) -> str:
    text = str(image or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="镜像不能为空")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="镜像地址不能超过 500 字符")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise HTTPException(status_code=400, detail="镜像地址包含非法字符")
    if " " in text:
        raise HTTPException(status_code=400, detail="镜像地址不能包含空格")
    return text

@router.patch(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}/set-image",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "set-image")),
    ],
)
def set_workload_image(
    cluster_id: str,
    kind: str,
    namespace: str,
    name: str,
    payload: SetImageRequest,
):
    """更新工作负载容器镜像，相当于 `kubectl set image`。

    安全边界：
    - 只允许修改已有容器的 image 字段，不允许变更容器名或新增容器；
    - 通过 strategic merge patch 下发，不替换整个 spec；
    - 写入 `kubernetes.io/change-cause` annotation 供 rollout history 展示。
    """
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROLLOUT_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的 rollout 类型: {kind}")
    if not payload.images:
        raise HTTPException(status_code=400, detail="未提供镜像变更项")

    api_client = _shared._get_api_client(cluster_id)
    workload = _read_workload(api_client, kind_lower, namespace, name)
    existing_containers = {
        c.name: c
        for c in (getattr(workload.spec.template.spec, "containers", None) or [])
    }
    unknown = [item.name for item in payload.images if item.name not in existing_containers]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"容器不存在于当前工作负载: {', '.join(unknown[:5])}",
        )

    patched_containers = [
        {"name": item.name, "image": _validate_image_ref(item.image)}
        for item in payload.images
    ]

    change_cause = (payload.change_cause or "").strip()[:200]
    annotations: dict[str, str] = {}
    if change_cause:
        annotations["kubernetes.io/change-cause"] = change_cause

    body: dict[str, Any] = {
        "spec": {
            "template": {
                "spec": {"containers": patched_containers},
            },
        },
    }
    if annotations:
        body["metadata"] = {"annotations": annotations}

    apps_v1 = client.AppsV1Api(api_client=api_client)
    try:
        if kind_lower == "deployment":
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body, _request_timeout=6)
        elif kind_lower == "statefulset":
            apps_v1.patch_namespaced_stateful_set(name=name, namespace=namespace, body=body, _request_timeout=6)
        else:
            apps_v1.patch_namespaced_daemon_set(name=name, namespace=namespace, body=body, _request_timeout=6)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "更新工作负载镜像")

    _shared._clear_k8s_read_cache(cluster_id)
    return {
        "success": True,
        "kind": kind.capitalize(),
        "namespace": namespace,
        "name": name,
        "images": patched_containers,
    }

def _find_deployment_previous_revision(api_client: Any, deployment: Any) -> int:
    """找到 Deployment 上一轮有效 revision。"""
    history = _collect_deployment_history(api_client, deployment)
    current_revision = int(
        (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0) or 0
    )
    previous = [row for row in history if row["revision"] != current_revision]
    if not previous:
        raise HTTPException(status_code=400, detail="未找到可回滚的历史 revision")
    previous.sort(key=lambda r: r["revision"], reverse=True)
    return int(previous[0]["revision"])

@router.post(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}/rollout-undo",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "rollout-undo")),
    ],
)
def rollout_undo_workload(
    cluster_id: str,
    kind: str,
    namespace: str,
    name: str,
    payload: RolloutUndoRequest,
):
    """回滚工作负载到指定 revision，不传则回滚到上一次。

    架构：
    - Deployment：找到目标 revision 对应的 ReplicaSet，用其 `spec.template`
      构造 strategic merge patch 覆盖回 Deployment；
    - StatefulSet / DaemonSet：回滚走同 kind 的 ControllerRevision，通过
      `data.spec.template` 重新 patch 到工作负载。

    安全边界：
    - revision 必须是当前资源的历史 revision，任意 ID 会拒绝；
    - 仅 patch `spec.template`（含 annotations），不改 replicas、selector、strategy；
    - 写入 `kubernetes.io/change-cause` 供后续 rollout history 可追溯。
    """
    kind_lower = kind.lower()
    if kind_lower not in SUPPORTED_ROLLOUT_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的 rollout 类型: {kind}")

    api_client = _shared._get_api_client(cluster_id)
    workload = _read_workload(api_client, kind_lower, namespace, name)
    apps_v1 = client.AppsV1Api(api_client=api_client)

    if kind_lower == "deployment":
        target_revision = int(payload.revision) if payload.revision else _find_deployment_previous_revision(api_client, workload)
        # 找到 revision 对应的 ReplicaSet
        rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, _request_timeout=6).items
        target_rs = None
        for rs in rs_list:
            owners = [o for o in (rs.metadata.owner_references or []) if getattr(o, "kind", "") == "Deployment"]
            if not owners or all(getattr(o, "uid", None) != workload.metadata.uid for o in owners):
                continue
            annotations = rs.metadata.annotations or {}
            try:
                rs_rev = int(annotations.get("deployment.kubernetes.io/revision", 0))
            except (TypeError, ValueError):
                rs_rev = 0
            if rs_rev == target_revision:
                target_rs = rs
                break
        if target_rs is None:
            raise HTTPException(status_code=404, detail=f"未找到 revision={target_revision} 的历史版本")

        template = target_rs.spec.template
        # 把 API 对象序列化为 dict，方便直接用作 strategic merge patch 的 body。
        template_dict = api_client.sanitize_for_serialization(template)
        # 保留 kubectl 给 rollout undo 使用的 change-cause 约定，便于 history 追溯。
        metadata_annotations = {"kubernetes.io/change-cause": f"rollback to revision {target_revision}"}
        body: dict[str, Any] = {
            "metadata": {"annotations": metadata_annotations},
            "spec": {"template": template_dict},
        }
        try:
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body, _request_timeout=10)
        except ApiException as exc:
            raise _raise_k8s_http_exception(exc, "Deployment 回滚")
        _shared._clear_k8s_read_cache(cluster_id)
        return {
            "success": True,
            "kind": "Deployment",
            "namespace": namespace,
            "name": name,
            "revision": target_revision,
        }

    # StatefulSet / DaemonSet 基于 ControllerRevision
    selector_labels = getattr(workload.spec.selector, "match_labels", None) if getattr(workload.spec, "selector", None) else None
    if not selector_labels:
        raise HTTPException(status_code=400, detail="工作负载缺少 matchLabels，无法定位历史版本")

    selector = _labels_to_selector(selector_labels)
    revisions = apps_v1.list_namespaced_controller_revision(
        namespace=namespace,
        label_selector=selector,
        _request_timeout=6,
    ).items
    if not revisions:
        raise HTTPException(status_code=400, detail="未找到可回滚的 ControllerRevision")

    target_revision = int(payload.revision) if payload.revision else 0
    target_rev_obj = None
    if target_revision:
        for rev in revisions:
            if int(getattr(rev, "revision", 0) or 0) == target_revision:
                target_rev_obj = rev
                break
        if target_rev_obj is None:
            raise HTTPException(status_code=404, detail=f"未找到 revision={target_revision} 的历史版本")
    else:
        current_revision = int(getattr(workload.status, "current_revision", "") or 0) or int(
            getattr(workload.status, "update_revision", "") or 0
        )
        sorted_revs = sorted(revisions, key=lambda r: int(getattr(r, "revision", 0) or 0), reverse=True)
        for rev in sorted_revs:
            if int(getattr(rev, "revision", 0) or 0) != current_revision:
                target_rev_obj = rev
                target_revision = int(getattr(rev, "revision", 0) or 0)
                break
        if target_rev_obj is None:
            raise HTTPException(status_code=400, detail="没有可回滚的上一轮 ControllerRevision")

    raw_data = getattr(target_rev_obj, "data", None)
    if raw_data is None:
        raise HTTPException(status_code=400, detail="目标 ControllerRevision 缺少 data，无法回滚")

    raw_dict = raw_data if isinstance(raw_data, dict) else api_client.sanitize_for_serialization(raw_data)
    spec = (raw_dict or {}).get("spec") or {}
    template = spec.get("template") or {}
    if not template:
        raise HTTPException(status_code=400, detail="目标 ControllerRevision 缺少 template，无法回滚")

    body = {
        "metadata": {"annotations": {"kubernetes.io/change-cause": f"rollback to revision {target_revision}"}},
        "spec": {"template": template},
    }

    try:
        if kind_lower == "statefulset":
            apps_v1.patch_namespaced_stateful_set(name=name, namespace=namespace, body=body, _request_timeout=10)
        else:
            apps_v1.patch_namespaced_daemon_set(name=name, namespace=namespace, body=body, _request_timeout=10)
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, f"{kind} 回滚")

    _shared._clear_k8s_read_cache(cluster_id)
    return {
        "success": True,
        "kind": kind.capitalize(),
        "namespace": namespace,
        "name": name,
        "revision": target_revision,
    }

@router.patch(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}/pause",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "pause")),
    ],
)
def toggle_workload_pause(cluster_id: str, kind: str, namespace: str, name: str, body: dict):
    """Pause or resume workload (deployment only)."""
    api_client = _shared._get_api_client(cluster_id)
    kind_lower = kind.lower()
    paused = bool(body.get("paused", False))
    try:
        apps_v1 = client.AppsV1Api(api_client=api_client)
        patch_body = {"spec": {"paused": paused}}
        if kind_lower == "deployment":
            apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=patch_body, _request_timeout=6)
        else:
            raise HTTPException(status_code=400, detail="Only Deployment supports pause/resume.")
        _shared._clear_k8s_read_cache(cluster_id)
        return {"message": "Success"}
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{kind}/{namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"Pause/Resume failed: {e.reason}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pause/Resume failed: {str(e)}")

@router.delete(
    "/{cluster_id}/workloads/{kind}/{namespace}/{name}",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "delete")),
    ],
)
def delete_workload(cluster_id: str, kind: str, namespace: str, name: str):
    """Delete workload. kind supports deployment, statefulset, daemonset."""
    api_client = _shared._get_api_client(cluster_id)
    kind_lower = kind.lower()
    try:
        apps_v1 = client.AppsV1Api(api_client=api_client)
        if kind_lower == "deployment":
            apps_v1.delete_namespaced_deployment(name=name, namespace=namespace, _request_timeout=6)
        elif kind_lower == "statefulset":
            apps_v1.delete_namespaced_stateful_set(name=name, namespace=namespace, _request_timeout=6)
        elif kind_lower == "daemonset":
            apps_v1.delete_namespaced_daemon_set(name=name, namespace=namespace, _request_timeout=6)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported workload type: {kind}")
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"{kind} {name} deleted"}
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{kind}/{namespace}/{name} not found")
        raise HTTPException(status_code=502, detail=f"Delete failed: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")

@router.post(
    "/{cluster_id}/cronjobs/{namespace}/{name}/trigger",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "trigger_cronjob")),
    ],
)
def trigger_cronjob(cluster_id: str, namespace: str, name: str):
    """手动触发 CronJob（创建一个 Job）。"""
    api_client = _shared._get_api_client(cluster_id)
    batch_v1 = client.BatchV1Api(api_client=api_client)
    try:
        cj = batch_v1.read_namespaced_cron_job(name=name, namespace=namespace, _request_timeout=6)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"CronJob {namespace}/{name} 不存在") from exc
        raise _raise_k8s_http_exception(exc, "读取 CronJob")

    job_name = f"{name}-manual-{int(time.time())}"
    job_body = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name, namespace=namespace,
            labels={"cronjob-trigger": "manual", "cronjob-name": name},
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=cj.spec.job_template.spec,
    )
    try:
        batch_v1.create_namespaced_job(namespace=namespace, body=job_body, _request_timeout=6)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"已触发 Job: {job_name}", "job_name": job_name}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "创建 Job")

@router.patch(
    "/{cluster_id}/cronjobs/{namespace}/{name}/suspend",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "suspend_cronjob")),
    ],
)
def suspend_cronjob(cluster_id: str, namespace: str, name: str, suspend: bool = Query(...)):
    """暂停或恢复 CronJob。"""
    api_client = _shared._get_api_client(cluster_id)
    batch_v1 = client.BatchV1Api(api_client=api_client)
    try:
        batch_v1.patch_namespaced_cron_job(name=name, namespace=namespace, body={"spec": {"suspend": suspend}}, _request_timeout=6)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"CronJob {name} {'已暂停' if suspend else '已恢复'}"}
    except ApiException as exc:
        raise _raise_k8s_http_exception(exc, "暂停/恢复 CronJob")

@router.get("/{cluster_id}/cronjobs/{namespace}/{name}/jobs")
def list_cronjob_jobs(cluster_id: str, namespace: str, name: str):
    """获取 CronJob 关联的 Job 执行历史。"""
    api_client = _shared._get_api_client(cluster_id)
    batch_v1 = client.BatchV1Api(api_client=api_client)

    def _load():
        jobs = batch_v1.list_namespaced_job(namespace=namespace, _request_timeout=8).items
        related = []
        for job in jobs:
            owners = job.metadata.owner_references or []
            is_owned = any(o.name == name and o.kind == "CronJob" for o in owners)
            is_manual = (job.metadata.labels or {}).get("cronjob-name") == name
            if not is_owned and not is_manual:
                continue
            conditions = job.status.conditions or []
            status = "Running"
            if any(c.type == "Complete" and c.status == "True" for c in conditions):
                status = "Complete"
            elif any(c.type == "Failed" and c.status == "True" for c in conditions):
                status = "Failed"
            related.append({
                "name": job.metadata.name,
                "status": status,
                "start_time": job.status.start_time.isoformat() if job.status.start_time else None,
                "completion_time": job.status.completion_time.isoformat() if job.status.completion_time else None,
                "succeeded": job.status.succeeded or 0,
                "failed": job.status.failed or 0,
                "active": job.status.active or 0,
            })
        return sorted(related, key=lambda x: x["start_time"] or "", reverse=True)[:20]

    try:
        return _shared._get_or_set_k8s_read_cache(cluster_id, f"cj_jobs_{namespace}_{name}", (namespace, name), _load, ttl_seconds=5)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取 Job 历史失败: {e}")

@router.get("/{cluster_id}/hpa/{namespace}/{name}/status")
def get_hpa_status(cluster_id: str, namespace: str, name: str):
    """获取 HPA 当前指标状态。"""
    api_client = _shared._get_api_client(cluster_id)

    def _load():
        autoscaling_v2 = client.AutoscalingV2Api(api_client=api_client)
        try:
            hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name=name, namespace=namespace, _request_timeout=6)
        except ApiException:
            autoscaling_v1 = client.AutoscalingV1Api(api_client=api_client)
            hpa = autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(name=name, namespace=namespace, _request_timeout=6)

        current_metrics = []
        if hasattr(hpa.status, 'current_metrics') and hpa.status.current_metrics:
            for m in hpa.status.current_metrics:
                entry = {"type": m.type}
                if m.type == "Resource" and m.resource:
                    entry["name"] = m.resource.name
                    entry["current"] = m.resource.current.average_utilization if m.resource.current else None
                elif m.type == "Pods" and m.pods:
                    entry["name"] = m.pods.metric.name if m.pods.metric else "unknown"
                    entry["current"] = m.pods.current.average_value if m.pods.current else None
                current_metrics.append(entry)

        target_metrics = []
        if hasattr(hpa.spec, 'metrics') and hpa.spec.metrics:
            for m in hpa.spec.metrics:
                entry = {"type": m.type}
                if m.type == "Resource" and m.resource:
                    entry["name"] = m.resource.name
                    entry["target"] = m.resource.target.average_utilization if m.resource.target else None
                target_metrics.append(entry)

        return {
            "min_replicas": hpa.spec.min_replicas,
            "max_replicas": hpa.spec.max_replicas,
            "current_replicas": hpa.status.current_replicas,
            "desired_replicas": hpa.status.desired_replicas,
            "last_scale_time": hpa.status.last_scale_time.isoformat() if hpa.status.last_scale_time else None,
            "current_metrics": current_metrics,
            "target_metrics": target_metrics,
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message} for c in (hpa.status.conditions or [])],
        }

    try:
        return _shared._get_or_set_k8s_read_cache(cluster_id, f"hpa_status_{namespace}_{name}", (namespace, name), _load, ttl_seconds=5)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取 HPA 状态失败: {e}")

