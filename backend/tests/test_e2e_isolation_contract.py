"""保证默认测试不会连接并写入开发数据库。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_external_e2e_is_opt_in_and_has_no_hardcoded_password() -> None:
    source = (ROOT / "backend/tests/test_e2e_api.py").read_text(encoding="utf-8")
    assert 'RUN_EXTERNAL_E2E") != "1"' in source
    assert "E2E_ADMIN_PASSWORD" in source
    assert "Admin@12345" not in source
    assert "View@12345" not in source
    assert "Dba@12345" not in source
    assert "Ops@12345" not in source
