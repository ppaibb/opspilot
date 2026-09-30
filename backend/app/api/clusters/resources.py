"""Generic resource list/detail/yaml/apply/search routes."""

import datetime
import yaml
from typing import Any, List, Optional

from fastapi import Depends, HTTPException, Query
from kubernetes import client
from kubernetes.client.rest import ApiException

from app.core.security import require_permissions, require_risk_ack

from ._router import router
from ._schemas import ResourceFormPatchRequest, YamlApplyRequest
from ._shared import (
    RESOURCE_MAP,
    K8S_READ_CACHE_TTL_SECONDS,
    _apply_single_doc,
    _extract_continue_token,
    _extract_workload_image,
    _extract_workload_ready,
    _format_age,
    _labels_to_selector,
    _model_to_dict,
    _normalize_kind,
    _normalize_k8s_limit,
    _page_response,
    _parse_multi_docs,
    _patch_config_form,
    _patch_metadata_form,
    _patch_service_form,
    _patch_workload_form,
    _pod_ready,
    _pod_restarts,
    _pod_status,
    _raise_k8s_http_exception,
    _read_dynamic_resource,
    _resource_edit_payload,
    _safe_continue_token,
    _validate_k8s_selector,
)
from . import _shared

@router.post("/{cluster_id}/dry-run")
def cluster_dry_run(cluster_id: str, payload: YamlApplyRequest):
    """Server-side dry-run 校验，不实际修改集群资源。"""
    api_client = _shared._get_api_client(cluster_id)
    docs = _parse_multi_docs(payload.yaml_content)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接集群失败: {str(e)}")
    results = []
    errors = []
    for doc in docs:
        result = _apply_single_doc(dyn, doc, dry_run=True)
        if result["status"] == "valid":
            results.append(result)
        else:
            errors.append(result)
    if errors:
        return {"success": False, "results": results, "errors": errors}
    return {"success": True, "results": results}

@router.post(
    "/{cluster_id}/apply",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "apply")),
    ],
)
def cluster_apply(cluster_id: str, payload: YamlApplyRequest):
    """真正执行 server-side apply 操作。"""
    api_client = _shared._get_api_client(cluster_id)
    docs = _parse_multi_docs(payload.yaml_content)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接集群失败: {str(e)}")
    # 第一阶段：dry-run 校验
    dry_run_errors = []
    for doc in docs:
        result = _apply_single_doc(dyn, doc, dry_run=True)
        if result["status"] != "valid":
            dry_run_errors.append(result)
    if dry_run_errors:
        raise HTTPException(status_code=422, detail={"message": "Dry-run 校验未通过，已阻止 Apply", "errors": dry_run_errors})
    # 第二阶段：真正 apply
    applied = []
    apply_errors = []
    for doc in docs:
        result = _apply_single_doc(dyn, doc, dry_run=False)
        if result["status"] == "valid":
            applied.append(result)
        else:
            apply_errors.append(result)
    if apply_errors:
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": False, "applied": applied, "errors": apply_errors}
    _shared._clear_k8s_read_cache(cluster_id)
    return {"success": True, "applied": applied}

def _resource_api_version(kind: str) -> str:
    """根据 Kind 返回 Kubernetes apiVersion，供通用资源列表展示。"""
    kind = _normalize_kind(kind)
    if kind in RESOURCE_MAP:
        group, version, _ = RESOURCE_MAP[kind]
        return f"{group}/{version}" if group else version
    return "-"

def _summarize_resource_status(kind: str, obj: dict) -> str:
    """提取通用资源状态，不返回 Secret data 等敏感字段。"""
    kind = _normalize_kind(kind)
    status = obj.get("status") or {}
    spec = obj.get("spec") or {}
    if kind == "Service":
        return spec.get("type") or "Service"
    if kind == "Ingress":
        return "Ready" if status.get("loadBalancer") else "Pending"
    if kind in ("PersistentVolume", "PersistentVolumeClaim"):
        return status.get("phase") or "Unknown"
    if kind == "Event":
        return obj.get("type") or "Event"
    if kind == "Secret":
        return obj.get("type") or "Opaque"
    if status.get("phase"):
        return status.get("phase")
    for condition in status.get("conditions") or []:
        if condition.get("type") in ("Ready", "Available", "Established") and condition.get("status") == "True":
            return condition.get("type")
    if status:
        return "Observed"
    return "Active"

