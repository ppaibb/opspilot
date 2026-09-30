"""数据库管理核心服务：SQL 审核（Yearning 风格规则子集）与执行引擎。

审核引擎（纯函数，便于单测）：
- 语句切分：状态机跳过字符串字面量/注释内的分号；
- 语句分类：DDL / DML / DQL / TCL / 其他；
- 规则（block 级直接拒绝工单，warn 级提示后放行）：
  R1 DROP DATABASE / DROP SCHEMA           → block
  R2 DROP TABLE                            → block
  R3 TRUNCATE                              → block
  R4 UPDATE/DELETE 无 WHERE                → block
  R5 SELECT *                              → warn（建议明列）
  R6 ALTER TABLE ... DROP COLUMN           → warn（不可逆变更提示）
  R7 超大事务（语句 > 100KB）              → warn
  R8 查询工单包含非只读语句                → block

执行引擎：
- MySQL 走 pymysql、PostgreSQL 走 psycopg（复用项目既有驱动）；
- 变更逐条执行（自动提交）；查询仅返回首条语句结果集（行数上限截断）；
- 单条语句超时由驱动层 socket timeout 控制，整单最多 60 秒；
- 驱动报错先翻成中文可操作提示（hint），原始错误原文一并保留供审计。
"""
from __future__ import annotations

import re
import time
from typing import Any

# ───────────────────────── 语句切分 ─────────────────────────

def split_sql_statements(sql_text: str) -> list[str]:
    """按分号切分 SQL，跳过 '...' / "..." / `...` 字符串与 -- / # 注释内的分号。"""
    statements: list[str] = []
    buffer: list[str] = []
    in_single = in_double = in_backtick = in_line_comment = in_block_comment = False
    i, length = 0, len(sql_text or "")
    while i < length:
        ch = sql_text[i]
        nxt = sql_text[i + 1] if i + 1 < length else ""
        if in_line_comment:
            buffer.append(ch)
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                i += 1
                in_block_comment = False
        elif in_single:
            buffer.append(ch)
            if ch == "\\" and nxt:
                buffer.append(nxt)
                i += 1
            elif ch == "'":
                in_single = False
        elif in_double:
            buffer.append(ch)
            if ch == "\\" and nxt:
                buffer.append(nxt)
                i += 1
            elif ch == '"':
                in_double = False
        elif in_backtick:
            buffer.append(ch)
            if ch == "`":
                in_backtick = False
        else:
            if ch == "-" and nxt == "-":
                in_line_comment = True
                buffer.extend("--")
                i += 1
            elif ch == "#":
                in_line_comment = True
                buffer.append(ch)
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                buffer.extend("/*")
                i += 1
            elif ch == "'":
                in_single = True
                buffer.append(ch)
            elif ch == '"':
                in_double = True
                buffer.append(ch)
            elif ch == "`":
                in_backtick = True
                buffer.append(ch)
            elif ch == ";":
                stmt = "".join(buffer).strip()
                if stmt:
                    statements.append(stmt)
                buffer = []
            else:
                buffer.append(ch)
        i += 1
    tail = "".join(buffer).strip()
    # 尾段若只剩注释（如末尾的 -- note）不是可执行语句，丢弃。
    if tail and _strip_comments(tail):
        statements.append(tail)
    return statements


# ───────────────────────── 语句分类与审核 ─────────────────────────

