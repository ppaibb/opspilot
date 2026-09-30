#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpsPilot 统一验证入口（仓库根 检查-全量.bat 的实际逻辑都在这里）。

用法（在仓库根目录执行）：
    .venv\\Scripts\\python.exe scripts\\run_checks.py [--secret-scan] [--only-secret]

步骤：
    1. 后端 pytest        backend\\.venv\\Scripts\\python.exe -m pytest -q   （在 backend 目录）
    2. pyflakes 未定义名  根 .venv 的 python -m pyflakes backend/app
                          输出含 "undefined name" 判失败；
                          "unable to detect undefined names"（star-import 放弃提示）不算失败
    3. 前端 lint          pnpm --dir frontend run lint   （eslint error 才失败，warning 不失败）
    4. 前端 build         pnpm --dir frontend run build
    5. 秘密扫描           --secret-scan 开启（默认跳过），只报告不修改不轮换

秘密扫描口径（重要）：
    - 扫描范围：frontend/src、backend/app、backend/alembic、scripts，另加 backend/.env
    - 已知秘密位置（backend/.env、backend/app/kubeconfigs/）正常会命中——如实报告
      （只打印 文件:行号 + 规则名，绝不打印命中行内容），但**不计入失败**；
      其余位置出现命中即失败（退出码 1）
    - 排除目录/文件：node_modules、.venv、dist、.git、__pycache__、pnpm-lock.yaml 等
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
BACKEND_PY = BACKEND / ".venv" / "Scripts" / "python.exe"
ROOT_PY = ROOT / ".venv" / "Scripts" / "python.exe"

MAX_FILE_BYTES = 2 * 1024 * 1024  # 超过 2MB 的文件不扫（基本只可能是产物/数据）
BINARY_SNIFF_BYTES = 8192

# ---------------------------------------------------------------- 秘密扫描配置

# 已知本身就该有秘密的位置：报告但不失败（见模块 docstring 的口径说明）
KNOWN_SECRET_FILES = [BACKEND / ".env"]
KNOWN_SECRET_DIRS = [BACKEND / "app" / "kubeconfigs"]

SECRET_SCAN_PATHS = [
    FRONTEND / "src",
    BACKEND / "app",
    BACKEND / "alembic",
    ROOT / "scripts",
] + KNOWN_SECRET_FILES

EXCLUDED_DIR_NAMES = {
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "coverage",
}
EXCLUDED_FILE_NAMES = {
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
}

# 只匹配、只报告位置，绝不打印命中行内容
SECRET_RULES = [
    # PEM 私钥块起始行（RSA / OPENSSH / PKCS8 / ENCRYPTED 等变体都长这样）
    ("private-key-block", re.compile(r"BEGIN.{0,60}PRIVATE[ ]KEY")),
    # 阿里云 AccessKey Id 固定前缀 LTAI
    ("aliyun-access-key", re.compile(r"LTAI[A-Za-z0-9]{6,}")),
    # kubeconfig 里 user 的 token: 后跟长 base64（JWT/ opaque token）
    ("kubeconfig-token", re.compile(r"token:\s*['\"]?[A-Za-z0-9+/_=-]{40,}")),
    # api_secret / app_secret 之类的高熵字面量赋值（秘钥字段名本身不算）
    (
        "hardcoded-secret-assignment",
        re.compile(r"(?:api[_-]?secret|app[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9]{8,}"),
    ),
]

# --------------------------------------------------------------------- 工具函数


def _child_env() -> dict:
    env = dict(os.environ)
    # 根 .venv 是 uv 托管的 Python，其启动器会向子进程注入 PYTHONHOME /
    # UV_INTERNAL__PYTHONHOME；不剔除的话 backend venv（另一个基线解释器）
    # 会去加载 uv 那套 stdlib，直接在 init_import_site 崩溃。
    for leaked in ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME", "PYTHONPATH"):
        env.pop(leaked, None)
    # 子进程统一 UTF-8 输出，避免 GBK 控制台解码问题
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _print_tail(text: str, tail: int) -> None:
    lines = [ln for ln in text.splitlines()]
    if len(lines) > tail:
        print(f"  ...（输出共 {len(lines)} 行，仅显示末尾 {tail} 行）")
        lines = lines[-tail:]
    for ln in lines:
        print(f"  | {ln}")


