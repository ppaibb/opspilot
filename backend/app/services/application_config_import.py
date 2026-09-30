"""应用配置批量导入解析服务。"""
from __future__ import annotations

import re

from fastapi import HTTPException

MAX_CONFIG_IMPORT_CONTENT_LENGTH = 65536
MAX_CONFIG_IMPORT_LINES = 500
# `.env` 键名校验：必须以字母或下划线开头，后续允许字母、数字和下划线。
CONFIG_IMPORT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 命中即视为敏感键，除非调用方显式允许明文导入。
CONFIG_IMPORT_SENSITIVE_PATTERN = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY|ACCESSKEY)$",
    re.IGNORECASE,
)


def parse_env_import_content(content: str) -> tuple[list[tuple[int, str, str]], list[tuple[int, str]]]:
    """解析 `.env` 文本为 key/value 列表。

    返回：
        entries: 每行解析成功的 (line_no, key, value) 列表；
        errors: 解析失败的 (line_no, reason) 列表。

    安全边界：
    - 只接受 `KEY=VALUE`、`export KEY=VALUE` 两种；
    - 遇到行内注释按 `#` 之前的部分处理；
    - 仅裁剪外层双 / 单引号，不做 shell 展开；
    - 不允许 `\x00`，长度受字符数和行数两重约束。
    """
    text = str(content or "")
    if "\x00" in text:
        raise HTTPException(status_code=400, detail=".env 内容包含非法字符")
    if len(text) > MAX_CONFIG_IMPORT_CONTENT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f".env 内容不能超过 {MAX_CONFIG_IMPORT_CONTENT_LENGTH} 个字符",
        )
    entries: list[tuple[int, str, str]] = []
    errors: list[tuple[int, str]] = []
    for index, raw_line in enumerate(text.splitlines(), start=1):
        if index > MAX_CONFIG_IMPORT_LINES:
            errors.append((index, f"超过单次最多 {MAX_CONFIG_IMPORT_LINES} 行"))
            break
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            errors.append((index, "缺少 `=`，无法解析为 KEY=VALUE"))
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # 按 shell 习惯裁剪外层引号；只处理最简单的单 / 双引号包裹。
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif "#" in value:
            # 去掉行内注释（仅当值未被引号包裹时）。
            value = value.split("#", 1)[0].strip()
        if not key:
            errors.append((index, "键为空"))
            continue
        if not CONFIG_IMPORT_KEY_PATTERN.fullmatch(key):
            errors.append((index, f"键 {key} 仅允许字母、数字、下划线，且不能以数字开头"))
            continue
        entries.append((index, key, value))
    return entries, errors
