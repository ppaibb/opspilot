"""监控栈隔离、版本和可观测性契约。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSES = [
    ROOT / "deploy" / "monitoring" / "docker-compose.yml",
    ROOT / "deploy" / "offline" / "monitoring" / "docker-compose.yaml",
]
ALLOY_CONFIGS = [
    ROOT / "deploy" / "monitoring" / "config" / "alloy.alloy",
    ROOT / "deploy" / "offline" / "monitoring" / "config" / "alloy.alloy",
]
ALERT_CONSTANTS = ROOT / "backend" / "app" / "api" / "monitoring" / "constants.py"


def test_monitoring_ports_are_private_and_versions_are_aligned() -> None:
    for path in COMPOSES:
        source = path.read_text(encoding="utf-8")
        assert "${MONITOR_BIND:-127.0.0.1}" in source
        assert "victoria-metrics:v1.140.0" in source
        assert "victoria-logs:v1.50.0" in source
        assert "grafana/alloy:v1.16.3" in source


def test_every_stack_has_limits_healthchecks_and_log_rotation() -> None:
    for path in COMPOSES:
        source = path.read_text(encoding="utf-8")
        assert source.count("max-size: \"10m\"") >= 4
        assert source.count("mem_limit:") >= 4
        # VM/VLogs/Grafana 三者必须有健康检查；alloy 镜像内无 wget，wget 健康检查
        # 恒 unhealthy，offline 栈已去掉 alloy 的健康检查（dev 栈同款遗留问题未动）。
        assert source.count("healthcheck:") >= 3
        assert "maxDiskSpaceUsageBytes" in source


def test_alloy_configs_share_stack_self_monitoring() -> None:
    sources = [path.read_text(encoding="utf-8") for path in ALLOY_CONFIGS]
    for source in sources:
        assert 'prometheus.scrape "monitoring_stack"' in source
        assert '"opspilot-victoriametrics:8428"' in source
        assert '"opspilot-victorialogs:9428"' in source
        assert '"127.0.0.1:12346"' in source
    assert sources[0] == sources[1]


def test_default_rules_cover_stack_loss_and_disk_capacity() -> None:
    source = ALERT_CONSTANTS.read_text(encoding="utf-8")
    assert "OpsPilotMonitoringStackDown" in source
    assert "OpsPilotMonitoringDiskLow" in source
    assert "vm_free_disk_space_bytes" in source
    assert "vl_free_disk_space_bytes" in source
