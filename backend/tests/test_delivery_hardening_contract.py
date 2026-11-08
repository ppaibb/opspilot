"""容器、CI 和破坏性操作保护契约。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_images_are_non_root_and_multistage() -> None:
    backend = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert backend.count("FROM ") >= 2
    assert "USER opspilot" in backend
    assert "HEALTHCHECK" in backend
    assert "nginx-unprivileged:1.27-alpine" in frontend
    assert "USER nginx" in frontend
    assert "HEALTHCHECK" in frontend
    assert "dangerouslyAllowAllBuilds" not in frontend


def test_root_compose_has_health_dependencies_and_restart_policy() -> None:
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert source.count("restart: unless-stopped") >= 3
    assert "condition: service_healthy" in source
    assert source.count("healthcheck:") >= 3


def test_ci_runs_linux_postgres_migrations_builds_and_audits() -> None:
    source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for marker in ("ubuntu-latest", "postgres:", "alembic upgrade head", "docker compose -f", "docker build", "pip-audit", "pnpm audit"):
        assert marker in source


def test_downgrade_requires_backup_and_explicit_confirmation() -> None:
    source = (ROOT / "scripts" / "guarded_alembic_downgrade.py").read_text(encoding="utf-8")
    assert "--backup-id" in source
    assert "--confirm" in source
    assert "I_HAVE_A_VERIFIED_BACKUP" in source