_COMMENT_RE = re.compile(r"(--[^\n]*|#[^\n]*|/\*.*?\*/)", re.S)
# 三种引号字面量（含反斜杠转义），用于审核前脱敏，防止数据文本误伤规则匹配。
_STRING_RE = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`)""")


def _strip_comments(stmt: str) -> str:
    return _COMMENT_RE.sub(" ", stmt).strip()


def _desensitize(stmt: str) -> str:
    """把字符串/反引号字面量替换为 ''，防止数据中的关键词（如 'drop table'）误伤规则匹配。"""
    return _STRING_RE.sub("''", _strip_comments(stmt))


def classify_statement(stmt: str) -> str:
    """返回语句大类：ddl / dml / dql / tcl / utility / unknown。"""
    normalized = _desensitize(stmt).lstrip("( \n\t").upper()
    first = normalized.split(None, 1)[0] if normalized.split() else ""
    if first in {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME"}:
        return "ddl"
    if first in {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}:
        return "dml"
    if first in {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH"}:
        return "dql"
    if first in {"BEGIN", "COMMIT", "ROLLBACK", "START", "SET"}:
        return "tcl"
    if first in {"USE", "GRANT", "REVOKE", "FLUSH", "ANALYZE", "OPTIMIZE", "CALL", "KILL", "LOCK", "UNLOCK"}:
        return "utility"
    return "unknown"


def is_readonly_statement(stmt: str) -> bool:
    """只读判定：SELECT/SHOW/DESC/EXPLAIN/WITH（CTE 查询）且不含写副作用。

    WITH 分支专门堵 CTE 写绕过：PostgreSQL 允许 `WITH x AS (DELETE FROM t
    RETURNING *) SELECT * FROM x` 这类"体内写"语句，不能凭首关键字放行。
    做法：在字符串脱敏 + 去注释之后，按括号深度切出每个 `name AS ( ... )`
    体，取体内第一条有效 SQL 关键字，出现写副作用关键字（DML 之外还包括
    CALL/DO/GRANT/REVOKE/LOCK/VACUUM/ANALYZE/SET）即判非只读；嵌套 CTE
    （体内又是 WITH）递归判定；CTE 之后的主查询关键字同样参与判定
    （`WITH ... DELETE FROM t` 也是合法写法）。非 WITH 分支行为保持不变。
    """
    normalized = _desensitize(stmt).lstrip("( \n\t").upper()
    if not normalized.split():
        return False
    first = normalized.split(None, 1)[0]
    if first not in {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH"}:
        return False
    # SELECT ... INTO OUTFILE/DUMPFILE 有写副作用（CTE 体内出现同样拒绝）。
    if "INTO OUTFILE" in normalized or "INTO DUMPFILE" in normalized:
        return False
    if first != "WITH":
        return True
    return _is_with_readonly(normalized)


# CTE 体内（含 WITH 之后的主查询）出现即判非只读的关键字。
# 除 DML 外还收录有副作用的 utility/管理语句：CALL（存储过程）、DO（匿名块）、
# GRANT/REVOKE（授权）、LOCK（锁表）、VACUUM/ANALYZE（维护写）、SET（改会话
# 状态，PG 上 SET 后接 DML 的事务语义会被误用）——首关键字白名单挡不住它们
# 藏在 CTE 体内或主查询里。
_WRITE_SIDE_EFFECT_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE",
    "CALL", "DO", "GRANT", "REVOKE", "LOCK", "VACUUM", "ANALYZE", "SET",
}
# CTE 体括号前必须紧跟 AS 关键字（区分 `name AS (` 与列名列表 `name (a,b)`）。
_CTE_AS_PREFIX_RE = re.compile(r"\bAS\s*$")


def _first_sql_keyword(text: str) -> str:
    """取去掉空白与多余左括号后的第一个 SQL 关键字（大写）。"""
    stripped = text.lstrip("( \n\t\r")
    parts = stripped.split(None, 1)
    return parts[0] if parts else ""


def _match_paren(text: str, open_idx: int) -> int:
    """返回与 text[open_idx] 的 '(' 配对的 ')' 下标；不配对返回文本末尾。"""
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(text)


def _is_with_readonly(normalized: str) -> bool:
    """判定 WITH 语句是否只读：逐个 CTE 体与主查询的关键字均不得为写操作。"""
    # 剥掉开头 WITH 及可选的 RECURSIVE，剩下的从第一个 CTE 名开始。
    rest = re.sub(r"^WITH\s+(RECURSIVE\s+)?", "", normalized)
    bodies: list[str] = []
    main_query = ""
    i, length = 0, len(rest)
    while i < length:
        ch = rest[i]
        if ch != "(":
            i += 1
            continue
        prefix = rest[:i].rstrip()
        if _CTE_AS_PREFIX_RE.search(prefix):
            # `name AS (`：这是 CTE 体，整体捕获。
            close = _match_paren(rest, i)
            bodies.append(rest[i + 1:close])
            i = close + 1
            # 体结束后：空白后是逗号 → 还有下一个 CTE；其他字符 → 主查询开始。
            while i < length and rest[i] in " \n\t\r":
                i += 1
            if i < length and rest[i] == ",":
                i += 1
                continue
            main_query = rest[i:]
            break
        # 非体括号（CTE 列名列表 `name (a,b)` 等）：跳过配对段继续扫描。
        i = _match_paren(rest, i) + 1

    for body in bodies:
        keyword = _first_sql_keyword(body)
        if not keyword:
            continue
        if keyword in _WRITE_SIDE_EFFECT_KEYWORDS:
            return False
        if keyword == "WITH":
            # 嵌套 CTE：体内又是 WITH，递归判定。
            if not _is_with_readonly(body):
                return False
    if main_query:
        if _first_sql_keyword(main_query) in _WRITE_SIDE_EFFECT_KEYWORDS:
            return False
    return True


# 默认规则配置（system_config 可覆盖）：rule → {enabled, level}。
# level 可调 block↔warn，例如把 DROP TABLE 从拦截降为警告（不推荐但允许）。
DEFAULT_AUDIT_RULES: dict[str, dict[str, Any]] = {
    "R1": {"enabled": True, "level": "block", "name": "禁 DROP DATABASE/SCHEMA"},
    "R2": {"enabled": True, "level": "block", "name": "禁 DROP TABLE"},
    "R3": {"enabled": True, "level": "block", "name": "禁 TRUNCATE"},
    "R4": {"enabled": True, "level": "block", "name": "UPDATE/DELETE 必须带 WHERE"},
    "R5": {"enabled": True, "level": "warn", "name": "避免 SELECT *"},
    "R6": {"enabled": True, "level": "warn", "name": "ALTER DROP 不可逆提示"},
    "R7": {"enabled": True, "level": "warn", "name": "超大语句（>100KB）提示"},
    "R8": {"enabled": True, "level": "block", "name": "查询仅允许只读语句"},
    "R9": {"enabled": True, "level": "warn", "name": "未识别语句类型提示"},
}


def audit_sql(sql_text: str, ticket_type: str = "change", rule_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """规则引擎入口：返回 {passed, findings: [{rule, level, message, statement_index}]}。

    rule_config：{rule_id: {"enabled": bool, "level": "block"|"warn"}}，
    传 None 用 DEFAULT_AUDIT_RULES；由 API 层从 system_config 读取后传入。
    """
    rules = {k: dict(v) for k, v in DEFAULT_AUDIT_RULES.items()}
    if rule_config:
        for rule_id, override in rule_config.items():
            if rule_id in rules and isinstance(override, dict):
                rules[rule_id].update(override)
    statements = split_sql_statements(sql_text)
    findings: list[dict[str, Any]] = []
    if not statements:
        return {"passed": False, "findings": [{"rule": "R0", "level": "block", "message": "SQL 内容为空", "statement_index": -1}]}

    for idx, stmt in enumerate(statements):
        upper = _desensitize(stmt).upper()
        label = f"第 {idx + 1} 条"

        # R1/R2/R3：破坏性 DDL（开关与级别由规则配置决定）。
        if rules["R1"]["enabled"] and re.search(r"\bDROP\s+(DATABASE|SCHEMA)\b", upper):
            findings.append({"rule": "R1", "level": rules["R1"]["level"], "message": f"{label}: 禁止 DROP DATABASE/SCHEMA", "statement_index": idx})
        if rules["R2"]["enabled"] and re.search(r"\bDROP\s+TABLE\b", upper):
            findings.append({"rule": "R2", "level": rules["R2"]["level"], "message": f"{label}: 禁止 DROP TABLE（数据不可恢复）", "statement_index": idx})
        if rules["R3"]["enabled"] and re.search(r"\bTRUNCATE\b", upper):
            findings.append({"rule": "R3", "level": rules["R3"]["level"], "message": f"{label}: 禁止 TRUNCATE（清空全表）", "statement_index": idx})

        # R4：UPDATE/DELETE 必须带 WHERE。
        if rules["R4"]["enabled"] and re.match(r"^\s*(UPDATE|DELETE)\b", upper) and not re.search(r"\bWHERE\b", upper):
            findings.append({"rule": "R4", "level": rules["R4"]["level"], "message": f"{label}: UPDATE/DELETE 缺少 WHERE 条件（全表变更）", "statement_index": idx})

        # R5：SELECT * 建议明列。
        if rules["R5"]["enabled"] and re.search(r"\bSELECT\s+\*", upper):
            findings.append({"rule": "R5", "level": rules["R5"]["level"], "message": f"{label}: 建议避免 SELECT *，明确列名", "statement_index": idx})

        # R6：ALTER DROP COLUMN 不可逆提示。
        if rules["R6"]["enabled"] and re.search(r"\bALTER\s+TABLE\b[\s\S]*\bDROP\s+(COLUMN\b|INDEX\b)", upper):
            findings.append({"rule": "R6", "level": rules["R6"]["level"], "message": f"{label}: ALTER DROP 属于不可逆变更，请确认已备份", "statement_index": idx})

        # R7：超大语句。
        if rules["R7"]["enabled"] and len(stmt.encode("utf-8")) > 100 * 1024:
            findings.append({"rule": "R7", "level": rules["R7"]["level"], "message": f"{label}: 单条语句超过 100KB，属超大事务", "statement_index": idx})

        # R8：查询工单只允许只读语句。
        if rules["R8"]["enabled"] and ticket_type == "query" and not is_readonly_statement(stmt):
            findings.append({"rule": "R8", "level": rules["R8"]["level"], "message": f"{label}: 查询工单仅允许 SELECT/SHOW/DESC/EXPLAIN", "statement_index": idx})

        # 变更工单里出现未知语句类型（可能绕过规则）给 warn。
        if rules["R9"]["enabled"] and ticket_type == "change" and classify_statement(stmt) == "unknown":
            findings.append({"rule": "R9", "level": rules["R9"]["level"], "message": f"{label}: 无法识别的语句类型，请人工确认", "statement_index": idx})

    passed = not any(f["level"] == "block" for f in findings)
    return {"passed": passed, "findings": findings}


# ───────────────────────── 执行引擎 ─────────────────────────

EXEC_TIMEOUT_SECONDS = 60
QUERY_MAX_ROWS = 500

# 维护库：列出"可选目标库"时要藏起来，但实例自己配的库即便叫这些名字也得保留。
PG_SYSTEM_DATABASES = {"postgres", "template0", "template1"}
MYSQL_SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}


def filter_selectable_databases(db_type: str, names: list[str], keep: str = "") -> list[str]:
    """从数据库清单里剔掉维护库，只留业务上真会查的。

    实例自身配置的库永远保留——有人就是把 OpsPilot 指在一个叫 postgres 的库上，
    这时候把它过滤掉反而让"默认库"下拉空掉，更莫名其妙。
    """
    hidden = MYSQL_SYSTEM_DATABASES if db_type == "mysql" else PG_SYSTEM_DATABASES
    kept = (keep or "").strip()
    return [n for n in names if n == kept or n not in hidden]


# PostgreSQL SQLSTATE / MySQL errno → 中文可操作提示。
# 只覆盖普通用户真会碰到的几类：连错库、表/列名写错、权限不够。
# 连接类错误（错密码/超时/拒连）sqlstate 是 None，靠 _friendly_error 里的消息特征匹配。
_PG_HINTS: dict[str, str] = {
    "42P01": "表不存在。注意 PostgreSQL 的 `a.b` 是 **schema.表**（不是库.表）："
             "`\"opspilot\".\"users\"` 会找 schema=opspilot。常用写法是 `public.users`，"
             "或不写 schema 走默认 search_path。要查别的库请在上方切换目标库——"
             "PostgreSQL 不能用 `库名.表名` 跨库。",
    "3F000": "库（schema）不存在。检查 SQL 里的 schema 名，PostgreSQL 不指定时默认走 public。",
    "42703": "列不存在。检查列名拼写，注意 PostgreSQL 的标识符区分大小写，"
             "建表时用了引号的列必须一直带引号。",
    "42P02": "表或视图不存在（带 schema 限定名时）。检查 schema 与表名是否写对。",
    "42501": "权限不足。当前连接账号没有访问该表/该操作的权限，找 DBA 授权，或换一个有权限的数据源。",
    "28P01": "密码认证失败。检查数据源里保存的密码是否已过期或被修改。",
    "3D000": "数据库不存在。检查数据源配置的库名。",
    "08006": "连接被断开。可能是网络抖动或服务端主动关闭，重试一次；持续失败就检查服务端状态。",
}

_MYSQL_HINTS: dict[int, str] = {
    1146: "最常见的原因是连错了库——OpsPilot 自己的表都在实例默认库里；"
          "如果表在别的库，SQL 里要写全 `库名.表名`，或在上方切换目标库后再查。",
    1049: "数据库不存在。检查数据源配置的库名。",
    1045: "密码认证失败。检查数据源里保存的密码是否已过期或被修改。",
    1044: "权限不足。当前连接账号没有访问该库的权限，找 DBA 授权，或换一个有权限的数据源。",
    1046: "没有选择数据库。检查数据源配置的默认库。",
    1054: "列不存在。检查列名拼写。",
    1142: "权限不足。当前连接账号没有对该表的操作权限。",
    1051: "表不存在（带库名限定时）。检查库名与表名。",
}

# 连接类失败在驱动层拿不到 SQLSTATE，只能按消息特征判断。
# 每条是 (匹配关键词, 提示语)，从上往下第一个命中的生效。
_CONNECTION_HINTS: list[tuple[str, str]] = [
    ("password authentication failed", "密码认证失败。检查数据源里保存的密码是否正确、是否已被修改。"),
    ("access denied", "密码认证失败。检查数据源里保存的密码是否正确、是否已被修改。"),
    ("connection timeout expired", "连接超时。主机或端口不对、对方没监听，或网络不通——先确认地址端口，再看网络连通性。"),
    ("timeout", "连接超时。主机或端口不对、对方没监听，或网络不通——先确认地址端口，再看网络连通性。"),
    ("connection refused", "连接被拒绝。数据库服务没在跑，或端口填错了。"),
    ("could not translate host name", "主机名解析不了。检查数据源里填的是 IP 还是域名，域名的话确认 DNS 能解析。"),
    ("name or service not known", "主机名解析不了。检查数据源里填的是 IP 还是域名，域名的话确认 DNS 能解析。"),
    ("no route to host", "网络不可达。主机不在同一网段，或被防火墙拦了。"),
    ("ssl connection has been closed", "TLS 连接被关闭。检查服务端是否强制 SSL，或证书是否过期。"),
]


def _friendly_error(db_type: str, exc: Exception, database: str = "") -> str:
    """把驱动异常翻成用户能看懂、能接着做的中文提示。

    原始错误（`str(exc)`）仍然单独返回，审计和排查都要用原文；这里只负责
    "告诉用户下一步该干嘛"，所以宁可说得啰嗦一点。
    """
    raw = str(exc) or ""
    first_line = raw.splitlines()[0] if raw else ""
    where = f"当前连的是「{database}」库。" if database else ""

    def _conn_hint() -> str | None:
        low = raw.lower()
        for keyword, text in _CONNECTION_HINTS:
            if keyword in low:
                return text
        return None

    if db_type == "mysql":
        code = None
        args = getattr(exc, "args", None) or ()
        if args and isinstance(args[0], int):
            code = args[0]
        hint = _MYSQL_HINTS.get(code) if code is not None else None
        if hint:
            subject = f"表不存在。{hint}" if code == 1146 else hint
            return f"{subject}{where}"
        conn = _conn_hint()
        if conn:
            return f"{conn}{where}"
        return f"执行失败：{first_line[:200]}{where}"

    sqlstate = getattr(exc, "sqlstate", None)
    hint = _PG_HINTS.get(sqlstate) if sqlstate else None
    if hint:
        if sqlstate == "42P01":
            # 把 relation 名从原文里抠出来，提示里点名，用户不用自己去比对；
            # 抠不到也要保留"表不存在"这个主语，不能只剩一串建议。
            match = re.search(r'relation "([^"]+)" does not exist', raw)
            subject = f'表 "{match.group(1)}" 不存在。' if match else "表不存在。"
            return f"{subject}{hint}{where}"
        return f"{hint}{where}"

    conn = _conn_hint()
    if conn:
        return f"{conn}{where}"

    name = type(exc).__name__.lower()
    if "operational" in name or "interface" in name or "timeout" in name:
        return f"连不上数据库服务（网络不可达、端口错误或服务未启动）。检查主机、端口和网络连通性。{where}"
    return f"执行失败：{first_line[:200]}{where}"


def _build_connection(db_type: str, host: str, port: int, username: str, password: str, database: str):
    """按类型建立原生连接（每次执行新建，用完即关——工单低频，不值得做连接池）。"""
    if db_type == "mysql":
        import pymysql

        return pymysql.connect(
            host=host, port=int(port), user=username, password=password,
            database=database or None, charset="utf8mb4",
            connect_timeout=6, read_timeout=EXEC_TIMEOUT_SECONDS, write_timeout=EXEC_TIMEOUT_SECONDS,
        )
    if db_type == "postgresql":
        import psycopg

        return psycopg.connect(
            host=host, port=int(port), user=username, password=password,
            dbname=database or "postgres", connect_timeout=6,
        )
    raise ValueError(f"不支持的数据库类型: {db_type}")


def test_instance_connection(db_type: str, host: str, port: int, username: str, password: str, database: str) -> tuple[bool, str]:
    """连通测试：SELECT 1 + 版本号。"""
    try:
        conn = _build_connection(db_type, host, port, username, password, database)
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败: {str(exc)[:200]}"
    try:
        if db_type == "mysql":
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                version = str(cursor.fetchone()[0])
        else:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version()")
                version = str(cursor.fetchone()[0])
        return True, f"连接成功（{db_type} {version.split('-')[0]}）"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接成功但查询失败（检查库权限）: {str(exc)[:160]}"
    finally:
        conn.close()


def _apply_session_readonly(conn, db_type: str) -> str:
    """把连接切到会话级只读，作为审核引擎之外的数据库侧兜底。

    - PostgreSQL：`SET default_transaction_read_only = on` 只对后续事务生效，
      所以设置后立即 commit，让用户语句开在新只读事务里；
    - MySQL 8 / 5.7.20+：`SET SESSION transaction_read_only = 1`
      （MySQL 没有 default_transaction_read_only，这是会话级只读的正确写法；
      `SET SESSION TRANSACTION READ ONLY` 语法只影响下个事务，不采用）；
    - 老版本 MySQL 没有该变量：跳过设置并返回提示文案，不阻塞执行
      （此时写拦截仅依赖审核引擎）。

    Returns:
        提示文案（设置失败时非空，供结果透出）；设置成功返回空串。
    """
    try:
        with conn.cursor() as cursor:
            if db_type == "postgresql":
                cursor.execute("SET default_transaction_read_only = on")
            else:
                cursor.execute("SET SESSION transaction_read_only = 1")
        conn.commit()
        return ""
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        if db_type == "mysql":
            return (
                "当前实例不支持会话级只读（transaction_read_only 变量不可用，"
                "常见于 MySQL 5.7.20 之前的版本），已跳过该设置，写拦截仅依赖 SQL 审核。"
            )
        return f"会话只读设置失败（{str(exc)[:120]}），已跳过，写拦截仅依赖 SQL 审核。"


def execute_statements(
    db_type: str, host: str, port: int, username: str, password: str, database: str,
    statements: list[str], fetch_rows: bool = False,
    readonly: bool = False,
) -> dict[str, Any]:
    """执行语句列表，返回逐条结果。

    fetch_rows=True（自助查询）：只执行第一条（多语句拒绝），返回结果集（上限截断）。
    变更模式逐条自动提交；中途失败即停止（已执行的不回滚，P0 简化并在工单注明）。

    readonly=True（自助查询入口使用）：执行用户 SQL 前先把连接切为会话级只读
    （PG: default_transaction_read_only / MySQL: transaction_read_only），
    即使审核被绕过，数据库本身也会拒绝写语句。连接用完即关的模式不变，
    会话设置随连接销毁，不影响其他调用。返回值在该场景多一个
    ``readonly_notice`` 字段（设置失败的原因提示，成功为空串）。
    """
    started = time.time()
    results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    columns: list[str] = []
    ok = True
    conn = None
    try:
        conn = _build_connection(db_type, host, port, username, password, database)
        readonly_notice = _apply_session_readonly(conn, db_type) if readonly else ""
        if fetch_rows:
            statements = statements[:1]
        with conn.cursor() as cursor:
            for idx, stmt in enumerate(statements):
                stmt_start = time.time()
                try:
                    # 不能用 execute() 的返回值当影响行数：pymysql 返回 int，
                    # psycopg3 返回 cursor 自身（int(cursor) 直接 TypeError）。
                    # rowcount 两个驱动都支持，SELECT 时是结果集行数。
                    cursor.execute(stmt)
                    if fetch_rows and idx == 0 and cursor.description:
                        columns = [desc[0] for desc in cursor.description]
                        raw_rows = cursor.fetchmany(QUERY_MAX_ROWS)
                        for row in raw_rows:
                            rows.append(dict(zip(columns, [_jsonable(v) for v in row])))
                    conn.commit()
                    results.append({
                        "index": idx,
                        "sql": stmt[:500],
                        "ok": True,
                        "rows_affected": max(0, int(cursor.rowcount or 0)),
                        "duration_ms": int((time.time() - stmt_start) * 1000),
                    })
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    ok = False
                    results.append({
                        "index": idx,
                        "sql": stmt[:500],
                        "ok": False,
                        "error": str(exc)[:500],
                        "hint": _friendly_error(db_type, exc, database),
                        "duration_ms": int((time.time() - stmt_start) * 1000),
                    })
                    break
    except Exception as exc:  # noqa: BLE001
        # 连接本身就建不起来（密码错/网络不通）时，上面那个循环还没进去，
        # 也要给一条带 hint 的结果，否则前端只看到空 results。
        ok = False
        results.append({
            "index": 0,
            "sql": (statements[0][:500] if statements else ""),
            "ok": False,
            "error": str(exc)[:500],
            "hint": _friendly_error(db_type, exc, database),
            "duration_ms": int((time.time() - started) * 1000),
        })
    finally:
        if conn is not None:
            conn.close()
    result = {
        "ok": ok,
        "results": results,
        "columns": columns,
        "rows": rows,
        "total_duration_ms": int((time.time() - started) * 1000),
    }
    if readonly:
        result["readonly_notice"] = readonly_notice
    return result


def _jsonable(value: Any) -> Any:
    """把驱动返回的类型转为 JSON 可序列化（bytes/datetime 等）。"""
    import datetime as _dt
    import decimal

    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat(sep=" ") if isinstance(value, _dt.datetime) else value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.timedelta,)):
        return str(value)
    return value


