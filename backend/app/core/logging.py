"""应用运行日志：控制台 + 滚动文件，供部署/排障使用。

约定：
- 文件落在 `backend/logs/`（可用环境变量 `LOG_DIR` 覆盖）
- 按大小轮转：默认 10MB × 5 份（app.log / app.log.1 …）
- 审计类业务数据仍写数据库（audit 表），这里只管运行日志
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_CONFIGURED = False

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(
    log_dir: str | os.PathLike | None = None,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path:
    """初始化 root logger：控制台 + RotatingFileHandler。可重复调用（幂等）。"""
    global _CONFIGURED
    directory = Path(log_dir or os.environ.get("LOG_DIR") or DEFAULT_LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "app.log"

    root = logging.getLogger()
    if _CONFIGURED:
        return log_file

    root.setLevel(level)
    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 访问/uvicorn 自带 logger 也落同一文件
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING if name.startswith("sqlalchemy") else level)

    _CONFIGURED = True
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
