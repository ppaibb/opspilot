# 时间格式化统一工具。
#
# 项目里并存两种落库约定（见 AGENTS.md 注意事项第 0 条）：
#   1. ORM 列 default=datetime.utcnow —— 存 naive UTC（audit_logs.time、
#      monitoring_active_alerts.first_seen_at 等）；
#   2. `_now_str()` —— 存本地时间字符串（AI 会话、LLM 配置等 JSON 存储）。
# 序列化给前端时**必须区分**来源，否则就会出现"审计时间偏差 16 小时"这类 bug
# （naive UTC 被当本地时间再转一次）。规则：
#   - utcnow 落库的值 → 用 to_local_str()（按 UTC 解释转本地）；
#   - 本地串落库的值 → 直接 strftime，绝不过 to_local_str()。
from __future__ import annotations

from datetime import datetime, timezone


def to_local_str(value: datetime | str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把 naive UTC（datetime 或 ISO 字符串）转为服务器本地时间字符串。

    - naive datetime / naive ISO 串：按 UTC 解释（utcnow 落库约定）转本地；
    - 带 tzinfo 的值：按其自身时区转本地；
    - 解析失败：原样返回字符串（宁可显示原始值也不猜）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        return _aware_to_local(parsed, fmt)
    if isinstance(value, datetime):
        return _aware_to_local(value, fmt)
    return str(value)


def _aware_to_local(dt: datetime, fmt: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def utcnow_naive() -> datetime:
    """统一入口：当前的 naive UTC（给 utcnow 落库约定用，替代散落的 datetime.utcnow()）。"""
    return datetime.utcnow()


def local_str_to_utc_naive(value: "datetime | str | None") -> "datetime | None":
    """把本地时间（串或 datetime）转成 naive UTC，给 utcnow 落库的列做范围比较。

    筛选类接口收到的时间参数天然是用户本地时区（前端时间选择器给出的就是本地时间），
    而 audit_logs.time 存的是 naive UTC——不转换就会出现"选今天却查不到今天"。
    - naive datetime / naive ISO 串：按本地时区解释后转 UTC 再去掉 tzinfo；
    - 带 tzinfo 的值：按自身时区转 UTC；
    - 空值 / 解析失败：None（调用方应忽略该条件，不当成 1970 之类的边界值）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def local_str_now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """统一入口：当前本地时间字符串（给 _now_str 落库约定用）。"""
    return datetime.now().strftime(fmt)
