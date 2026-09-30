"""敏感文件落盘工具。"""
from __future__ import annotations

import os
from pathlib import Path


def _best_effort_chmod(path: Path, mode: int) -> None:
    """尽力设置文件权限，兼容 Windows 开发环境不完整的 chmod 语义。"""
    try:
        os.chmod(path, mode)
    except Exception:
        # Windows 或特殊挂载目录可能不支持完整 POSIX 权限语义。
        # 这里不阻断主流程，生产 Linux 环境会正常生效。
        pass


def ensure_private_directory(path: str | Path) -> Path:
    """创建仅服务进程可访问的敏感目录。

    Args:
        path: 目录路径。

    Returns:
        已 resolve 前的 Path 对象，供调用方继续拼接文件。
    """
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(target, 0o700)
    return target


def write_sensitive_text_file(path: str | Path, content: str) -> Path:
    """以 0600 权限写入敏感文本文件。

    Args:
        path: 目标文件路径。
        content: 待写入内容。

    Returns:
        写入完成的 Path 对象。
    """
    target = Path(path)
    ensure_private_directory(target.parent)
    target.write_text(str(content or ""), encoding="utf-8")
    _best_effort_chmod(target, 0o600)
    return target