def _summarize_resource_detail(kind: str, obj: dict) -> str:
    """为通用资源列表生成短描述，避免前端读取完整对象。"""
    kind = _normalize_kind(kind)
    spec = obj.get("spec") or {}
    metadata = obj.get("metadata") or {}
    if kind == "Service":
        ports = spec.get("ports") or []
        port_strs = []
        for p in ports:
            port_val = p.get("port")
            node_port = p.get("nodePort") or p.get("node_port")
            proto = p.get("protocol") or "TCP"
            if node_port:
                port_strs.append(f"{port_val}:{node_port}/{proto}")
            else:
                port_strs.append(f"{port_val}/{proto}")
        ports_desc = ", ".join(port_strs) if port_strs else "-"
        
        status_obj = obj.get("status") or {}
        lb = status_obj.get("loadBalancer") or {}
        ingresses = lb.get("ingress") or []
        ext_ips = []
        for ing in ingresses:
            if ing.get("ip"):
                ext_ips.append(ing.get("ip"))
            elif ing.get("hostname"):
                ext_ips.append(ing.get("hostname"))
        ext_ip_desc = f" | Ext: {', '.join(ext_ips)}" if ext_ips else ""
        
        return f"{spec.get('type', '-')}/{spec.get('clusterIP') or spec.get('cluster_ip') or '-'}{ext_ip_desc} | Ports: {ports_desc}"
    if kind == "Ingress":
        rules = spec.get("rules") or []
        hosts = [rule.get("host") for rule in rules if rule.get("host")]
        return ", ".join(hosts[:2]) or "-"
    if kind == "Event":
        return obj.get("reason") or obj.get("note") or "-"
    if kind == "Secret":
        secret_type = obj.get("type") or "Opaque"
        return f"{secret_type} | {len(obj.get('data') or {})} keys"
    if kind == "ConfigMap":
        return f"{len(obj.get('data') or {})} keys"
    if kind in ("Role", "ClusterRole"):
        return f"{len(obj.get('rules') or [])} rules"
    if kind in ("RoleBinding", "ClusterRoleBinding"):
        return f"{len(obj.get('subjects') or [])} subjects"
    labels = metadata.get("labels") or {}
    return f"{len(labels)} labels" if labels else "-"

