"""离线安装器 fail-closed 与制品校验契约。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "deploy" / "offline" / "install.sh"
PACKAGER = ROOT / "deploy" / "offline" / "package.sh"
MANIFEST = ROOT / "deploy" / "offline" / "manifest.example.json"


def test_installer_fails_on_health_and_validates_required_secrets() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'wait_http "http://127.0.0.1:${FRONTEND_PORT}/" "前端" 40' in source
    assert 'wait_http "http://127.0.0.1:${BACKEND_PORT}/api/settings/login-page" "后端" 60' in source
    assert "|| true" not in "\n".join(line for line in source.splitlines() if "wait_http" in line)
    assert "validate_configuration" in source
    assert "至少 24 个字符" in source
    assert "SECRET_KEY 与 CREDENTIAL_ENCRYPTION_KEY 不能相同" in source


def test_installer_uses_install_dir_and_private_backups() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert "prepare_install_directory" in source
    assert 'chmod 600 "$backup"' in source
    assert 'chmod 600 "$ENV_FILE"' in source


def test_manifest_is_valid_and_packager_emits_checksums() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert {"backend", "frontend", "postgres", "victoriametrics", "victorialogs", "grafana", "alloy"} <= set(manifest["images"])
    source = PACKAGER.read_text(encoding="utf-8")
    assert "manifest.json" in source
    assert "sha256" in source
    assert "docker image inspect" in source


def test_installer_reads_image_refs_and_digests_from_manifest() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert "verify_manifest" in source
    assert "sha256sum" in source
    assert "OPSPILOT_BACKEND_IMAGE" in source
    assert "OPSPILOT_FRONTEND_IMAGE" in source
