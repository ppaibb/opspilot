"""前端轮询、筛选与大列表渲染的静态契约。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_deployment_success_callback_has_task_guard() -> None:
    source = (ROOT / "frontend/src/components/DeploymentProgressModal.tsx").read_text(encoding="utf-8")
    assert "notifiedTaskRef.current !== task.task_id" in source
    assert "notifiedTaskRef.current = task.task_id" in source


def test_domain_and_certificate_filters_reach_server() -> None:
    api = (ROOT / "frontend/src/api/index.ts").read_text(encoding="utf-8")
    domains = (ROOT / "backend/app/api/domains.py").read_text(encoding="utf-8")
    certificates = (ROOT / "backend/app/api/certificates.py").read_text(encoding="utf-8")
    assert api.count("group_id?: number") >= 2
    assert "RESOURCE_TYPE_DOMAIN" in domains
    assert "RESOURCE_TYPE_CERT" in certificates


def test_log_table_uses_bounded_pages() -> None:
    source = (ROOT / "frontend/src/pages/monitoring/logs/index.tsx").read_text(encoding="utf-8")
    assert "defaultPageSize: 100" in source


def test_host_page_uses_summary_instead_of_duplicate_full_list() -> None:
    source = (ROOT / "frontend/src/pages/assets/hosts/index.tsx").read_text(encoding="utf-8")
    assert "queryFn: assetsApi.summary" in source
    assert "queryFn: assetsApi.list," not in source