def _typed_resource_rows(
    api_client: client.ApiClient,
    kind: str,
    namespace: str,
    kwargs: dict[str, Any],
    paginated: bool,
    page_limit: int,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """高频核心资源的 typed client 快路径。

    DynamicClient 灵活但需要 discovery/dynamic 包装。Pod/Service/ConfigMap/Secret/
    Namespace 这些高频资源走 typed client，降低列表接口开销。
    """
    kind = _normalize_kind(kind)
    v1 = client.CoreV1Api(api_client=api_client)
    scope = "Namespaced"
    if kind == "Pod":
        response = (
            v1.list_namespaced_pod(namespace=namespace, **kwargs)
            if namespace and namespace != "_all"
            else v1.list_pod_for_all_namespaces(**kwargs)
        )
    elif kind == "Service":
        response = (
            v1.list_namespaced_service(namespace=namespace, **kwargs)
            if namespace and namespace != "_all"
            else v1.list_service_for_all_namespaces(**kwargs)
        )
    elif kind == "ConfigMap":
        response = (
            v1.list_namespaced_config_map(namespace=namespace, **kwargs)
            if namespace and namespace != "_all"
            else v1.list_config_map_for_all_namespaces(**kwargs)
        )
    elif kind == "Secret":
        response = (
            v1.list_namespaced_secret(namespace=namespace, **kwargs)
            if namespace and namespace != "_all"
            else v1.list_secret_for_all_namespaces(**kwargs)
        )
    elif kind == "Namespace":
        response = v1.list_namespace(**kwargs)
        scope = "Cluster"
    else:
        return None

    rows = []
    for item in response.items:
        raw = _model_to_dict(item)
        metadata = raw.get("metadata") or {}
        rows.append({
            "name": metadata.get("name") or "-",
            "namespace": metadata.get("namespace") or "",
            "kind": kind,
            "apiVersion": _resource_api_version(kind),
            "scope": scope,
            "status": _summarize_resource_status(kind, raw),
            "detail": _summarize_resource_detail(kind, raw),
            "age": _format_age(metadata.get("creation_timestamp") or metadata.get("creationTimestamp")),
            "image": _extract_workload_image(kind, raw),
            "ready": _extract_workload_ready(kind, raw),
        })
    if paginated:
        return _page_response(rows, _extract_continue_token(response), page_limit)
    return rows

@router.get("/{cluster_id}/resources")
def list_cluster_resources(
    cluster_id: str,
    kind: str = Query(..., description="Kubernetes Kind, e.g. Service"),
    namespace: str = Query("_all", description="namespace, _all for all or cluster scoped"),
    limit: int | None = Query(None, ge=1, le=500, description="分页大小，最大 500"),
    continue_token: str = Query("", description="Kubernetes continue token"),
    label_selector: str = Query("", description="Kubernetes labelSelector"),
    field_selector: str = Query("", description="Kubernetes fieldSelector"),
    paginated: bool = Query(False, description="true 时返回分页 envelope，false 保持旧版列表结构"),
    no_cache: bool = Query(False, description="强制跳过缓存直接拉取"),
):
    """通用 K8s 资源列表接口。

    仅返回列表展示所需的元信息和简要状态；Secret 不返回 data/stringData，
    避免把敏感内容泄露到浏览器。
    """
    api_client = _shared._get_api_client(cluster_id)
    page_limit = _normalize_k8s_limit(limit)
    safe_continue_token = _safe_continue_token(continue_token)
    safe_label_selector = _validate_k8s_selector(label_selector, "label_selector")
    safe_field_selector = _validate_k8s_selector(field_selector, "field_selector")
    use_k8s_paging = paginated or limit is not None or bool(safe_continue_token)

    def _resource_list_kwargs(resource: Any | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"_request_timeout": 6} if resource is None else {}
        if resource is not None and getattr(resource, "namespaced", False) and namespace != "_all":
            kwargs["namespace"] = namespace
        if use_k8s_paging:
            kwargs["limit"] = page_limit
        if safe_continue_token:
            kwargs["_continue"] = safe_continue_token
        if safe_label_selector:
            kwargs["label_selector"] = safe_label_selector
        if safe_field_selector:
            kwargs["field_selector"] = safe_field_selector
        return kwargs

    def _load_resources() -> list[dict[str, Any]] | dict[str, Any]:
        typed_rows = _typed_resource_rows(
            api_client,
            kind,
            namespace or "_all",
            _resource_list_kwargs(),
            paginated,
            page_limit,
        )
        if typed_rows is not None:
            return typed_rows

        dyn = _shared._get_dynamic_client(cluster_id)
        if kind in RESOURCE_MAP:
            group, version, _ = RESOURCE_MAP[kind]
            api_ver = f"{group}/{version}" if group else version
            resource = dyn.resources.get(api_version=api_ver, kind=kind)
        else:
            resource = dyn.resources.get(kind=kind)

        result = resource.get(**_resource_list_kwargs(resource))

        result_dict = result.to_dict()
        items = result_dict.get("items") or []
        rows = []
        for item in items:
            metadata = item.get("metadata") or {}
            rows.append({
                "name": metadata.get("name") or "-",
                "namespace": metadata.get("namespace") or "",
                "kind": kind,
                "apiVersion": _resource_api_version(kind),
                "scope": "Namespaced" if getattr(resource, "namespaced", False) else "Cluster",
                "status": _summarize_resource_status(kind, item),
                "detail": _summarize_resource_detail(kind, item),
                "age": _format_age(metadata.get("creationTimestamp")),
                "image": _extract_workload_image(kind, item),
                "ready": _extract_workload_ready(kind, item),
            })
        if paginated:
            return _page_response(rows, _extract_continue_token(result_dict), page_limit)
        return rows

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "resources",
            (
                namespace or "_all",
                kind,
                page_limit if use_k8s_paging else 0,
                safe_continue_token,
                safe_label_selector,
                safe_field_selector,
                paginated,
            ),
            _load_resources,
            ttl_seconds=0 if no_cache else K8S_READ_CACHE_TTL_SECONDS,
        )
    except ApiException as e:
        raise _raise_k8s_http_exception(e, "K8s 资源列表查询")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"K8s 资源列表查询失败: {str(e)}")

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/events")
def get_resource_events(cluster_id: str, namespace: str, kind: str, name: str):
    """获取特定资源相关的 K8s Events。"""
    api_client = _shared._get_api_client(cluster_id)

    def _load_events() -> list[dict[str, Any]]:
        v1 = client.CoreV1Api(api_client=api_client)
        # 使用 fieldSelector 在 API Server 侧过滤目标对象，避免把全量事件拉到后端再筛。
        field_selector = f"involvedObject.kind={kind},involvedObject.name={name}"
        if namespace and namespace != "_all":
            events = v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
                _request_timeout=6,
            ).items
        else:
            # Node、Namespace、PV 等集群级资源没有 namespace，必须全局查询事件。
            events = v1.list_event_for_all_namespaces(
                field_selector=field_selector,
                _request_timeout=6,
            ).items

        # 按照时间倒序
        events.sort(key=lambda x: x.last_timestamp or x.event_time or x.creation_timestamp or datetime.datetime.min, reverse=True)

        result = []
        for e in events:
            result.append({
                "type": e.type, # Normal / Warning
                "reason": e.reason,
                "message": e.message,
                "count": e.count,
                "firstTimestamp": str(e.first_timestamp) if e.first_timestamp else None,
                "lastTimestamp": str(e.last_timestamp) if e.last_timestamp else None,
                "source": e.source.component if e.source else "Unknown"
            })
            if len(result) >= 100:
                break
        return result

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id,
            "events",
            (namespace or "_all", kind, name),
            _load_events,
        )
    except ApiException as e:
        raise HTTPException(status_code=502, detail=f"获取事件失败: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取事件异常: {str(e)}")

