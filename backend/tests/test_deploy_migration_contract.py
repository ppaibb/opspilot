"""部署链路只能通过 Alembic 变更生产 schema。"""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
COMPOSE = ROOT_DIR / "deploy" / "offline" / "app" / "docker-compose.yaml"
UPDATE_SCRIPT = ROOT_DIR / "deploy" / "update" / "deploy_update.sh"
ALEMBIC_ENV = ROOT_DIR / "backend" / "alembic" / "env.py"


def test_init_container_runs_upgrade_before_backend() -> None:
    source = COMPOSE.read_text(encoding="utf-8")
    assert "alembic upgrade head" in source
    assert "service_completed_successfully" in source
    assert "Base.metadata.create_all" not in source
    assert "alembic stamp" not in source


def test_update_migrates_before_switch_and_rolls_back_on_failure() -> None:
    source = UPDATE_SCRIPT.read_text(encoding="utf-8")
    assert "COMPOSE run --rm --no-deps init-db" in source
    migration_position = source.index("COMPOSE run --rm --no-deps init-db")
    service_switch_position = source.index("COMPOSE up -d ${SERVICES}")
    assert migration_position < service_switch_position
    assert "rollback_deploy" in source
    assert "trap rollback_deploy ERR" in source
    assert ".env.pre-update" in source
    assert "curl -fsS" in source


def test_deploy_files_do_not_fake_migration_state() -> None:
    for path in (COMPOSE, UPDATE_SCRIPT):
        source = path.read_text(encoding="utf-8")
        assert "stamp head" not in source
        assert "create_all" not in source


def test_alembic_compares_types_and_defaults_transactionally() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    assert "compare_type=True" in source
    assert "compare_server_default=True" in source
    assert "transaction_per_migration=True" in source
