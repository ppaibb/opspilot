"""权限与交互整改的静态契约。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_credential_export_uses_selected_rows_only() -> None:
    source = (ROOT / "frontend/src/pages/assets/credentials/index.tsx").read_text(encoding="utf-8")
    assert "selectedKeys.includes(credential.cred_id)" in source
    assert "请先勾选要导出的凭证" in source
    assert "label: r.title" in source


def test_local_notifications_have_ttl_cap_and_rollback() -> None:
    source = (ROOT / "frontend/src/store/notifications.ts").read_text(encoding="utf-8")
    assert "LOCAL_NOTIFICATION_TTL_MS" in source
    assert "MAX_LOCAL_NOTIFICATIONS" in source
    assert "const previous = get().items.find" in source
    assert "item.id === id ? previous : item" in source


def test_domain_acme_credential_round_trips() -> None:
    frontend = (ROOT / "frontend/src/pages/assets/domains/index.tsx").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/api/domains.py").read_text(encoding="utf-8")
    assert "dns_credential: dom.dns_credential" in frontend
    assert "dns_credential: str | None = None" in backend
    assert "ensure_credential_access" in backend
