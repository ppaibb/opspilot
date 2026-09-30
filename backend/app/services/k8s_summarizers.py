"""
K8s 资源摘要函数 — 供 API 列表端点和 Informer 缓存共用

将 K8s API 原始对象转换为前端列表表格所需的统一格式。
"""
from datetime import datetime, timezone
from typing import Any


def format_age(creation_timestamp) -> str:
    if not creation_timestamp:
        return "Unknown"
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


def pod_status(pod) -> str:
    phase = pod.status.phase or "Unknown"
    if phase == "Failed" and pod.status.reason:
        return f"Failed({pod.status.reason})"
    if pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or "Waiting"
                return reason
    return phase


def pod_ready(pod) -> str:
    if not pod.status.container_statuses:
        return "0/0"
    ready = sum(1 for cs in pod.status.container_statuses if cs.ready)
    total = len(pod.status.container_statuses)
    return f"{ready}/{total}"


def pod_restarts(pod) -> int:
    if not pod.status.container_statuses:
        return 0
    return sum(cs.restart_count or 0 for cs in pod.status.container_statuses)


def _empty_diagnosis() -> dict[str, str]:
    return {"level": "ok", "reason": "", "detail": "", "suggestion": ""}


def diagnose_pod(pod: Any) -> dict[str, str]:
    """基于 Pod 状态生成可读诊断（不读取敏感字段）。"""
    metadata = getattr(pod, "metadata", None)
    status = getattr(pod, "status", None)
    if getattr(metadata, "deletion_timestamp", None):
        return {"level": "warning", "reason": "正在终止", "detail": "Pod 已进入删除流程", "suggestion": "查看事件与节点状态"}

    phase = getattr(status, "phase", "") or "Unknown"
    reason = getattr(status, "reason", "") or ""
    container_statuses = getattr(status, "container_statuses", None) or []
    for cs in container_statuses:
        state = getattr(cs, "state", None)
        waiting = getattr(state, "waiting", None) if state else None
        terminated = getattr(state, "terminated", None) if state else None
        restart_count = int(getattr(cs, "restart_count", 0) or 0)
        if waiting:
            wait_reason = getattr(waiting, "reason", "") or "Waiting"
            message = getattr(waiting, "message", "") or ""
            if wait_reason in ("ImagePullBackOff", "ErrImagePull"):
                return {"level": "warning", "reason": "镜像拉取失败", "detail": message or wait_reason, "suggestion": "检查镜像地址与凭证"}
            if wait_reason == "CrashLoopBackOff":
                return {"level": "critical", "reason": "容器反复崩溃", "detail": message or f"已重启 {restart_count} 次", "suggestion": "查看容器日志"}
            if wait_reason in ("CreateContainerConfigError", "CreateContainerError"):
                return {"level": "critical", "reason": "容器创建失败", "detail": message or wait_reason, "suggestion": "检查 ConfigMap/Secret/挂载"}
        if terminated:
            term_reason = getattr(terminated, "reason", "") or "Terminated"
            if term_reason == "OOMKilled":
                return {"level": "critical", "reason": "内存超限退出", "detail": "OOMKilled", "suggestion": "扩容或优化内存"}

    if phase == "Pending":
        return {"level": "warning", "reason": "调度等待", "detail": reason or "Pending", "suggestion": "检查资源/调度/PVC"}
    if phase == "Failed":
        return {"level": "critical", "reason": "Pod 已失败", "detail": reason or "Failed", "suggestion": "查看事件和日志"}
    return _empty_diagnosis()


def diagnose_workload(kind: str, workload: Any, *, ready_replicas: int, desired_replicas: int) -> dict[str, str]:
    if desired_replicas == 0:
        return {"level": "info", "reason": "副本数为 0", "detail": f"{kind} 期望副本为 0", "suggestion": "如非预期停服请伸缩"}
    if ready_replicas < desired_replicas:
        status = getattr(workload, "status", None)
        updated = int(getattr(status, "updated_replicas", 0) or 0)
        available = int(getattr(status, "available_replicas", 0) or 0)
        level = "warning" if ready_replicas > 0 else "critical"
        return {"level": level, "reason": "副本未就绪", "detail": f"Ready {ready_replicas}/{desired_replicas}, Updated {updated}, Available {available}", "suggestion": "查看关联 Pod 与 Events"}
    return _empty_diagnosis()


def summarize_pod(pod: Any) -> dict[str, Any]:
    """将 K8s Pod 对象转换为前端列表行格式。"""
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "kind": "Pod",
        "status": pod_status(pod),
        "ready": pod_ready(pod),
        "restarts": pod_restarts(pod),
        "diagnosis": diagnose_pod(pod),
        "node": pod.spec.node_name or "N/A",
        "age": format_age(pod.metadata.creation_timestamp),
        "image": pod.spec.containers[0].image if pod.spec.containers else "",
        "containers": [cs.name for cs in (pod.status.container_statuses or [])],
        "labels": pod.metadata.labels or {},
        "owner_references": [{"kind": o.kind, "name": o.name, "uid": o.uid} for o in (pod.metadata.owner_references or [])],
    }


def summarize_workload(kind: str, workload: Any) -> dict[str, Any]:
    """将 K8s 工作负载转换为前端展示的格式"""
    status = workload.status
    
    if kind == "DaemonSet":
        desired = status.desired_number_scheduled or 0
        ready_reps = status.number_ready or 0
        status_text = "Running" if ready_reps == desired else "Degraded"
        ready_text = f"{ready_reps}/{desired}"
        image = workload.spec.template.spec.containers[0].image if workload.spec.template.spec.containers else ""
    elif kind == "Job":
        desired = workload.spec.completions or 1
        ready_reps = status.succeeded or 0
        if status.failed and status.failed > 0:
            status_text = "Failed"
        elif ready_reps >= desired:
            status_text = "Complete"
        else:
            status_text = "Running"
        ready_text = f"{ready_reps}/{desired}"
        image = workload.spec.template.spec.containers[0].image if workload.spec.template.spec.containers else ""
    elif kind == "CronJob":
        active_jobs = len(status.active) if status.active else 0
        ready_text = f"{active_jobs} Active"
        status_text = "Suspended" if workload.spec.suspend else "Active"
        desired = 0
        ready_reps = active_jobs
        image = workload.spec.job_template.spec.template.spec.containers[0].image if workload.spec.job_template.spec.template.spec.containers else ""
    else:
        desired = workload.spec.replicas or 0
        ready_reps = getattr(status, "ready_replicas", None) or 0
        status_text = "Running" if ready_reps == desired else "Degraded"
        ready_text = f"{ready_reps}/{desired}"
        image = workload.spec.template.spec.containers[0].image if workload.spec.template.spec.containers else ""

    return {
        "kind": kind,
        "namespace": workload.metadata.namespace,
        "name": workload.metadata.name,
        "ready": ready_text,
        "status": status_text,
        "diagnosis": diagnose_workload(kind, workload, ready_replicas=ready_reps, desired_replicas=desired),
        "restarts": 0,
        "age": str(workload.metadata.creation_timestamp)[:10],
        "image": image,
    }
