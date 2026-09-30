"""JSON 字段归一化工具。

这里集中处理“字符串列表存成 JSON”的历史兼容问题：
1. 老数据可能是空串、NULL 或 JSON 字符串；
2. 新代码可能直接给 ORM 字段赋 list；
3. API 始终向前端输出 list，避免调用方关心数据库里的物理表示。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def normalize_json_list(raw_value: Any) -> list[str]:
    """把字符串、JSON 字符串或列表归一化成去重后的字符串列表。"""
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        items: Iterable[Any] = parsed
    elif isinstance(raw_value, (list, tuple, set)):
        items = raw_value
    else:
        return []

    normalized = [str(item or "").strip() for item in items if str(item or "").strip()]
    return list(dict.fromkeys(normalized))


def dump_json_list(items: Any) -> str:
    """把列表型字段序列化为规范 JSON 字符串；空列表写成 []。"""
    return json.dumps(normalize_json_list(items), ensure_ascii=False)
