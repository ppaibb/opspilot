"""应用中心时间字段兼容工具。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

APPLICATION_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_dt() -> datetime:
    """返回应用中心统一使用的本地 datetime。"""
    return datetime.now().replace(microsecond=0)


def parse_app_datetime(value: Any) -> datetime | None:
    """把历史字符串或 DateTime 值解析为 datetime。

    旧 SQLite 数据里保存的是 `YYYY-MM-DD HH:MM:SS` 字符串；新代码写入
    SQLAlchemy DateTime。这里同时兼容两种形态，避免迁移窗口内读写报错。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text[:19], text.replace("T", " ")[:19]):
        for fmt in (APPLICATION_TIME_FORMAT, "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        return None


def format_app_datetime(value: Any) -> str:
    """把 DateTime/历史字符串格式化为前端兼容字符串。"""
    parsed = parse_app_datetime(value)
    if parsed is None:
        return str(value or "").strip()
    return parsed.strftime(APPLICATION_TIME_FORMAT)
