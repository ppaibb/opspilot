"""VictoriaMetrics / VictoriaLogs 监控告警接口。

当前阶段能力：
- 监控连接配置、查询代理、实时大盘；
- 兼容旧 vmagent target 清单；
- 新主机统一通过 Collector Fleet 下发 Alloy 指标与日志采集策略。

【向后兼容设计说明】
本包由原先 3039 行的 monolithic monitoring.py 拆分而来。__init__.py 采用"显式静态再导出"
而非动态 exec() 加载，原因有三：
1. 静态导入保证 IDE、类型检查器（mypy）、Linter 能正确解析符号；
2. 避免运行时 exec 带来的性能损耗与调试栈混乱；
3. __all__ 可控，防止无意暴露内部辅助函数。

新增代码应优先从子模块直接导入，避免继续膨胀本文件。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 再导出：常量
# ---------------------------------------------------------------------------
from app.api.monitoring.constants import (
    ALERT_RULES_FILE,
    ALERT_SEVERITIES,
    ALLOY_DEFAULT_POLICY_PREFIX,
    ALLOY_SYNC_SERVICE,
    AUTH_TYPES,
    CONFIG_KEY,
    DASHBOARD_RANGE_QUERIES,
    DEFAULT_ALERT_RULES,
    DEFAULT_NODE_EXPORTER_PORT,
    DEFAULT_RULE_GROUP_NAME,
    DEFAULT_STEP_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEPLOYMENT_ACTIONS,
    LOG_TARGETS_CONFIG_KEY,
    MAX_DASHBOARD_RANGE_MINUTES,
    MAX_DASHBOARD_STEP_SECONDS,
    MAX_LOG_LIMIT,
    MAX_LOG_QUERY_LENGTH,
    MIN_DASHBOARD_RANGE_MINUTES,
    MIN_DASHBOARD_STEP_SECONDS,
    MONITORING_COMPOSE_FILE,
    MONITORING_DEPLOY_DIR,
    NOTIFICATIONS_CONFIG_KEY,
    NOTIFICATION_TYPES,
    RELOADABLE_MONITORING_SERVICES,
    REPO_ROOT,
    RULES_CONFIG_KEY,
    TARGETS_CONFIG_KEY,
    TOP_HOST_QUERIES,
    VMAGENT_CONFIG_FILE,
    VMAGENT_LEGACY_MARKER,
    VMAGENT_MANAGED_BEGIN,
    VMAGENT_MANAGED_END,
)

# ---------------------------------------------------------------------------
# 再导出：Pydantic Schemas
# ---------------------------------------------------------------------------
from app.api.monitoring.schemas import (
    AlertingFileChangeItem,
    AlertingRuntimeReloadItem,
    AlertingSyncInfo,
    AlertRuleItem,
    AlertRuleTemplateItem,
    AlertRuleTemplatesResponse,
    ApplyTemplatesRequest,
    LogFieldsRequest,
    LogQueryRequest,
    LogTargetEnableRequest,
    MonitoringCheckItem,
    MonitoringConfigResponse,
    MonitoringConfigUpdate,
    MonitoringDeploymentAction,
    MonitoringRulesResponse,
    MonitoringRulesUpdate,
    MonitoringSelfCheckResponse,
    NotificationChannelItem,
    NotificationChannelsResponse,
    NotificationChannelsUpdate,
    TargetEnableRequest,
)

# ---------------------------------------------------------------------------
# 再导出：Services（保持历史 import 兼容）
# ---------------------------------------------------------------------------
from app.api.monitoring.services.config_store import (
    _normalize_config_item_id,
    _read_system_config_row,
    _safe_load_json_dict,
    _save_system_config_json,
    read_log_targets,
    read_monitoring_config,
    read_monitoring_targets,
    save_log_targets,
    save_monitoring_targets,
)
from app.api.monitoring.services.notifications import (
    _build_test_alertmanager_payload,
    _normalize_notification_channels,
    _persist_notifications_and_sync_alerting,
    read_monitoring_notifications,
)
from app.api.monitoring.services.rules import (
    _normalize_monitoring_rules,
    _persist_rules_and_sync_alerting,
    _restart_compose_service_if_running,
    _yaml_quote,
    apply_rule_templates,
    read_monitoring_rules,
)
from app.api.monitoring.services.vmagent import (
    _merge_vmagent_config_text,
    _normalize_targets_for_vmagent,
    _persist_targets_and_sync_vmagent,
    _reload_vmagent_if_running,
    _render_opspilot_vmagent_block,
    _write_vmagent_config_file,
)
from app.api.monitoring.services.deployment import (
    _apply_local_monitoring_config,
    _compose_args,
    _deployment_status,
    _run_local_command,
)
from app.api.monitoring.services.proxy import (
    _build_authorization_header,
    _normalize_api_base,
    _normalize_vlogs_base,
    _normalize_vlogs_ingest_base,
    _request_kwargs,
    _response_config,
    _vm_get,
    _vlogs_get,
)
from app.api.monitoring.services.dashboard import (
    _clamp_dashboard_range,
    _fetch_alerts,
    _fetch_top_hosts,
    _filter_dashboard_alerts,
    _filter_dashboard_targets,
    _filter_logs_by_scope,
    _filter_metric_instances,
    _matches_any_scope,
    _parse_matrix_first_series,
    _parse_up_targets,
    _parse_vector_by_instance,
    _safe_metric_float,
    _split_scope_param,
)
from app.api.monitoring.services.targets import (
    _asset_target_record,
    _build_alloy_disable_command,
    _build_alloy_install_command,
    _check_exporter_endpoint,
    _external_request_base_url,
    _get_asset_or_404,
    _upsert_target,
)
from app.api.monitoring.services.alloy_policy import (
    _agent_logs_write_url,
    _agent_metric_write_url,
    _agent_reachable_url,
    _agent_status_record,
    _asset_policy_labels,
    _collector_now_ts,
    _create_collector_register_token,
    _ensure_asset_alloy_policy,
    _hash_agent_token,
    _is_agent_recent,
    _latest_agent_for_asset,
    _log_collect_state,
    _metrics_collect_state,
    _policy_logs_json,
    _resolve_app_scope_for_asset,
    resync_alloy_policy_for_asset_ids,
)
from app.api.monitoring.services.log_targets import (
    _asset_log_target_record,
    _build_remote_vector_check_command,
    _build_remote_vector_disable_command,
    _format_remote_error,
    _normalize_log_targets_for_assets,
    _persist_log_targets,
    _run_asset_ssh,
    _sync_docker_log_policy,
)
from app.api.monitoring.services.deployment import (
    _validate_write_url_format,
    probe_agent_write_urls,
    resolve_agent_write_urls,
)

# ---------------------------------------------------------------------------
# 再导出：路由（router 定义在 routes.py，端点函数也显式再导出）
# ---------------------------------------------------------------------------
from app.api.monitoring.routes import (
    router,
    apply_alert_rule_templates,
    check_log_vector,
    check_monitoring_target,
    disable_log_vector,
    disable_monitoring_target,
    enable_monitoring_target,
    get_alert_rule_templates,
    get_host_metrics,
    get_log_targets,
    get_monitoring_config,
    get_monitoring_dashboard,
    get_monitoring_deployment_status,
    get_monitoring_notifications,
    get_monitoring_rules,
    get_monitoring_targets,
    get_vmagent_config,
    install_alloy_agent,
    install_log_vector,
    install_node_exporter,
    list_log_fields,
    query_victorialogs,
    run_monitoring_deployment_action,
    save_monitoring_config,
    save_monitoring_notifications,
    save_monitoring_rules,
    self_check_monitoring,
    test_monitoring_notification,
)
