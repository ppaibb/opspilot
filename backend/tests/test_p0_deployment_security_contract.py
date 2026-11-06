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