def run_step(
    title: str,
    cmd,  # list[str] 或 (str, shell=True) 元组
    cwd: Path,
    timeout: int,
    tail_pass: int = 12,
    tail_fail: int = 80,
    ignore_exit: bool = False,
) -> tuple[bool, str]:
    print()
    print("=" * 72)
    print(f"[STEP] {title}")
    print("=" * 72)
    shell = False
    if isinstance(cmd, tuple):
        cmd, shell = cmd  # type: ignore[misc]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            shell=shell,
            timeout=timeout,
            env=_child_env(),
        )
        output = _decode(proc.stdout) + _decode(proc.stderr)
        ok = proc.returncode == 0
        detail = f"退出码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        output = ""
        ok = False
        detail = f"超时（>{timeout}s）"
    except FileNotFoundError as exc:
        output = ""
        ok = False
        detail = f"命令不可用：{exc}"
    elapsed = time.time() - started
    _print_tail(output, tail_pass if ok or ignore_exit else tail_fail)
    if ignore_exit:
        # 退出码不参与判定（如 pyflakes 有任何提示都返回 1），由调用方按输出判定
        print(f"[INFO] {title} —— {detail}，耗时 {elapsed:.1f}s（退出码不参与判定）")
        return True, output
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {title} —— {detail}，耗时 {elapsed:.1f}s")
    return ok, output


def _require(cond: bool, message: str) -> tuple[bool, str]:
    if cond:
        return True, ""
    print(f"  [FAIL] {message}")
    return False, message


# ------------------------------------------------------------------- 各检查步骤


def check_pytest() -> tuple[bool, str]:
    if not BACKEND_PY.exists():
        return _require(False, f"未找到后端虚拟环境：{BACKEND_PY}")
    return run_step(
        "1/5 后端 pytest（backend/.venv）",
        [str(BACKEND_PY), "-m", "pytest", "-q"],
        cwd=BACKEND,
        timeout=1800,
        tail_pass=10,
        tail_fail=100,
    )


def check_pyflakes() -> tuple[bool, str]:
    py = ROOT_PY if ROOT_PY.exists() else Path(sys.executable)
    # 注意：pyflakes 只要发现任何消息（含"imported but unused"）退出码就是 1，
    # 因此本步骤不看退出码，只按输出内容里的 "undefined name" 判失败。
    ok, output = run_step(
        "2/5 pyflakes 未定义名扫描（backend/app，按输出判定，不看退出码）",
        [str(py), "-m", "pyflakes", "backend/app"],
        cwd=ROOT,
        timeout=300,
        tail_pass=8,
        tail_fail=8,
        ignore_exit=True,
    )
    del ok  # 退出码不参与判定
    lines = output.splitlines()
    # "unable to detect undefined names"（star-import 文件放弃分析）包含
    # "undefined name" 子串，必须先剔除再判断
    offenders = [
        ln
        for ln in lines
        if "undefined name" in ln and "unable to detect undefined names" not in ln
    ]
    star_notes = [ln for ln in lines if "unable to detect undefined names" in ln]
    others = [ln for ln in lines if ln.startswith("backend") and "undefined name" not in ln]
    print(f"  pyflakes 消息共 {len(lines)} 行：其他提示（未使用导入等，不判失败）{len(others)} 行")
    if star_notes:
        print(f"  [提示] {len(star_notes)} 个 star-import 文件 pyflakes 放弃分析（不算失败）")
    if offenders:
        print(f"[FAIL] pyflakes 发现 {len(offenders)} 处 undefined name：")
        for ln in offenders:
            print(f"  | {ln}")
        return False, output
    print("[PASS] pyflakes：无 undefined name")
    return True, output


def check_lint() -> tuple[bool, str]:
    return run_step(
        "3/5 前端 lint（pnpm --dir frontend run lint，仅 error 判失败）",
        ("pnpm --dir frontend run lint", True),
        cwd=ROOT,
        timeout=600,
        tail_pass=20,
        tail_fail=80,
    )


def check_build() -> tuple[bool, str]:
    return run_step(
        "4/5 前端 build（pnpm --dir frontend run build）",
        ("pnpm --dir frontend run build", True),
        cwd=ROOT,
        timeout=1200,
        tail_pass=25,
        tail_fail=80,
    )


# --------------------------------------------------------------- 秘密扫描实现


