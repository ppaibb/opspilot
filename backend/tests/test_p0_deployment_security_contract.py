"""P0 部署配置安全契约：默认值必须保持 fail-closed。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_root_compose_has_no_default_database_password():
    text = _read("docker-compose.yml")
    assert "change-this-db-password" not in text
    assert "${POSTGRES_PASSWORD:?" in text


def test_root_compose_disables_runtime_schema_creation():
    text = _read("docker-compose.yml")
    assert "DB_AUTO_CREATE_TABLES: '1'" not in text
    assert "DB_AUTO_CREATE_TABLES: '0'" in text
    assert "alembic upgrade head" in text
    assert "service_completed_successfully" in text


def test_development_grafana_is_private_and_rejects_untrusted_proxy_headers():
    text = _read("deploy/monitoring/docker-compose.yml")
    assert "grafana/grafana:11.6.14-security-04" in text
    assert "${GRAFANA_ADMIN_PASSWORD:?" in text
    assert "GF_AUTH_PROXY_WHITELIST: ${GRAFANA_AUTH_PROXY_WHITELIST:?" in text
    assert '"${GRAFANA_BIND:-127.0.0.1}:3000:3000"' in text
    assert '"3000:3000"' not in text


def test_offline_grafana_is_private_and_connected_by_internal_network():
    text = _read("deploy/offline/monitoring/docker-compose.yaml")
    assert "grafana/grafana:11.6.14-security-04" in text
    assert "GF_AUTH_PROXY_WHITELIST: ${GRAFANA_AUTH_PROXY_WHITELIST:?" in text
    assert '"${GRAFANA_BIND:-127.0.0.1}:${GRAFANA_PORT:-3000}:3000"' in text
    assert "opspilot-monitoring-bridge" in text

    app_text = _read("deploy/offline/app/docker-compose.yaml")
    assert "opspilot-monitoring-bridge" in app_text
    assert 'grafana_url="http://grafana:3000"' in _read("deploy/offline/install.sh")
    assert "GRAFANA_URL: ${GRAFANA_URL:-}" not in app_text


def test_installer_keeps_generated_secrets_private_and_never_prints_passwords():
    text = _read("deploy/offline/install.sh")
    assert "umask 077" in text
    assert 'chmod 600 "$ENV_FILE"' in text
    assert 'chmod 600 "$dest"' in text
    assert 'log "管理员:' not in text
    assert 'log "Grafana:' not in text
