"""监控告警模块常量与默认配置。

【设计权衡】
将常量从业务逻辑中抽离为独立模块，目的是：
1. 打破循环依赖：services/*.py 之间互相引用时，常量可作为最底层被安全导入；
2. 统一收口：默认规则、查询 PromQL、文件路径全部集中一处，后续调整阈值或路径时只需改这里；
3. 测试友好：测试代码可直接导入 DEFAULT_ALERT_RULES 做断言，无需初始化整个 FastAPI 上下文。
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# 确保在解析常量路径前，先加载 backend/.env 中的自定义部署路径
_env_path = Path(__file__).resolve().parents[3] / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

CONFIG_KEY = "victoriametrics_config"
TARGETS_CONFIG_KEY = "monitoring_targets"
LOG_TARGETS_CONFIG_KEY = "log_targets"
RULES_CONFIG_KEY = "monitoring_rules"
NOTIFICATIONS_CONFIG_KEY = "monitoring_notifications"
AUTH_TYPES = {"none", "basic", "bearer"}
ALERT_SEVERITIES = {"critical", "warning", "info"}
NOTIFICATION_TYPES = {"webhook"}
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_STEP_SECONDS = 60
DEFAULT_NODE_EXPORTER_PORT = 9100
MAX_LOG_QUERY_LENGTH = 500
MAX_LOG_LIMIT = 1000
def _resolve_repo_root() -> Path:
    """
    解析 OpsPilot 仓库根目录。

    设计原因：
    - 本地开发时，`constants.py` 位于 `backend/app/api/monitoring`，向上 4 层是仓库根；
    - Docker 镜像中后端代码位于 `/app/app/api/monitoring`，向上 4 层会变成 `/`；
    - 部署状态页需要读取 `deploy/monitoring/docker-compose.yml`，因此必须允许通过
      环境变量或 `/app` 回退路径定位部署目录，避免误报“监控部署套件未安装”。
    """
    env_root = os.getenv("OPSPILOT_REPO_ROOT", "").strip()
    candidates = [
        Path(env_root) if env_root else None,
        Path(__file__).resolve().parents[4],
        Path("/app"),
        Path.cwd(),
    ]
    for candidate in candidates:
        if candidate and (candidate / "deploy" / "monitoring" / "docker-compose.yml").exists():
            return candidate
    return Path(env_root) if env_root else Path(__file__).resolve().parents[4]


REPO_ROOT = _resolve_repo_root()
MONITORING_DEPLOY_DIR = Path(os.getenv("OPSPILOT_MONITORING_DEPLOY_DIR", "")).expanduser() if os.getenv("OPSPILOT_MONITORING_DEPLOY_DIR", "").strip() else REPO_ROOT / "deploy" / "monitoring"
MONITORING_COMPOSE_FILE = MONITORING_DEPLOY_DIR / "docker-compose.yml"
VMAGENT_CONFIG_FILE = MONITORING_DEPLOY_DIR / "config" / "alloy.alloy"
ALERT_RULES_FILE = MONITORING_DEPLOY_DIR / "config" / "alerts.yml"
REMOTE_VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
ALLOY_SYNC_SERVICE = "opspilot-alloy-sync"
ALLOY_DEFAULT_POLICY_PREFIX = "asset-alloy-"
DEPLOYMENT_ACTIONS = {"status", "start", "stop", "restart", "apply-local-config"}
RELOADABLE_MONITORING_SERVICES = {"alloy"}
VMAGENT_LEGACY_MARKER = "  # OpsPilot 会通过 /api/monitoring/vmagent-config 导出资产 target 片段。"
VMAGENT_MANAGED_BEGIN = "// ===== OpsPilot managed targets begin ====="
VMAGENT_MANAGED_END = "// ===== OpsPilot managed targets end ====="
DEFAULT_RULE_GROUP_NAME = "opspilot-managed"

# 默认告警规则（新环境首次加载时使用）。
# 仅保留 3 条最基础的节点级规则，原因：
# - 规则过多会导致新用户首次进入页面信息过载；
# - 磁盘、网络、证书等高级规则放在"规则模板库"中供用户按需启用；
# - 所有默认规则的 channel_ids 为空，避免未配置通知渠道时产生无效告警噪音。
DEFAULT_ALERT_RULES = [
    {
        "id": "rule-target-down",
        "name": "OpsPilotTargetDown",
        "expr": "up == 0",
        "duration": "2m",
        "severity": "warning",
        "enabled": True,
        "channel_ids": [],
        "summary": "监控目标不可达",
        "description": "{{ $labels.job }} {{ $labels.instance }} 已连续 2 分钟不可达",
    },
    {
        "id": "rule-node-cpu-high",
        "name": "OpsPilotNodeCpuHigh",
        "expr": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85',
        "duration": "5m",
        "severity": "warning",
        "enabled": True,
        "channel_ids": [],
        "summary": "主机 CPU 使用率过高",
        "description": "{{ $labels.instance }} CPU 使用率超过 85%",
    },
    {
        "id": "rule-node-memory-high",
        "name": "OpsPilotNodeMemoryHigh",
        "expr": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 > 90",
        "duration": "5m",
        "severity": "warning",
        "enabled": True,
        "channel_ids": [],
        "summary": "主机内存使用率过高",
        "description": "{{ $labels.instance }} 内存使用率超过 90%",
    },
]

DASHBOARD_RANGE_QUERIES = {
    "cpu": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "cpu_p95": 'quantile(0.95, 100 - (rate(node_cpu_seconds_total{mode="idle"}[5m]) * 100))',
    "memory": "(1 - (sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))) * 100",
    "memory_p95": 'quantile(0.95, (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)',
    "disk": 'avg((1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|squashfs|iso9660"} / node_filesystem_size_bytes{fstype!~"tmpfs|squashfs|iso9660"})) * 100)',
    "disk_p95": 'quantile(0.95, (1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|squashfs|iso9660"} / node_filesystem_size_bytes{fstype!~"tmpfs|squashfs|iso9660"})) * 100)',
    "networkIn": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    "networkOut": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    # 2026 实时看板补维度：Load1 / 磁盘读写吞吐（按主机盘聚合后再全局求和，单位 MB/s）
    "load1": "avg(node_load1)",
    "diskRead": (
        'sum(rate(node_disk_read_bytes_total{device!~"loop.*|ram.*"}[5m])) / 1024 / 1024'
    ),
    "diskWrite": (
        'sum(rate(node_disk_written_bytes_total{device!~"loop.*|ram.*"}[5m])) / 1024 / 1024'
    ),
}

# 实时看板摘要与文件系统 Top 占用查询（只增不减，失败时字段留空）
LOAD1_AVG_QUERY = "avg(node_load1)"
# Top5 文件系统占用率：过滤 tmpfs/squashfs/iso9660，topk 按已用% 取前 5
FS_USED_PCT_TOP_QUERY = (
    'topk(5, (1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|squashfs|iso9660"} '
    '/ node_filesystem_size_bytes{fstype!~"tmpfs|squashfs|iso9660"})) * 100)'
)
FS_SIZE_QUERY = 'node_filesystem_size_bytes{fstype!~"tmpfs|squashfs|iso9660"}'
# 磁盘 IO 设备过滤：跳过 loop/ram 虚拟盘，避免噪声
DISK_IO_DEVICE_FILTER = 'device!~"loop.*|ram.*"'

MIN_DASHBOARD_RANGE_MINUTES = 1
MAX_DASHBOARD_RANGE_MINUTES = 30 * 24 * 60  # 30 days (43200 minutes)
MIN_DASHBOARD_STEP_SECONDS = 10
MAX_DASHBOARD_STEP_SECONDS = 86400  # 24 hours (86400 seconds)

# ---------------------------------------------------------------------------
# 告警规则模板库（20+ 条，覆盖系统/磁盘/网络/进程/容器/证书六大类）。
# 【设计说明】
# 1. 所有表达式尽量基于 node_exporter 标准指标，保证开箱即用；
# 2. 需要额外 Exporter（cadvisor、blackbox_exporter）的规则在 required_exporter 中标注，
#    前端展示时会提示用户先部署对应组件；
# 3. 阈值选取兼顾"避免误报"与"不过度宽松"，用户应用后仍可自行调整；
# 4. 模板规则默认 enabled=False，批量创建后不会立即触发告警风暴。
# ---------------------------------------------------------------------------

ALERT_RULE_TEMPLATES = [
    # ===== system =====
    {
        "id": "tpl-node-load-high",
        "category": "system",
        "name": "OpsPilotNodeLoadHigh",
        "expr": "node_load1 > 8",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机负载过高",
        "description": "{{ $labels.instance }} 1分钟平均负载超过 8",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-swap-high",
        "category": "system",
        "name": "OpsPilotNodeSwapHigh",
        "expr": "node_memory_SwapTotal_bytes > 0 and (1 - (node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)) * 100 > 80",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机 Swap 使用率过高",
        "description": "{{ $labels.instance }} Swap 使用率超过 80%",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-uptime-high",
        "category": "system",
        "name": "OpsPilotNodeUptimeHigh",
        "expr": "time() - node_boot_time_seconds > 31536000",
        "duration": "1h",
        "severity": "info",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机运行时间超过 365 天",
        "description": "{{ $labels.instance }} 已连续运行超过 365 天，建议安排重启以应用内核/安全更新",
        "required_exporter": "",
    },
    # ===== disk =====
    {
        "id": "tpl-node-disk-full",
        "category": "disk",
        "name": "OpsPilotNodeDiskFull",
        "expr": "node_filesystem_avail_bytes{fstype!~\"tmpfs|squashfs|iso9660\"} / node_filesystem_size_bytes{fstype!~\"tmpfs|squashfs|iso9660\"} < 0.1",
        "duration": "5m",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机磁盘空间不足",
        "description": "{{ $labels.instance }} 分区 {{ $labels.mountpoint }} 可用空间不足 10%",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-disk-io-high",
        "category": "disk",
        "name": "OpsPilotNodeDiskIOHigh",
        "expr": "avg by(instance) (rate(node_disk_io_time_seconds_total[5m])) > 0.8",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机磁盘 IO 使用率过高",
        "description": "{{ $labels.instance }} 磁盘 IO 时间占比超过 80%",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-disk-readonly",
        "category": "disk",
        "name": "OpsPilotNodeDiskReadonly",
        "expr": "node_filesystem_readonly{fstype!~\"tmpfs|squashfs|iso9660\"} == 1",
        "duration": "1m",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机磁盘变为只读",
        "description": "{{ $labels.instance }} 分区 {{ $labels.mountpoint }} 已变为只读，通常由文件系统损坏或底层存储故障引起",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-inode-full",
        "category": "disk",
        "name": "OpsPilotNodeInodeFull",
        "expr": "node_filesystem_files_free{fstype!~\"tmpfs|squashfs|iso9660\"} / node_filesystem_files{fstype!~\"tmpfs|squashfs|iso9660\"} < 0.1",
        "duration": "5m",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机 inode 不足",
        "description": "{{ $labels.instance }} 分区 {{ $labels.mountpoint }} 可用 inode 不足 10%",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-disk-predict-full",
        "category": "disk",
        "name": "OpsPilotNodeDiskPredictFull",
        "expr": "predict_linear(node_filesystem_avail_bytes{fstype!~\"tmpfs|squashfs|iso9660\"}[1h], 4 * 3600) < 0",
        "duration": "1h",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机磁盘预测将在 4 小时内写满",
        "description": "{{ $labels.instance }} 分区 {{ $labels.mountpoint }} 按最近 1 小时趋势推算将在 4 小时内耗尽",
        "required_exporter": "",
    },
    # ===== network =====
    {
        "id": "tpl-node-network-drop",
        "category": "network",
        "name": "OpsPilotNodeNetworkDrop",
        "expr": "rate(node_network_receive_drop_total{device!~\"lo|docker.*|veth.*|br.*\"}[5m]) > 0",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机网络丢包",
        "description": "{{ $labels.instance }} 网卡 {{ $labels.device }} 存在丢包",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-network-err",
        "category": "network",
        "name": "OpsPilotNodeNetworkErr",
        "expr": "rate(node_network_receive_errs_total{device!~\"lo|docker.*|veth.*|br.*\"}[5m]) > 0",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机网络错误",
        "description": "{{ $labels.instance }} 网卡 {{ $labels.device }} 存在接收错误",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-tcp-connections-high",
        "category": "network",
        "name": "OpsPilotNodeTcpConnectionsHigh",
        "expr": "node_netstat_Tcp_CurrEstab > 10000",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机 TCP 连接数过高",
        "description": "{{ $labels.instance }} 当前 established TCP 连接数超过 10000",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-bandwidth-high",
        "category": "network",
        "name": "OpsPilotNodeBandwidthHigh",
        "expr": "(rate(node_network_receive_bytes_total{device!~\"lo|docker.*|veth.*|br.*\"}[5m]) + rate(node_network_transmit_bytes_total{device!~\"lo|docker.*|veth.*|br.*\"}[5m])) / 1024 / 1024 / 1024 > 1",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机带宽使用率过高",
        "description": "{{ $labels.instance }} 总带宽（收+发）超过 1GB/s",
        "required_exporter": "",
    },
    # ===== process =====
    {
        "id": "tpl-node-zombies-high",
        "category": "process",
        "name": "OpsPilotNodeZombiesHigh",
        "expr": "node_processes_state{state=\"Z\"} > 10",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机僵尸进程过多",
        "description": "{{ $labels.instance }} 僵尸进程数超过 10",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-process-count-high",
        "category": "process",
        "name": "OpsPilotNodeProcessCountHigh",
        "expr": "node_processes_state > 5000",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机进程总数过多",
        "description": "{{ $labels.instance }} 进程总数超过 5000",
        "required_exporter": "",
    },
    # ===== container =====
    {
        "id": "tpl-container-restart-frequent",
        "category": "container",
        "name": "OpsPilotContainerRestartFrequent",
        "expr": "increase(container_start_time_seconds{name!~\".*pause.*|.*POD.*\"}[1h]) > 2",
        "duration": "5m",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "容器重启频繁",
        "description": "{{ $labels.name }} 在 1 小时内重启超过 2 次",
        "required_exporter": "cadvisor",
    },
    {
        "id": "tpl-container-cpu-high",
        "category": "container",
        "name": "OpsPilotContainerCpuHigh",
        "expr": "rate(container_cpu_usage_seconds_total{name!~\".*pause.*|.*POD.*\"}[5m]) > 0.8",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "容器 CPU 使用率过高",
        "description": "{{ $labels.name }} CPU 使用率超过 80%",
        "required_exporter": "cadvisor",
    },
    {
        "id": "tpl-container-memory-high",
        "category": "container",
        "name": "OpsPilotContainerMemoryHigh",
        "expr": "container_memory_usage_bytes{name!~\".*pause.*|.*POD.*\"} / ignoring(id) container_spec_memory_limit_bytes{name!~\".*pause.*|.*POD.*\"} > 0.9",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "容器内存使用率过高",
        "description": "{{ $labels.name }} 内存使用率超过 90%",
        "required_exporter": "cadvisor",
    },
    {
        "id": "tpl-container-oom",
        "category": "container",
        "name": "OpsPilotContainerOOM",
        "expr": "increase(container_memory_failures_total{name!~\".*pause.*|.*POD.*\"}[5m]) > 0",
        "duration": "1m",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "容器发生 OOM",
        "description": "{{ $labels.name }} 发生内存分配失败（OOM）",
        "required_exporter": "cadvisor",
    },
    # ===== cert =====
    {
        "id": "tpl-ssl-cert-expire-soon",
        "category": "cert",
        "name": "OpsPilotSslCertExpireSoon",
        "expr": "ssl_certificate_expiry_seconds / 86400 < 30",
        "duration": "1h",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "SSL 证书即将过期",
        "description": "{{ $labels.instance }} SSL 证书将在 30 天内过期",
        "required_exporter": "blackbox_exporter",
    },
    {
        "id": "tpl-ssl-cert-expired",
        "category": "cert",
        "name": "OpsPilotSslCertExpired",
        "expr": "ssl_certificate_expiry_seconds < 0",
        "duration": "1h",
        "severity": "critical",
        "enabled": False,
        "channel_ids": [],
        "summary": "SSL 证书已过期",
        "description": "{{ $labels.instance }} SSL 证书已过期",
        "required_exporter": "blackbox_exporter",
    },
    # ===== hardware =====
    {
        "id": "tpl-node-clock-drift",
        "category": "system",
        "name": "OpsPilotNodeClockDrift",
        "expr": "abs(node_timex_offset_seconds) > 0.1",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机时钟漂移",
        "description": "{{ $labels.instance }} NTP 时间偏移超过 0.1 秒",
        "required_exporter": "",
    },
    {
        "id": "tpl-node-temperature-high",
        "category": "system",
        "name": "OpsPilotNodeTemperatureHigh",
        "expr": "node_hwmon_temp_celsius > 80",
        "duration": "5m",
        "severity": "warning",
        "enabled": False,
        "channel_ids": [],
        "summary": "主机硬件温度过高",
        "description": "{{ $labels.instance }} 传感器 {{ $labels.sensor }} 温度超过 80°C",
        "required_exporter": "",
    },
]

TOP_HOST_QUERIES = {
    "cpu": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "memory": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    "network": (
        'sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|veth.*|br.*"}[5m]) '
        '+ rate(node_network_transmit_bytes_total{device!~"lo|docker.*|veth.*|br.*"}[5m])) / 1024 / 1024'
    ),
    "disk": 'max by (instance) ((1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|squashfs|iso9660"} / node_filesystem_size_bytes{fstype!~"tmpfs|squashfs|iso9660"})) * 100)',
}