# ───────────────────────── 变更安全网：备份与回滚 ─────────────────────────

# 单条 UPDATE/DELETE 备份的行数上限：超出则截断并在工单标注（防快照爆库）。
BACKUP_MAX_ROWS = 10000


def extract_table_name(stmt: str) -> str:
    """从 UPDATE/DELETE 语句提取表名（支持 schema.table；反引号先剔除再匹配）。"""
    # 注意不走 _desensitize：反引号标识符正是提取目标（脱敏会把它替换为空）。
    normalized = _strip_comments(stmt).replace("`", "")
    match = re.match(r"^\s*(?:UPDATE|DELETE\s+FROM)\s+([\w.]+)", normalized, re.I)
    return match.group(1) if match else ""


def extract_where_clause(stmt: str) -> str:
    """提取 WHERE 子句（含），无 WHERE 返回空（审核规则已拦截该情况）。"""
    normalized = _strip_comments(stmt)
    match = re.search(r"\bWHERE\b[\s\S]+$", normalized, re.I)
    return match.group(0).strip() if match else ""


def build_backup_select(stmt: str) -> str | None:
    """为 UPDATE/DELETE 生成备份 SELECT（SELECT * FROM t WHERE ...）。

    只处理单表简单语句；JOIN/子查询等复杂形态返回 None（不备份也不阻塞执行，
    工单里标注"该语句未生成备份"由人工确认）。
    """
    # JOIN 形态的 WHERE 作用于联接结果，按单表备份语义不精确，保守跳过。
    normalized = _strip_comments(stmt)
    if re.search(r"\bJOIN\b", normalized, re.I):
        return None
    table = extract_table_name(stmt)
    if not table or not re.match(r"^[\w.]+$", table):
        return None
    where = extract_where_clause(stmt)
    if not where:
        return None
    return f"SELECT * FROM {table} {where}"