def _pod_summary(pod: Any) -> dict[str, Any]:
    """生成前端关联拓扑需要的 Pod 摘要，避免泄露完整 spec。"""
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "status": _pod_status(pod),
        "ready": _pod_ready(pod),
        "restarts": _pod_restarts(pod),
        "node": pod.spec.node_name or "-",
        "ip": pod.status.pod_ip or "-",
        "age": _format_age(pod.metadata.creation_timestamp),
    }

def _endpoint_subset_summary(endpoint: Any) -> list[dict[str, Any]]:
    """整理 Service Endpoints，前端只展示地址、端口和 Ready 状态。"""
    rows: list[dict[str, Any]] = []
    for subset in endpoint.subsets or []:
        ports = [str(port.port) for port in (subset.ports or [])]
        for address in subset.addresses or []:
            rows.append({
                "ip": address.ip,
                "target": getattr(address.target_ref, "name", "") if address.target_ref else "",
                "ready": True,
                "ports": ports,
            })
        for address in subset.not_ready_addresses or []:
            rows.append({
                "ip": address.ip,
                "target": getattr(address.target_ref, "name", "") if address.target_ref else "",
                "ready": False,
                "ports": ports,
            })
    return rows

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/related")
def get_resource_related(cluster_id: str, namespace: str, kind: str, name: str):
    """查询资源关联关系，用于详情抽屉中的轻量拓扑。

    安全边界：
    1. 只读 Kubernetes API，不执行任何写操作；
    2. 只返回 Pod/Endpoint 摘要，不返回完整 spec、env、secret 等敏感字段；
    3. 复杂 selector 先保持只读提示，不做不确定的错误推断。
    """
    if not namespace or namespace == "_all":
        return {"pods": [], "endpoints": [], "message": "该资源没有明确 namespace，暂不查询关联资源"}
    api_client = _shared._get_api_client(cluster_id)
    kind_lower = kind.lower()
    try:
        v1 = client.CoreV1Api(api_client=api_client)
        apps_v1 = client.AppsV1Api(api_client=api_client)
        selector = ""
        message = ""

        if kind_lower == "deployment":
            obj = apps_v1.read_namespaced_deployment(name=name, namespace=namespace, _request_timeout=6)
            selector = _labels_to_selector(obj.spec.selector.match_labels or {})
        elif kind_lower == "statefulset":
            obj = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace, _request_timeout=6)
            selector = _labels_to_selector(obj.spec.selector.match_labels or {})
        elif kind_lower == "daemonset":
            obj = apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace, _request_timeout=6)
            selector = _labels_to_selector(obj.spec.selector.match_labels or {})
        elif kind_lower == "pod":
            obj = v1.read_namespaced_pod(name=name, namespace=namespace, _request_timeout=6)
            selector = "" # Pod doesn't have a selector to find other pods usually, we just return itself or empty
        elif kind_lower == "service":
            obj = v1.read_namespaced_service(name=name, namespace=namespace, _request_timeout=6)
            selector = _labels_to_selector(obj.spec.selector or {})
        else:
            return {"pods": [], "endpoints": [], "ingresses": [], "pvcs": [], "message": f"{kind} 暂无内置关联拓扑"}

        pods = []
        if selector:
            pods = [
                _pod_summary(item)
                for item in v1.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=selector,
                    _request_timeout=6,
                ).items[:100]
            ]
        else:
            message = "未发现可用于匹配 Pod 的 selector"

        endpoints = []
        ingresses = []
        if kind_lower == "service":
            try:
                endpoint = v1.read_namespaced_endpoints(
                    name=name,
                    namespace=namespace,
                    _request_timeout=6,
                )
                endpoints = _endpoint_subset_summary(endpoint)
            except ApiException as e:
                if e.status != 404:
                    raise
            
            try:
                networking_v1 = client.NetworkingV1Api(api_client=api_client)
                all_ingresses = networking_v1.list_namespaced_ingress(namespace=namespace, _request_timeout=6).items
                for ing in all_ingresses:
                    matches = False
                    for rule in (ing.spec.rules or []):
                        for path in getattr(rule.http, "paths", []):
                            if getattr(getattr(path.backend, "service", None), "name", None) == name:
                                matches = True
                                break
                        if matches:
                            break
                    if matches:
                        ingresses.append({
                            "name": ing.metadata.name,
                            "namespace": ing.metadata.namespace,
                            "class_name": getattr(ing.spec, "ingress_class_name", "-") or "-",
                            "hosts": [rule.host for rule in (ing.spec.rules or []) if rule.host],
                            "age": _format_age(ing.metadata.creation_timestamp),
                        })
            except ApiException as e:
                if e.status != 404:
                    raise

        pvcs = []
        configmaps = set()
        secrets_used = set()
        if kind_lower in ("deployment", "statefulset", "daemonset", "pod"):
            pod_spec = None
            if kind_lower == "pod":
                pod_spec = obj.spec
            elif kind_lower in ("deployment", "statefulset", "daemonset"):
                pod_spec = obj.spec.template.spec

            if pod_spec:
                pvc_names = []
                for vol in (pod_spec.volumes or []):
                    if vol.persistent_volume_claim:
                        pvc_names.append(vol.persistent_volume_claim.claim_name)
                    if vol.config_map:
                        configmaps.add(vol.config_map.name)
                    if vol.secret:
                        secrets_used.add(vol.secret.secret_name)

                # 从容器 envFrom / env.valueFrom 提取 ConfigMap/Secret 引用
                for ctr in list(pod_spec.containers or []) + list(pod_spec.init_containers or []):
                    for ef in (ctr.env_from or []):
                        if ef.config_map_ref:
                            configmaps.add(ef.config_map_ref.name)
                        if ef.secret_ref:
                            secrets_used.add(ef.secret_ref.name)
                    for env_var in (ctr.env or []):
                        if env_var.value_from:
                            if env_var.value_from.config_map_key_ref:
                                configmaps.add(env_var.value_from.config_map_key_ref.name)
                            if env_var.value_from.secret_key_ref:
                                secrets_used.add(env_var.value_from.secret_key_ref.name)
                
                if pvc_names:
                    for pvc_name in pvc_names:
                        try:
                            pvc_obj = v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace, _request_timeout=6)
                            pvcs.append({
                                "name": pvc_obj.metadata.name,
                                "status": getattr(pvc_obj.status, "phase", "Unknown"),
                                "capacity": getattr(pvc_obj.status, "capacity", {}).get("storage", "-"),
                                "storage_class": pvc_obj.spec.storage_class_name or "-",
                            })
                        except ApiException:
                            pvcs.append({
                                "name": pvc_name,
                                "status": "NotFound",
                                "capacity": "-",
                                "storage_class": "-",
                            })

        services = []
        if kind_lower in ("deployment", "statefulset", "daemonset", "pod"):
            pod_labels = {}
            if kind_lower == "pod":
                pod_labels = obj.metadata.labels or {}
            else:
                pod_labels = getattr(obj.spec.template.metadata, "labels", {}) or {}
            
            try:
                all_svcs = v1.list_namespaced_service(namespace=namespace, _request_timeout=6).items
                for svc in all_svcs:
                    svc_selector = svc.spec.selector
                    if svc_selector and all(pod_labels.get(k) == v for k, v in svc_selector.items()):
                        services.append({
                            "name": svc.metadata.name,
                            "namespace": svc.metadata.namespace,
                            "type": svc.spec.type,
                            "cluster_ip": svc.spec.cluster_ip,
                            "ports": [f"{p.port}:{p.target_port}/{p.protocol}" for p in (svc.spec.ports or [])],
                        })
                
                if services:
                    networking_v1 = client.NetworkingV1Api(api_client=api_client)
                    all_ingresses = networking_v1.list_namespaced_ingress(namespace=namespace, _request_timeout=6).items
                    matched_svc_names = {s["name"] for s in services}
                    for ing in all_ingresses:
                        matches = False
                        target_svcs = set()
                        for rule in (ing.spec.rules or []):
                            for path in getattr(rule.http, "paths", []):
                                svc_name = getattr(getattr(path.backend, "service", None), "name", None)
                                if svc_name in matched_svc_names:
                                    matches = True
                                    target_svcs.add(svc_name)
                        if matches:
                            ingresses.append({
                                "name": ing.metadata.name,
                                "namespace": ing.metadata.namespace,
                                "class_name": getattr(ing.spec, "ingress_class_name", "-") or "-",
                                "hosts": [rule.host for rule in (ing.spec.rules or []) if rule.host],
                                "age": _format_age(ing.metadata.creation_timestamp),
                                "target_services": list(target_svcs)
                            })
            except ApiException:
                pass

        return {
            "selector": selector,
            "pods": pods,
            "endpoints": endpoints,
            "services": services,
            "ingresses": ingresses,
            "pvcs": pvcs,
            "configmaps": sorted(configmaps),
            "secrets": sorted(secrets_used),
            "message": message,
        }
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 不存在")
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询关联资源失败: {str(e)}")

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/consumers")
def get_resource_consumers(cluster_id: str, namespace: str, kind: str, name: str):
    """反查哪些 Workload 在使用指定的 ConfigMap/Secret/PVC。"""
    kind = _normalize_kind(kind)
    if kind not in ("ConfigMap", "Secret", "PersistentVolumeClaim"):
        raise HTTPException(status_code=400, detail=f"consumers 查询仅支持 ConfigMap/Secret/PersistentVolumeClaim，收到: {kind}")

    api_client = _shared._get_api_client(cluster_id)
    v1 = client.CoreV1Api(api_client=api_client)

    def _load_consumers():
        pods = v1.list_namespaced_pod(namespace=namespace, _request_timeout=10).items
        # 匹配到的 Pod owner 上溯到 Workload
        consumer_workloads: dict[str, dict] = {}
        matched_pod_count = 0

        for pod in pods:
            spec = pod.spec
            if not spec:
                continue
            matched = False

            if kind == "PersistentVolumeClaim":
                for vol in (spec.volumes or []):
                    if vol.persistent_volume_claim and vol.persistent_volume_claim.claim_name == name:
                        matched = True
                        break
            elif kind == "ConfigMap":
                for vol in (spec.volumes or []):
                    if vol.config_map and vol.config_map.name == name:
                        matched = True
                        break
                if not matched:
                    for ctr in list(spec.containers or []) + list(spec.init_containers or []):
                        for ef in (ctr.env_from or []):
                            if ef.config_map_ref and ef.config_map_ref.name == name:
                                matched = True
                                break
                        if matched:
                            break
                        for env_var in (ctr.env or []):
                            if env_var.value_from and env_var.value_from.config_map_key_ref and env_var.value_from.config_map_key_ref.name == name:
                                matched = True
                                break
                        if matched:
                            break
            elif kind == "Secret":
                for vol in (spec.volumes or []):
                    if vol.secret and vol.secret.secret_name == name:
                        matched = True
                        break
                if not matched:
                    for ctr in list(spec.containers or []) + list(spec.init_containers or []):
                        for ef in (ctr.env_from or []):
                            if ef.secret_ref and ef.secret_ref.name == name:
                                matched = True
                                break
                        if matched:
                            break
                        for env_var in (ctr.env or []):
                            if env_var.value_from and env_var.value_from.secret_key_ref and env_var.value_from.secret_key_ref.name == name:
                                matched = True
                                break
                        if matched:
                            break

            if matched:
                matched_pod_count += 1
                # 通过 ownerReferences 上溯到 Workload
                owner = _resolve_workload_owner(pod)
                if owner:
                    key = f"{owner['kind']}/{owner['name']}"
                    if key not in consumer_workloads:
                        consumer_workloads[key] = owner

        return {
            "consumers": list(consumer_workloads.values()),
            "pod_count": matched_pod_count,
        }

    try:
        return _shared._get_or_set_k8s_read_cache(
            cluster_id, "consumers", (namespace, kind, name), _load_consumers, ttl_seconds=5
        )
    except ApiException as e:
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询资源使用者失败: {str(e)}")

