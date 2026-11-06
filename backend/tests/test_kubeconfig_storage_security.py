"""Kubeconfig 持久化与镜像隔离安全契约。"""
from __future__ import annotations

import os
from pathlib import Path

from app.api.clusters import _shared


def test_default_kubeconfig_dir_is_inside_persistent_storage():
    normalized = Path(_shared.KUBECONFIG_DIR).as_posix()
    assert normalized.endswith("/app/storage/kubeconfigs")


def test_backend_dockerignore_excludes_all_kubeconfig_locations():
    text = (Path(__file__).parents[1] / ".dockerignore").read_text(encoding="utf-8")
    assert "app/kubeconfigs" in text
    assert "app/storage/kubeconfigs" in text
    assert "**/kubeconfigs" in text


def test_legacy_kubeconfig_migrates_with_private_permissions(tmp_path):
    legacy = tmp_path / "legacy"
    target = tmp_path / "storage" / "kubeconfigs"
    legacy.mkdir()
    source = legacy / "c-old.yaml"
    source.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")

    _shared.migrate_legacy_kubeconfigs(legacy, target)

    migrated = target / source.name
    assert migrated.read_text(encoding="utf-8") == "apiVersion: v1\nkind: Config\n"
    assert not source.exists()
    if os.name != "nt":
        assert migrated.stat().st_mode & 0o777 == 0o600
        assert target.stat().st_mode & 0o777 == 0o700


def test_legacy_migration_never_overwrites_current_file(tmp_path):
    legacy = tmp_path / "legacy"
    target = tmp_path / "storage" / "kubeconfigs"
    legacy.mkdir()
    target.mkdir(parents=True)
    source = legacy / "c-same.yaml"
    destination = target / source.name
    source.write_text("legacy", encoding="utf-8")
    destination.write_text("current", encoding="utf-8")

    _shared.migrate_legacy_kubeconfigs(legacy, target)

    assert source.read_text(encoding="utf-8") == "legacy"
    assert destination.read_text(encoding="utf-8") == "current"


def test_legacy_migration_ignores_unexpected_names_and_symlinks(tmp_path):
    legacy = tmp_path / "legacy"
    target = tmp_path / "storage" / "kubeconfigs"
    legacy.mkdir()
    (legacy / "admin.yaml").write_text("not-managed", encoding="utf-8")
    external = tmp_path / "outside.yaml"
    external.write_text("outside", encoding="utf-8")
    link = legacy / "c-link.yaml"
    try:
        link.symlink_to(external)
    except OSError:
        link = None

    _shared.migrate_legacy_kubeconfigs(legacy, target)

    assert not (target / "admin.yaml").exists()
    if link is not None:
        assert not (target / link.name).exists()