def build_rollback_inserts(table: str, columns: list[str], rows: list[dict[str, Any]], limit: int = BACKUP_MAX_ROWS) -> str:
    """把备份行渲染为回滚 INSERT 语句（值做 SQL 转义；NULL/数字/字符串分型处理）。"""
    def _quote(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{escaped}'"

    statements = []
    for row in rows[:limit]:
        values = ", ".join(_quote(row.get(col)) for col in columns)
        col_list = ", ".join(f"`{col}`" for col in columns)
        statements.append(f"INSERT INTO {table} ({col_list}) VALUES ({values});")
    return "\n".join(statements)


def snapshot_dangerous_statements(
    db_type: str, host: str, port: int, username: str, password: str, database: str,
    statements: list[str],
) -> dict[str, Any]:
    """执行前对 UPDATE/DELETE 语句做受影响行快照（SELECT 备份 + 回滚语句草稿）。

    返回 {items: [{sql, select, table, columns, rows, truncated}], skipped: [sql...]}。
    任何一条备份查询失败不阻塞工单（记入 skipped 由人工评估）。
    """
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    for stmt in statements:
        normalized = _desensitize(stmt)
        if not re.match(r"^\s*(UPDATE|DELETE)\b", normalized, re.I):
            continue
        select_sql = build_backup_select(stmt)
        if not select_sql:
            skipped.append(stmt[:200])
            continue
        try:
            result = execute_statements(
                db_type, host, port, username, password, database,
                [select_sql], fetch_rows=True,
            )
            if not result["ok"]:
                skipped.append(stmt[:200])
                continue
            rows = result["rows"][:BACKUP_MAX_ROWS]
            table = extract_table_name(stmt)
            items.append({
                "sql": stmt[:500],
                "select": select_sql,
                "table": table,
                "columns": result["columns"],
                "rows": rows,
                "row_count": len(result["rows"]),
                "truncated": len(result["rows"]) > BACKUP_MAX_ROWS,
            })
        except Exception:  # noqa: BLE001
            skipped.append(stmt[:200])
    return {"items": items, "skipped": skipped}
