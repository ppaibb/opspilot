"""生产迁移升级路径的静态契约。"""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = ROOT_DIR / "scripts" / "verify_alembic_full_upgrade.py"
LEGACY_FIXTURE = ROOT_DIR / "backend" / "tests" / "schema" / "legacy_schema.sql"


def test_upgrade_verifier_covers_empty_legacy_and_idempotent_paths() -> None:
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert '"empty": "opspilot_mig_check_empty"' in source
    assert '"legacy": "opspilot_mig_check_legacy"' in source
    assert "apply_legacy_fixture" in source
    assert "get_indexes" in source
    assert "get_foreign_keys" in source


def test_legacy_fixture_rolls_back_every_post_baseline_change() -> None:
    sql = LEGACY_FIXTURE.read_text(encoding="utf-8")
    for marker in (
        "cloud_account_id", "provider_zone_id", "snapshot_credential_id",
        "request_digest", "refresh_tokens", "login_attempts",
        "20260922_security_hardening_merge",
    ):
        assert marker in sql


def test_verifier_refuses_arbitrary_database_names() -> None:
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert source.count("拒绝操作非验证数据库") == 2
    assert 'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)' in source
