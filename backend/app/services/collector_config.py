"""
Alloy 采集配置渲染服务。

该模块只接收 OpsPilot 控制台产生的结构化策略，不接受用户直接提交
Alloy DSL，从源头降低配置注入和任意出站写入风险。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class CollectorConfigError(ValueError):
    """采集策略无法安全渲染为 Alloy 配置。"""


LABEL_KEY_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
COMPONENT_LABEL_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$")
TARGET_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]+:\d{1,5}$")
SAFE_LOG_PREFIXES = (
    "/var/log/",
    "/opt/",
    "/srv/",
    "/data/logs/",
)
DENIED_LOG_PREFIXES = (
    "/etc/",
    "/root/",
    "/home/",
    "/var/lib/",
    "/var/run/",
    "/run/",
)
HIGH_RISK_LABEL_KEYS = {
    "authorization",
    "cookie",
    "password",
    "request_id",
    "requestid",
    "secret",
    "session",
    "token",
}


def validate_label_key(key: str) -> str:
    """校验指标/日志标签键，避免高基数或敏感字段进入后端。"""
    value = str(key or "").strip()
    normalized = value.lower().replace("-", "_")
    if not LABEL_KEY_PATTERN.match(value):
        raise CollectorConfigError(f"非法标签键: {value}")
    if normalized in HIGH_RISK_LABEL_KEYS:
        raise CollectorConfigError(f"标签键不允许包含敏感或高基数字段: {value}")
    return value


def validate_log_path(path: str) -> str:
    """校验日志路径，只允许常见日志目录，禁止读取密钥和系统配置。"""
    value = str(path or "").strip().replace("\\", "/")
    if not value.startswith("/"):
        raise CollectorConfigError("日志路径必须是绝对路径")
    if any(value.startswith(prefix) for prefix in DENIED_LOG_PREFIXES):
        raise CollectorConfigError(f"日志路径不允许采集: {value}")
    if not any(value.startswith(prefix) for prefix in SAFE_LOG_PREFIXES):
        raise CollectorConfigError(f"日志路径不在白名单目录内: {value}")
    if ".." in value.split("/"):
        raise CollectorConfigError("日志路径不能包含上级目录跳转")
    return value


def validate_component_label(label: str, field_name: str) -> str:
    """校验 Alloy 组件标签，允许 job/service 使用短横线。"""
    value = str(label or "").strip()
    if not COMPONENT_LABEL_PATTERN.match(value):
        raise CollectorConfigError(f"非法{field_name}: {value}")
    return value


def config_hash(config: str) -> str:
    """生成配置哈希，Agent 只在哈希变化时重载。"""
    return hashlib.sha256(config.encode("utf-8")).hexdigest()


def render_alloy_config(
    *,
    agent_id: str,
    metrics: dict[str, Any] | None,
    logs: dict[str, Any] | None,
    labels: dict[str, str] | None,
    remote_write_url: str,
    vlogs_url: str = "",
    remote_write_auth: dict[str, str] | None = None,
    vlogs_auth: dict[str, str] | None = None,
) -> str:
    """将结构化采集策略渲染为受控 Alloy 配置。"""
    safe_agent_id = _safe_label_value(agent_id, "agent_id")
    safe_labels = _normalize_labels(labels or {})
    safe_labels["agent_id"] = safe_agent_id
    sections = [_render_header(safe_agent_id)]
    scrape_sections = _render_scrape_sections(metrics or {}, safe_labels)
    if scrape_sections:
        sections.append(_render_prometheus_remote_write(remote_write_url, remote_write_auth))
        sections.extend(scrape_sections)
    log_sections = _render_log_sections(logs or {}, safe_labels, vlogs_url, vlogs_auth)
    if log_sections:
        sections.extend(log_sections)
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


def render_alloy_module_config(
    *,
    agent_id: str,
    metrics: dict[str, Any] | None,
    logs: dict[str, Any] | None,
    labels: dict[str, str] | None,
    remote_write_url: str,
    vlogs_url: str = "",
    remote_write_auth: dict[str, str] | None = None,
    vlogs_auth: dict[str, str] | None = None,
) -> str:
    """将受控采集策略包装为 import.http 可加载的 Alloy module。

    `import.http` 拉取的远程内容必须声明 module，不能直接返回完整顶层
    Alloy 配置。这里复用既有结构化渲染，再缩进到 `declare "main"` 中，
    让主机本地 bootstrap 只需要实例化 `opspilot.main "default" {}`。
    """
    body = render_alloy_config(
        agent_id=agent_id,
        metrics=metrics,
        logs=logs,
        labels=labels,
        remote_write_url=remote_write_url,
        vlogs_url=vlogs_url,
        remote_write_auth=remote_write_auth,
        vlogs_auth=vlogs_auth,
    )
    return wrap_alloy_config_as_module(body)


def wrap_alloy_config_as_module(alloy_config: str) -> str:
    """把完整 Alloy 配置包装为 `declare "main"` module。"""
    body = str(alloy_config or "").strip()
    if body.startswith('declare "main"'):
        return body.rstrip() + "\n"
    indented = "\n".join(f"  {line}" if line else "" for line in body.splitlines())
    return "\n".join(["declare \"main\" {", indented, "}", ""])


def _normalize_labels(labels: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in labels.items():
        key = validate_label_key(raw_key)
        result[key] = _safe_label_value(raw_value, key)
    return dict(sorted(result.items()))


def _safe_label_value(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CollectorConfigError(f"{field_name} 不能为空")
    if len(text) > 128:
        raise CollectorConfigError(f"{field_name} 长度不能超过 128")
    return text


def _render_header(agent_id: str) -> str:
    return "\n".join(
        [
            "// 该文件由 OpsPilot 自动生成，请通过控制台修改采集策略。",
            f"// agent_id: {_quote(agent_id)}",
        ]
    )


def _render_prometheus_remote_write(remote_write_url: str, auth: dict[str, str] | None = None) -> str:
    url = str(remote_write_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise CollectorConfigError("VictoriaMetrics remote_write 地址必须是 HTTP(S)")
    auth_lines = _render_endpoint_auth_lines(auth)
    return "\n".join(
        [
            'prometheus.remote_write "opspilot" {',
            "  endpoint {",
            f"    url = {_quote(url)}",
            *[f"    {line}" for line in auth_lines],
            "  }",
            "}",
        ]
    )


def _render_scrape_sections(metrics: dict[str, Any], labels: dict[str, str]) -> list[str]:
    sections: list[str] = []
    if bool(metrics.get("node")):
        relabel_rules = []
        for key, value in labels.items():
            relabel_rules.extend(
                [
                    "  rule {",
                    f"    target_label = {_quote(key)}",
                    f"    replacement   = {_quote(value)}",
                    "  }",
                ]
            )
        relabel_rules.extend(
            [
                "  rule {",
                '    target_label = "job"',
                '    replacement   = "opspilot-node"',
                "  }",
            ]
        )
        sections.append(
            "\n".join(
                [
                    'prometheus.exporter.unix "local" {',
                    "}",
                    "",
                    'discovery.relabel "node" {',
                    "  targets = prometheus.exporter.unix.local.targets",
                    *relabel_rules,
                    "}",
                    "",
                    'prometheus.scrape "node" {',
                    "  targets = discovery.relabel.node.output",
                    "  forward_to = [prometheus.remote_write.opspilot.receiver]",
                    "}",
                ]
            )
        )
    for index, item in enumerate(metrics.get("scrape_targets") or []):
        job = validate_component_label(str(item.get("job") or f"job_{index}"), "采集任务名")
        targets = [_validate_target(target) for target in item.get("targets") or []]
        if not targets:
            continue
        target_lines = _render_target_list(targets, labels)
        sections.append(
            "\n".join(
                [
                    f'prometheus.scrape "{job}" {{',
                    "  targets = [",
                    *target_lines,
                    "  ]",
                    "  forward_to = [prometheus.remote_write.opspilot.receiver]",
                    "}",
                ]
            )
        )
    return sections


def _render_log_sections(
    logs: dict[str, Any],
    labels: dict[str, str],
    vlogs_url: str,
    auth: dict[str, str] | None = None,
) -> list[str]:
    files = list(logs.get("files") or [])
    if bool(logs.get("system")):
        files.extend(
            [
                {"path": "/var/log/syslog", "service": "system"},
                {"path": "/var/log/messages", "service": "system"},
                {"path": "/var/log/auth.log", "service": "auth"},
                {"path": "/var/log/secure", "service": "auth"},
            ]
        )
    if not files:
        files = []
    sections: list[str] = []
    source_names: list[str] = []
    for index, item in enumerate(files):
        path = validate_log_path(str(item.get("path") or ""))
        service = validate_component_label(str(item.get("service") or f"log_{index}"), "日志服务名")
        source_name = f"{service}_{index}"
        source_names.append(source_name)
        sections.append(
            "\n".join(
                [
                    f'local.file_match "{source_name}" {{',
                    "  path_targets = [{",
                    f"    __path__ = {_quote(path)},",
                    *[f"    {key} = {_quote(value)}," for key, value in labels.items()],
                    f"    service = {_quote(service)},",
                    "  }]",
                    "}",
                    "",
                    f'loki.source.file "{source_name}" {{',
                    f"  targets = local.file_match.{source_name}.targets",
                    "  forward_to = [loki.write.victorialogs.receiver]",
                    "}",
                ]
            )
        )
    if bool(logs.get("docker_containers")):
        # 产品决策：Docker 日志走 loki.source.docker（Docker API），
        # 不再 tail /var/lib/docker/containers/*/*-json.log。
        # 前置条件：Alloy 进程可读 unix:///var/run/docker.sock。
        # Alloy 1.16.3 的 loki.source.docker 必填 targets，需先 discovery.docker。
        source_names.append("docker_containers")
        label_lines = [f"    {key} = {_quote(value)}," for key, value in labels.items()]
        sections.append(
            "\n".join(
                [
                    'discovery.docker "docker_containers" {',
                    '  host = "unix:///var/run/docker.sock"',
                    "}",
                    "",
                    'loki.source.docker "docker_containers" {',
                    '  host = "unix:///var/run/docker.sock"',
                    "  targets = discovery.docker.docker_containers.targets",
                    "  labels = {",
                    '    job = "container",',
                    *label_lines,
                    "  }",
                    "  forward_to = [loki.write.victorialogs.receiver]",
                    "}",
                ]
            )
        )
    if not source_names:
        return []
    endpoint = str(vlogs_url or "").strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise CollectorConfigError("VictoriaLogs 地址必须是 HTTP(S)")
    auth_lines = _render_endpoint_auth_lines(auth)
    sections.append(
        "\n".join(
            [
                'loki.write "victorialogs" {',
                "  endpoint {",
                f"    url = {_quote(endpoint)}",
                *[f"    {line}" for line in auth_lines],
                "  }",
                "}",
            ]
        )
    )
    return sections


def _render_target_list(targets: list[str], labels: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for target in targets:
        lines.append("    {")
        lines.append(f'      "__address__" = {_quote(target)},')
        for key, value in labels.items():
            lines.append(f"      {key} = {_quote(value)},")
        lines.append("    },")
    return lines


def _validate_target(target: Any) -> str:
    value = str(target or "").strip()
    if not TARGET_PATTERN.match(value):
        raise CollectorConfigError(f"非法采集目标: {value}")
    port = int(value.rsplit(":", 1)[1])
    if port < 1 or port > 65535:
        raise CollectorConfigError(f"采集目标端口超出范围: {value}")
    return value


def _quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_endpoint_auth_lines(auth: dict[str, str] | None) -> list[str]:
    """渲染 Alloy endpoint 认证配置。

    认证来源只能来自 OpsPilot 已保存并解密后的监控连接配置，不能由前端直接提交
    Alloy 片段。这样既能支持 Grafana Cloud / 托管 VM 的 basic 或 bearer 写入，
    也避免用户借认证字段注入任意 Alloy DSL。
    """
    if not auth:
        return []
    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type == "basic":
        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        if not username or not password:
            return []
        return [
            "basic_auth {",
            f"      username = {_quote(username)}",
            f"      password = {_quote(password)}",
            "    }",
        ]
    if auth_type == "bearer":
        token = str(auth.get("token") or "").strip()
        if token:
            return [f"bearer_token = {_quote(token)}"]
    return []