def _is_known_secret_path(path: Path) -> bool:
    try:
        rel = path.resolve().as_posix()
    except OSError:
        return False
    for known in KNOWN_SECRET_FILES + KNOWN_SECRET_DIRS:
        if rel.startswith(known.resolve().as_posix()):
            return True
    return False


def _iter_candidate_files() -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for target in SECRET_SCAN_PATHS:
        if not target.exists():
            continue
        if target.is_file():
            key = str(target.resolve())
            if key not in seen:
                seen.add(key)
                files.append(target)
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
            for fn in sorted(filenames):
                if fn in EXCLUDED_FILE_NAMES:
                    continue
                fp = Path(dirpath) / fn
                key = str(fp.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(fp)
    return files


def _read_text(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    if not data:
        return None
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return None  # 二进制文件跳过
    return data.decode("utf-8", errors="replace")


def check_secrets() -> tuple[bool, str]:
    """只报告不修改不轮换。返回 是否通过（已知秘密位置命中不算失败）。"""
    print()
    print("=" * 72)
    print("[STEP] 5/5 秘密扫描（--secret-scan 开启；只报告，不修改、不轮换）")
    print("=" * 72)
    print("  扫描范围：" + ", ".join(str(p.relative_to(ROOT)) for p in SECRET_SCAN_PATHS if p.exists()))
    print("  排除：" + ", ".join(sorted(EXCLUDED_DIR_NAMES | EXCLUDED_FILE_NAMES)))
    print("  已知秘密位置（命中只报告、不失败）：backend/.env, backend/app/kubeconfigs/")
    print("  规则：" + ", ".join(name for name, _ in SECRET_RULES))

    expected_hits: list[tuple[str, int, str]] = []
    unexpected_hits: list[tuple[str, int, str]] = []
    scanned = 0

    for path in _iter_candidate_files():
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        known = _is_known_secret_path(path)
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = str(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule_name, rx in SECRET_RULES:
                if rx.search(line):
                    # 只记录 位置 + 规则名，绝不记录行内容
                    (expected_hits if known else unexpected_hits).append((rel, lineno, rule_name))

    print(f"  已扫描文本文件 {scanned} 个")
    if expected_hits:
        print(f"  —— 已知秘密位置命中（预期内，{len(expected_hits)} 处）——")
        for rel, lineno, rule in expected_hits:
            print(f"  [预期内] {rel}:{lineno}  规则={rule}")
    if unexpected_hits:
        print(f"  —— 预期外命中（{len(unexpected_hits)} 处，需要人工确认）——")
        for rel, lineno, rule in unexpected_hits:
            print(f"  [命中] {rel}:{lineno}  规则={rule}")

    ok = not unexpected_hits
    if ok:
        print(f"[PASS] 秘密扫描：预期外命中 0 处（已知位置命中 {len(expected_hits)} 处属预期）")
    else:
        print(f"[FAIL] 秘密扫描：预期外命中 {len(unexpected_hits)} 处（只报告，不自动修改）")
    return ok, ""


# ------------------------------------------------------------------------ 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpsPilot 统一验证入口")
    parser.add_argument("--secret-scan", action="store_true", help="追加秘密扫描步骤（默认跳过）")
    parser.add_argument("--only-secret", action="store_true", help="只跑秘密扫描（调试扫描规则用）")
    args = parser.parse_args(argv)

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    total_started = time.time()
    print("OpsPilot 全量检查")
    print(f"仓库根：{ROOT}")

    if args.only_secret:
        ok, _ = check_secrets()
        return 0 if ok else 1

    checks: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("后端 pytest", check_pytest),
        ("pyflakes 未定义名", check_pyflakes),
        ("前端 lint", check_lint),
        ("前端 build", check_build),
    ]
    if args.secret_scan:
        checks.append(("秘密扫描", check_secrets))

    results: list[tuple[str, bool, float]] = []
    for _, fn in checks:
        started = time.time()
        ok, _ = fn()
        results.append((fn.__name__, ok, time.time() - started))

    total_elapsed = time.time() - total_started
    print()
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    failed = 0
    for name, ok, elapsed in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}（{elapsed:.1f}s）")
        failed += 0 if ok else 1
    print(f"  共 {len(results)} 步，失败 {failed} 步，总耗时 {total_elapsed:.1f}s")
    if failed:
        print("结论：存在失败项。")
        return 1
    print("结论：全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