def _resolve_workload_owner(pod) -> dict | None:
    """从 Pod 的 ownerReferences 上溯到顶层 Workload（ReplicaSet → Deployment）。"""
    owners = pod.metadata.owner_references or []
    if not owners:
        return {"kind": "Pod", "name": pod.metadata.name, "namespace": pod.metadata.namespace}
    owner = owners[0]
    return {"kind": owner.kind, "name": owner.name, "namespace": pod.metadata.namespace}

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/detail")
def get_resource_detail(cluster_id: str, namespace: str, kind: str, name: str):
    """获取 K8s 资源的全量 JSON 详情（Secret 的 data/stringData 不回传）。"""
    api_client = _shared._get_api_client(cluster_id)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
        _, _, raw = _read_dynamic_resource(dyn, namespace, kind, name)
        # 清理无用的 managedFields 以减小体积
        if "metadata" in raw and "managedFields" in raw["metadata"]:
            del raw["metadata"]["managedFields"]
        if _normalize_kind(kind) == "Secret":
            # Secret 内容边界：密文不进浏览器，仅返回占位标记。
            raw.pop("data", None)
            raw.pop("stringData", None)
            raw["secret_redacted"] = True
        return raw
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 不存在")
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取资源详情失败: {str(e)}")

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/yaml")
def get_resource_yaml(cluster_id: str, namespace: str, kind: str, name: str):
    """获取已有 K8s 资源的 YAML 表示。"""
    kind = _normalize_kind(kind)
    api_client = _shared._get_api_client(cluster_id)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接集群失败: {str(e)}")
    # 定位资源类型
    if kind in RESOURCE_MAP:
        group, version, plural = RESOURCE_MAP[kind]
        api_ver = f"{group}/{version}" if group else version
        try:
            resource = dyn.resources.get(api_version=api_ver, kind=kind)
        except Exception:
            raise HTTPException(status_code=400, detail=f"无法定位资源类型: {kind}")
    else:
        try:
            resource = dyn.resources.get(kind=kind)
        except Exception:
            raise HTTPException(status_code=400, detail=f"不支持的资源类型: {kind}")
    try:
        kwargs = {"name": name}
        if namespace and namespace != "_all":
            kwargs["namespace"] = namespace
        obj = resource.get(**kwargs)
        raw_dict = obj.to_dict()
        # 清理运行时元数据
        metadata = raw_dict.get("metadata", {})
        for key in ("managedFields", "resourceVersion", "uid", "creationTimestamp", "generation", "selfLink"):
            metadata.pop(key, None)
        raw_dict["metadata"] = metadata
        raw_dict.pop("status", None)
        if _normalize_kind(kind) == "Secret":
            # Secret 内容边界：密文不落 YAML（也就不会进浏览器/前端缓存）。
            # 提示走注释而不是字段——YAML 会被直接回填到工作台并再次 Apply，
            # 非法字段会导致 apiserver 校验失败；缺 data 的回写由 apply 端
            # 保留原密文，不会清空线上 Secret。
            raw_dict.pop("data", None)
            raw_dict.pop("stringData", None)
            yaml_content = (
                "# Secret 的密文内容已由平台脱敏；直接 Apply 本 YAML 不会清空原有密文。\n"
                + yaml.dump(raw_dict, default_flow_style=False, allow_unicode=True, sort_keys=False)
            )
        else:
            yaml_content = yaml.dump(raw_dict, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return {"yaml_content": yaml_content}
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 在 namespace={namespace} 中不存在")
        raise HTTPException(status_code=502, detail=f"K8s API 错误: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取资源 YAML 失败: {str(e)}")

@router.get("/{cluster_id}/resources/{namespace}/{kind}/{name}/edit")
def get_resource_edit_form(cluster_id: str, namespace: str, kind: str, name: str):
    """获取资源表单化编辑模型。

    返回值只包含允许表单编辑的安全字段；复杂 spec 仍建议走 YAML 高级模式。
    """
    api_client = _shared._get_api_client(cluster_id)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
        _, _, raw = _read_dynamic_resource(dyn, namespace, kind, name)
        return _resource_edit_payload(kind, raw, namespace, name)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 不存在")
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取资源编辑信息失败: {str(e)}")

@router.patch(
    "/{cluster_id}/resources/{namespace}/{kind}/{name}/edit",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "edit")),
    ],
)
def patch_resource_edit_form(
    cluster_id: str,
    namespace: str,
    kind: str,
    name: str,
    payload: ResourceFormPatchRequest,
):
    """提交资源表单化编辑。

    该接口只允许白名单字段，不支持任意 Patch，避免从 UI 暴露危险能力。
    """
    api_client = _shared._get_api_client(cluster_id)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
        resource, _, raw_obj = _read_dynamic_resource(dyn, namespace, kind, name)
        if kind in ("Deployment", "StatefulSet", "DaemonSet"):
            if not namespace or namespace == "_all":
                raise HTTPException(status_code=400, detail=f"{kind} 表单编辑需要明确 namespace")
            result = _patch_workload_form(api_client, kind, namespace, name, payload, raw_obj)
        elif kind == "Service":
            if not namespace or namespace == "_all":
                raise HTTPException(status_code=400, detail="Service 表单编辑需要明确 namespace")
            result = _patch_service_form(api_client, namespace, name, payload)
        elif kind in ("ConfigMap", "Secret"):
            if not namespace or namespace == "_all":
                raise HTTPException(status_code=400, detail=f"{kind} 表单编辑需要明确 namespace")
            result = _patch_config_form(resource, namespace, name, payload)
        else:
            result = _patch_metadata_form(resource, namespace, name, payload)
        _shared._clear_k8s_read_cache(cluster_id)
        return result
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 不存在")
        raise HTTPException(status_code=e.status or 502, detail=f"K8s API 错误: {e.reason}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"资源表单编辑失败: {str(e)}")

@router.delete(
    "/{cluster_id}/resources/{namespace}/{kind}/{name}",
    dependencies=[
        Depends(require_permissions("action.assets.clusters.manage")),
        Depends(require_risk_ack("action.assets.clusters.manage", "delete")),
    ],
)
def delete_resource(cluster_id: str, namespace: str, kind: str, name: str):
    """删除已有 K8s 资源。"""
    api_client = _shared._get_api_client(cluster_id)
    try:
        dyn = _shared._get_dynamic_client(cluster_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接集群失败: {str(e)}")

    if kind in RESOURCE_MAP:
        group, version, plural = RESOURCE_MAP[kind]
        api_ver = f"{group}/{version}" if group else version
        try:
            resource = dyn.resources.get(api_version=api_ver, kind=kind)
        except Exception:
            raise HTTPException(status_code=400, detail=f"无法定位资源类型: {kind}")
    else:
        try:
            resource = dyn.resources.get(kind=kind)
        except Exception:
            raise HTTPException(status_code=400, detail=f"不支持的资源类型: {kind}")

    try:
        kwargs = {"name": name}
        if namespace and namespace != "_all":
            kwargs["namespace"] = namespace
        resource.delete(**kwargs)
        _shared._clear_k8s_read_cache(cluster_id)
        return {"success": True, "message": f"{kind} {name} deleted"}
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"资源 {kind}/{name} 不存在")
        raise HTTPException(status_code=502, detail=f"K8s API 错误: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除资源失败: {str(e)}")

@router.get("/{cluster_id}/search")
def search_cluster_resources(
    cluster_id: str,
    q: str = Query(..., min_length=1, max_length=128, description="搜索关键词"),
    kinds: str = Query("Pod,Deployment,StatefulSet,DaemonSet,Service,ConfigMap,Secret,Ingress", description="逗号分隔的 Kind 列表"),
    namespace: str = Query("_all", description="namespace, _all for all"),
    limit: int = Query(50, ge=1, le=200),
):
    """跨 Namespace 全局搜索：按名称模糊匹配多种资源。"""
    api_client = _shared._get_api_client(cluster_id)
    requested_kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    query = q.strip().lower()
    results: list[dict[str, Any]] = []

    def _search_pods():
        v1 = client.CoreV1Api(api_client=api_client)
        if namespace and namespace != "_all":
            pods = v1.list_namespaced_pod(namespace=namespace, _request_timeout=6).items
        else:
            pods = v1.list_pod_for_all_namespaces(_request_timeout=6).items
        for p in pods:
            if query in (p.metadata.name or "").lower() or query in (p.metadata.namespace or "").lower():
                results.append({"kind": "Pod", "name": p.metadata.name, "namespace": p.metadata.namespace, "status": _pod_status(p), "age": _format_age(p.metadata.creation_timestamp)})
                if len(results) >= limit:
                    return

    def _search_workloads(kind: str):
        apps_v1 = client.AppsV1Api(api_client=api_client)
        list_fns = {
            "Deployment": (apps_v1.list_namespaced_deployment, apps_v1.list_deployment_for_all_namespaces),
            "StatefulSet": (apps_v1.list_namespaced_stateful_set, apps_v1.list_stateful_set_for_all_namespaces),
            "DaemonSet": (apps_v1.list_namespaced_daemon_set, apps_v1.list_daemon_set_for_all_namespaces),
        }
        if kind not in list_fns:
            return
        ns_fn, all_fn = list_fns[kind]
        items = ns_fn(namespace=namespace, _request_timeout=6).items if namespace and namespace != "_all" else all_fn(_request_timeout=6).items
        for w in items:
            if query in (w.metadata.name or "").lower() or query in (w.metadata.namespace or "").lower():
                results.append({"kind": kind, "name": w.metadata.name, "namespace": w.metadata.namespace, "status": "Running", "age": _format_age(w.metadata.creation_timestamp)})
                if len(results) >= limit:
                    return

    def _search_core(kind: str):
        v1 = client.CoreV1Api(api_client=api_client)
        list_fns = {
            "Service": (v1.list_namespaced_service, v1.list_service_for_all_namespaces),
            "ConfigMap": (v1.list_namespaced_config_map, v1.list_config_map_for_all_namespaces),
            "Secret": (v1.list_namespaced_secret, v1.list_secret_for_all_namespaces),
        }
        if kind not in list_fns:
            return
        ns_fn, all_fn = list_fns[kind]
        items = ns_fn(namespace=namespace, _request_timeout=6).items if namespace and namespace != "_all" else all_fn(_request_timeout=6).items
        for item in items:
            if query in (item.metadata.name or "").lower() or query in (item.metadata.namespace or "").lower():
                results.append({"kind": kind, "name": item.metadata.name, "namespace": item.metadata.namespace, "status": "Active", "age": _format_age(item.metadata.creation_timestamp)})
                if len(results) >= limit:
                    return

    def _search_ingress():
        net_v1 = client.NetworkingV1Api(api_client=api_client)
        if namespace and namespace != "_all":
            items = net_v1.list_namespaced_ingress(namespace=namespace, _request_timeout=6).items
        else:
            items = net_v1.list_ingress_for_all_namespaces(_request_timeout=6).items
        for item in items:
            if query in (item.metadata.name or "").lower() or query in (item.metadata.namespace or "").lower():
                results.append({"kind": "Ingress", "name": item.metadata.name, "namespace": item.metadata.namespace, "status": "Active", "age": _format_age(item.metadata.creation_timestamp)})
                if len(results) >= limit:
                    return

    from concurrent.futures import ThreadPoolExecutor
    tasks = []
    with ThreadPoolExecutor(max_workers=min(len(requested_kinds), 6)) as executor:
        for kind in requested_kinds:
            if kind == "Pod":
                tasks.append(executor.submit(_search_pods))
            elif kind in ("Deployment", "StatefulSet", "DaemonSet"):
                tasks.append(executor.submit(_search_workloads, kind))
            elif kind in ("Service", "ConfigMap", "Secret"):
                tasks.append(executor.submit(_search_core, kind))
            elif kind == "Ingress":
                tasks.append(executor.submit(_search_ingress))
        for t in tasks:
            try:
                t.result(timeout=10)
            except Exception:
                pass

    results.sort(key=lambda x: (x["kind"], x["namespace"], x["name"]))
    return results[:limit]

