"""高信号静态质量门禁。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_no_new_star_imports_outside_legacy_application_shims() -> None:
    allowed = {
        "backend/app/api/applications.py",
        "backend/app/api/application_routes/catalog.py",
        "backend/app/api/application_routes/configs.py",
        "backend/app/api/application_routes/releases.py",
        "backend/app/api/application_routes/runtime.py",
    }
    violations = []
    for path in (ROOT / "backend/app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed:
            continue
        if re.search(r"^\s*from\s+\S+\s+import\s+\*", path.read_text(encoding="utf-8"), re.MULTILINE):
            violations.append(relative)
    assert violations == []


def test_empty_exception_handlers_require_an_explanatory_comment() -> None:
    violations = []
    for path in (ROOT / "backend/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"except(?:\s+[^:]+)?:\s*\n\s+pass(?:\s*\n|$)", source):
            line = source.count("\n", 0, match.start()) + 1
            violations.append(f"{path.relative_to(ROOT)}:{line}")
    # 现存解析兼容分支后续逐项清理；门禁先固定基线，新增数量不得增长。
    assert len(violations) <= 82, violations
