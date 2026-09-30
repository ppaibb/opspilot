"""数据库 JSON 列表字段类型。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.types import Text, TypeDecorator

from app.core.json_fields import dump_json_list, normalize_json_list


class JsonListText(TypeDecorator):
    """兼容历史字符串列的 JSON 字符串列表类型。

    SQLite 没有真正的 JSON 数组列，现有表结构也已经是 VARCHAR/TEXT。
    因此这里仍以文本保存，但 ORM 层统一读成 list、写成规范 JSON，
    为后续迁移到 PostgreSQL JSON/JSONB 或关联表保留清晰边界。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str:
        """写库前统一转成 JSON 数组字符串。"""
        return dump_json_list(value)

    def process_result_value(self, value: Any, dialect: Any) -> list[str]:
        """读库后统一转成字符串列表。"""
        return normalize_json_list(value)
