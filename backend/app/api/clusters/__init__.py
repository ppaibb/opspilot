"""容器集群后端交互 API
集成 Kubernetes 官方客户端进行深度纳管
"""

import sys
import types

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from ._router import router, ws_router
from ._schemas import (
    ClusterCreate,
    ClusterResponse,
    ClusterUpdate,
    CreateNamespaceRequest,
    KubeScanRequest,
    NamespaceLabelsPatch,
    NodeDrainRequest,
    PatchNodeLabelsRequest,
    PatchNodeTaintsRequest,
    ResourceContainerPatch,
    ResourceFormPatchRequest,
    ResourceServicePortPatch,
    RolloutUndoRequest,
    ScaleRequest,
    SetImageRequest,
    YamlApplyRequest,
)
from .ws_session import (
    K8sWebSocketSessionContext,
    _K8S_WS_SESSION_LOCK,
    _K8S_WS_SESSION_STORE,
    _K8S_WS_SESSION_TTL_SECONDS,
    _K8sWebSocketSessionRecord,
    _cleanup_expired_k8s_ws_sessions_locked,
    _consume_k8s_ws_session,
    _create_k8s_ws_session,
    _hash_k8s_ws_token,
)
from ._shared import (
    CLIENT_CACHE_TTL_SECONDS,
    K8S_READ_CACHE_TTL_SECONDS,
    KUBECONFIG_DIR,
    RESOURCE_MAP,
    logger,
    parse_k8s_cpu,
    parse_k8s_memory_to_gib,
    _apply_single_doc,
    _cache_entry_is_valid,
    _clear_cluster_client_cache,
    _clear_k8s_read_cache,
    _client_cache,
    _client_cache_lock,
    _diagnose_node,
    _diagnose_pod,
    _diagnose_workload,
    _dynamic_client_cache,
    _empty_diagnosis,
    _extract_continue_token,
    _extract_workload_image,
    _extract_workload_ready,
    _format_age,
    _get_api_client,
    _get_dynamic_client,
    _get_kubeconfig_mtime,
    _get_or_set_k8s_read_cache,
    _k8s_read_cache,
    _k8s_read_cache_lock,
    _k8s_read_inflight,
    _labels_to_selector,
    _load_cluster_kubeconfig,
    _log_k8s_read_metric,
    _mark_cache_hit,
    _metrics_not_available,
    _model_to_dict,
    _normalize_k8s_limit,
    _normalize_kind,
    _open_pod_exec_stream,
    _page_response,
    _parse_cpu_quantity,
    _parse_memory_quantity,
    _parse_multi_docs,
    _patch_config_form,
    _patch_metadata_form,
    _patch_service_form,
    _patch_workload_form,
    _pod_ready,
    _pod_restarts,
    _pod_status,
    _preserve_secret_data_before_apply,
    _raise_k8s_http_exception,
    _read_cache_entry_is_valid,
    _read_dynamic_resource,
    _resolve_dynamic_resource,
    _resolve_resource,
    _resource_edit_payload,
    _safe_continue_token,
    _safe_text_map,
    _sum_container_metrics,
    _validate_k8s_selector,
)
from .cluster_mgmt import (
    create_cluster,
    delete_cluster,
    get_cluster_audit_logs,
    get_cluster_components_health,
    get_cluster_health_summary,
    get_clusters,
    refresh_cluster,
    scan_kubernetes_cluster,
    update_cluster,
)
from .nodes import (
    cordon_node,
    drain_node,
    get_cluster_node_metrics,
    get_cluster_nodes,
    get_cluster_pod_metrics,
    get_node_detail,
    patch_node_labels,
    patch_node_taints,
    preview_node_drain,
    uncordon_node,
    _NODE_DRAIN_EVICTION_TIMEOUT,
    _NODE_DRAIN_MAX_PODS,
    _PROTECTED_LABEL_PREFIXES,
    _list_pods_on_node,
    _node_exists_or_404,
    _patch_node_unschedulable,
    _pod_has_local_emptydir,
    _pod_is_daemonset_managed,
    _pod_is_mirror,
    _pod_is_standalone,
    _pod_is_terminal,
    _summarize_pod_for_drain,
)
from .workloads import (
    MAX_ROLLOUT_HISTORY,
    SUPPORTED_ROLLOUT_KINDS,
    delete_workload,
    get_cluster_workloads,
    get_hpa_status,
    get_workload_rollout_history,
    get_workload_rollout_status,
    list_cronjob_jobs,
    restart_workload,
    rollout_undo_workload,
    scale_workload,
    set_workload_image,
    suspend_cronjob,
    toggle_workload_pause,
    trigger_cronjob,
    _collect_controller_revision_history,
    _collect_deployment_history,
    _find_deployment_previous_revision,
    _read_workload,
    _rollout_status_summary,
    _validate_image_ref,
)
from .resources import (
    cluster_apply,
    cluster_dry_run,
    delete_resource,
    get_resource_consumers,
    get_resource_detail,
    get_resource_edit_form,
    get_resource_events,
    get_resource_related,
    get_resource_yaml,
    list_cluster_resources,
    patch_resource_edit_form,
    search_cluster_resources,
    _endpoint_subset_summary,
    _pod_summary,
    _resolve_workload_owner,
    _resource_api_version,
    _summarize_resource_detail,
    _summarize_resource_status,
    _typed_resource_rows,
)
from .namespaces import (
    PROTECTED_NAMESPACES,
    create_namespace,
    delete_namespace,
    get_namespace_usage,
    list_namespaces,
    patch_namespace_labels,
    _LIMIT_RANGE_DEFAULTS,
    _QUOTA_PRESETS,
    _check_namespace_deletion_allowed,
)
from .pods import (
    create_pod_exec_ws_session,
    create_pod_logs_ws_session,
    delete_pod,
    get_ingress_tls_status,
    get_pod_detail,
    get_pod_diagnostics,
    list_pods,
    pod_exec_ws,
    pod_logs_ws,
    resize_pvc,
    _DIAGNOSTICS_CACHE_TTL,
    _DIAGNOSTICS_EVENT_LIMIT,
    _DIAGNOSTICS_LOG_TAIL,
    _DIAGNOSTIC_HINTS,
    _build_diagnostic_summary,
    _collect_probes,
    _container_state_payload,
    _last_terminated_payload,
    _probe_summary,
    _safe_tail_logs,
)

# Tests and main.py monkeypatch attributes on this package (mock.patch.object
# on app.api.clusters). Sync those writes onto _shared so late-bound lookups
# through ``_shared.<name>`` observe the patched values.
from . import _shared

_SYNCED_TO_SHARED = frozenset(
    {
        "KUBECONFIG_DIR",
        "_get_api_client",
        "_get_dynamic_client",
        "_clear_k8s_read_cache",
        "_get_or_set_k8s_read_cache",
        "k8s_stream",
    }
)


class _ClustersModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _SYNCED_TO_SHARED:
            setattr(_shared, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        if name in _SYNCED_TO_SHARED and hasattr(_shared, name):
            try:
                delattr(_shared, name)
            except AttributeError:
                pass


sys.modules[__name__].__class__ = _ClustersModule
