"""数据库管理（SQL 工单平台）核心契约测试。

纯函数覆盖：语句切分（字符串/注释内分号）、分类与只读判定、
审核规则 R1-R9 的命中与放行、结果序列化。
"""
import unittest
import unittest.mock

from app.services.db_ops import (
    audit_sql,
    classify_statement,
    is_readonly_statement,
    split_sql_statements,
    _jsonable,
)
import datetime
import decimal


class SqlSplitContract(unittest.TestCase):
    def test_basic_split(self):
        stmts = split_sql_statements("SELECT 1; UPDATE t SET a=1 WHERE id=2; -- done")
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].upper().startswith("SELECT"))

    def test_semicolon_in_string_and_comment(self):
        sql = "SELECT ';' AS a; -- drop; this\nSELECT 2 /* ; hidden */;"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 2)

    def test_trailing_without_semicolon(self):
        self.assertEqual(len(split_sql_statements("SELECT 1")), 1)
        self.assertEqual(split_sql_statements("   "), [])

    def test_escaped_quote(self):
        stmts = split_sql_statements("SELECT 'it\\'s; fine'; SELECT 2;")
        self.assertEqual(len(stmts), 2)


class ClassifyContract(unittest.TestCase):
    def test_categories(self):
        cases = {
            "create table t(a int)": "ddl",
            "ALTER TABLE t ADD c int": "ddl",
            "insert into t values (1)": "dml",
            "delete from t where id=1": "dml",
            "select * from t": "dql",
            "show tables": "dql",
            "explain select 1": "dql",
            "commit": "tcl",
            "grant all on *.* to u": "utility",
            "random garbage": "unknown",
        }
        for sql, expected in cases.items():
            self.assertEqual(classify_statement(sql), expected, sql)

    def test_leading_comment_ignored(self):
        self.assertEqual(classify_statement("-- note\nselect 1"), "dql")

    def test_readonly(self):
        self.assertTrue(is_readonly_statement("SELECT * FROM t"))
        self.assertTrue(is_readonly_statement("  (SELECT 1)"))
        self.assertTrue(is_readonly_statement("WITH x AS (SELECT 1) SELECT * FROM x"))
        self.assertFalse(is_readonly_statement("UPDATE t SET a=1"))
        self.assertFalse(is_readonly_statement("SELECT * FROM t INTO OUTFILE '/x'"))


class AuditRulesContract(unittest.TestCase):
    def assert_block(self, sql: str, rule: str):
        result = audit_sql(sql)
        blocks = [f for f in result["findings"] if f["level"] == "block"]
        self.assertFalse(result["passed"], f"{sql} 应被拦截")
        self.assertIn(rule, [f["rule"] for f in blocks], sql)

    def assert_pass(self, sql: str):
        result = audit_sql(sql)
        self.assertTrue(result["passed"], f"{sql} 不应被拦截: {result['findings']}")
        self.assertFalse([f for f in result["findings"] if f["level"] == "block"])

    def test_r1_r2_r3_destructive(self):
        self.assert_block("DROP DATABASE prod;", "R1")
        self.assert_block("drop schema if exists x;", "R1")
        self.assert_block("DROP TABLE orders;", "R2")
        self.assert_block("TRUNCATE TABLE logs;", "R3")

    def test_r4_missing_where(self):
        self.assert_block("DELETE FROM users;", "R4")
        self.assert_block("UPDATE users SET status=0;", "R4")
        self.assert_pass("DELETE FROM users WHERE id=1;")
        self.assert_pass("UPDATE users SET status=0 WHERE id=1;")

    def test_r5_select_star_warn_only(self):
        result = audit_sql("SELECT * FROM t;")
        self.assertTrue(result["passed"])  # warn 不拦截
        self.assertIn("R5", [f["rule"] for f in result["findings"]])

    def test_r6_alter_drop_warn(self):
        result = audit_sql("ALTER TABLE t DROP COLUMN c;")
        self.assertTrue(result["passed"])
        self.assertIn("R6", [f["rule"] for f in result["findings"]])

    def test_r8_query_ticket_readonly_only(self):
        result = audit_sql("SELECT 1; DELETE FROM t WHERE id=1;", ticket_type="query")
        self.assertFalse(result["passed"])
        self.assertIn("R8", [f["rule"] for f in result["findings"] if f["level"] == "block"])

    def test_empty_sql_blocked(self):
        result = audit_sql("   ")
        self.assertFalse(result["passed"])
        self.assertEqual(result["findings"][0]["rule"], "R0")

    def test_dangerous_sql_hiding_in_string_is_not_blocked(self):
        # 字符串里的 "drop table" 是数据不是语句，不应误伤。
        self.assert_pass("INSERT INTO t(msg) VALUES ('drop table users;');")

    def test_multi_statement_index_labeling(self):
        result = audit_sql("SELECT * FROM a; DROP TABLE b;")
        blocks = [f for f in result["findings"] if f["level"] == "block"]
        self.assertEqual(blocks[0]["statement_index"], 1)
        self.assertIn("第 2 条", blocks[0]["message"])


class JsonableContract(unittest.TestCase):
    def test_value_conversion(self):
        self.assertEqual(_jsonable(b"abc"), "abc")
        self.assertEqual(_jsonable(decimal.Decimal("1.5")), 1.5)
        self.assertIsInstance(_jsonable(datetime.datetime(2026, 1, 1, 10, 0)), str)
        self.assertEqual(_jsonable(42), 42)
        self.assertEqual(_jsonable(None), None)


if __name__ == "__main__":
    unittest.main()


class BackupRollbackContract(unittest.TestCase):
    """备份 SELECT 构造与回滚 INSERT 渲染的契约测试（不依赖真实数据库）。"""

    def test_extract_table_and_where(self):
        from app.services.db_ops import build_backup_select, extract_table_name
        sql = "UPDATE `app`.`orders` SET status=1 WHERE id=42 AND tenant='x'"
        self.assertEqual(extract_table_name(sql), "app.orders")
        backup = build_backup_select(sql)
        self.assertIn("SELECT * FROM app.orders WHERE id=42", backup)
        delete_sql = "DELETE FROM users WHERE id IN (1,2)"
        self.assertIn("FROM users WHERE id IN", build_backup_select(delete_sql))

    def test_no_where_returns_none(self):
        from app.services.db_ops import build_backup_select
        self.assertIsNone(build_backup_select("DELETE FROM users"))

    def test_complex_statement_skipped(self):
        from app.services.db_ops import build_backup_select
        # JOIN 形态（表名带空格/复杂）不备份。
        self.assertIsNone(build_backup_select("UPDATE a JOIN b ON a.id=b.id SET a.x=1 WHERE a.id=1"))

    def test_rollback_insert_rendering(self):
        from app.services.db_ops import build_rollback_inserts
        sql = build_rollback_inserts(
            "app.orders", ["id", "status", "note"],
            [{"id": 1, "status": 0, "note": "it's"}, {"id": 2, "status": None, "note": None}],
        )
        lines = sql.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("`id`, `status`, `note`", lines[0])
        self.assertIn("1, 0, 'it''s'", lines[0])  # 单引号转义
        self.assertIn("NULL", lines[1])           # NULL 不带引号


class RuleConfigContract(unittest.TestCase):
    """审核规则可配置：开关禁用与级别调整。"""

    def test_disable_r2_allows_drop_table(self):
        result = audit_sql("DROP TABLE x;", rule_config={"R2": {"enabled": False}})
        self.assertTrue(result["passed"])  # R2 关闭后不再拦截（R1 不命中 DROP TABLE）

    def test_downgrade_r4_to_warn(self):
        result = audit_sql("DELETE FROM t;", rule_config={"R4": {"level": "warn"}})
        self.assertTrue(result["passed"])  # 降级为 warn 不再阻断
        r4 = [f for f in result["findings"] if f["rule"] == "R4"]
        self.assertEqual(r4[0]["level"], "warn")

    def test_default_unchanged_without_config(self):
        self.assertFalse(audit_sql("DROP TABLE x;")["passed"])


class ExecuteRowCountContract(unittest.TestCase):
    """执行引擎的影响行数必须取自 cursor.rowcount，不能取自 execute() 的返回值。

    两个驱动的 execute() 返回类型不同：pymysql 返回 int 行数，
    psycopg3 返回 cursor 自身。早期代码 `int(cursor.execute(stmt) or 0)`
    在 PostgreSQL 上必然抛 `int() argument must be ... not 'Cursor'`，
    导致自助查询对 PG 数据源全部失败。
    """

    def _run(self, cursor_cls):
        from app.services import db_ops

        class _Conn:
            def cursor(self):
                return cursor_cls()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        with unittest.mock.patch.object(db_ops, "_build_connection", lambda *a, **k: _Conn()):
            return db_ops.execute_statements(
                db_type="postgresql", host="h", port=5432,
                username="u", password="p", database="d",
                statements=["SELECT 1"], fetch_rows=True,
            )

    def test_psycopg_style_cursor_returning_self(self):
        """psycopg3：execute() 返回 cursor 自身，rowcount 才是行数。"""

        class _PsycopgCursor:
            rowcount = 1
            description = (("?column?",),)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, _stmt):
                return self

            def fetchmany(self, _n):
                return [(1,)]

        result = self._run(_PsycopgCursor)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["rows_affected"], 1)
        self.assertEqual(len(result["rows"]), 1)

    def test_pymysql_style_cursor_returning_int(self):
        """pymysql：execute() 返回 int，rowcount 同样为 1（两条路径结果一致）。"""

        class _PymysqlCursor:
            rowcount = 1
            description = (("1",),)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, _stmt):
                return 1

            def fetchmany(self, _n):
                return [(1,)]

        result = self._run(_PymysqlCursor)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["rows_affected"], 1)

    def test_negative_rowcount_clamped_to_zero(self):
        """rowcount 为 -1（无法确定）时不能把 -1 当成影响行数返回给前端。"""

        class _UnknownCursor:
            rowcount = -1
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, _stmt):
                return None

        result = self._run(_UnknownCursor)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["rows_affected"], 0)


class FriendlyErrorContract(unittest.TestCase):
    """驱动报错必须翻成中文可操作提示（hint），原始错误原文保留。

    起因：用户查 `db_instances` 报 `relation "db_instances" does not exist`，
    完全看不出该干嘛——实际是查询连到了 postgres 维护库。
    """

    def _pg_error(self, message, sqlstate=None):
        exc = Exception(message)
        exc.sqlstate = sqlstate
        return exc

    def test_pg_undefined_table_names_the_relation_and_the_database(self):
        """42P01：点出表名 + 提示切库，并说明当前连的是哪个库。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error(
            "postgresql",
            self._pg_error('relation "db_instances" does not exist\nLINE 1: SELECT ...', "42P01"),
            "postgres",
        )
        self.assertIn("db_instances", hint)
        self.assertIn("postgres", hint)
        self.assertIn("库", hint)
        # 不能把英文原文直接甩给用户
        self.assertNotIn("relation", hint)

    def test_pg_undefined_table_without_relation_name_still_readable(self):
        """原文里抠不到 relation 名时，提示也要是完整的中文句子。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error("postgresql", self._pg_error("undefined_table", "42P01"), "opspilot")
        self.assertIn("表不存在", hint)
        self.assertIn("opspilot", hint)

    def test_pg_undefined_column(self):
        """42703：列名写错，提示大小写敏感这个 PostgreSQL 特有的坑。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error(
            "postgresql",
            self._pg_error('column "nope" does not exist', "42703"),
            "opspilot",
        )
        self.assertIn("列不存在", hint)
        self.assertIn("大小写", hint)

    def test_pg_permission_denied(self):
        """42501：权限不足，提示去找 DBA。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error("postgresql", self._pg_error("permission denied", "42501"), "d")
        self.assertIn("权限不足", hint)

    def test_pg_bad_password_detected_from_message(self):
        """错密码在驱动层 sqlstate 是 None，只能按消息特征判断。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error(
            "postgresql",
            self._pg_error('connection failed: FATAL:  password authentication failed for user "u"'),
            "opspilot",
        )
        self.assertIn("密码认证失败", hint)
        self.assertNotIn("网络", hint)

    def test_pg_timeout_is_not_reported_as_bad_password(self):
        """超时和错密码是两回事，不能混成一条提示。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error("postgresql", self._pg_error("connection timeout expired"), "d")
        self.assertIn("超时", hint)
        self.assertNotIn("密码", hint)

    def test_mysql_unknown_table(self):
        """MySQL 1146 同样要点出"可能连错库"。"""
        from app.services.db_ops import _friendly_error

        exc = Exception(1146, "Table 'x.y' doesn't exist")
        hint = _friendly_error("mysql", exc, "business")
        self.assertIn("表不存在", hint)
        self.assertIn("business", hint)

    def test_mysql_access_denied(self):
        """MySQL 1045：密码错。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error("mysql", Exception(1045, "Access denied for user"), "d")
        self.assertIn("密码认证失败", hint)

    def test_unknown_error_falls_back_to_first_line(self):
        """认不出来的错误也要给个说法，不能返回空。"""
        from app.services.db_ops import _friendly_error

        hint = _friendly_error("postgresql", self._pg_error("something odd\nsecond line"), "d")
        self.assertIn("something odd", hint)
        self.assertNotIn("second line", hint)

    def test_execute_statements_adds_hint_on_failure(self):
        """execute_statements 的失败结果里必须带 hint 字段。"""
        from app.services import db_ops

        class _Cursor:
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, _stmt):
                raise db_ops_error()

        class _Conn:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        class db_ops_error(Exception):
            sqlstate = "42P01"

            def __str__(self):
                return 'relation "nope" does not exist'

        with unittest.mock.patch.object(db_ops, "_build_connection", lambda *a, **k: _Conn()):
            result = db_ops.execute_statements(
                db_type="postgresql", host="h", port=5432,
                username="u", password="p", database="d",
                statements=["SELECT * FROM nope"], fetch_rows=True,
            )
        self.assertFalse(result["ok"])
        self.assertIn("hint", result["results"][0])
        self.assertIn("nope", result["results"][0]["hint"])

    def test_execute_statements_reports_connection_failure(self):
        """连接就建不起来时也要返回带 hint 的结果，不能把异常抛给调用方。"""
        from app.services import db_ops

        def _boom(*_a, **_k):
            exc = Exception("connection failed: FATAL:  password authentication failed")
            exc.sqlstate = None
            raise exc

        with unittest.mock.patch.object(db_ops, "_build_connection", _boom):
            result = db_ops.execute_statements(
                db_type="postgresql", host="h", port=5432,
                username="u", password="p", database="d",
                statements=["SELECT 1"], fetch_rows=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("密码认证失败", result["results"][0]["hint"])


class SelectableDatabasesContract(unittest.TestCase):
    """可选目标库必须剔掉维护库，否则用户会误连然后查不到表。"""

    def test_pg_maintenance_databases_hidden(self):
        from app.services.db_ops import filter_selectable_databases

        got = filter_selectable_databases(
            "postgresql", ["opspilot", "postgres", "template0", "template1"], "opspilot"
        )
        self.assertEqual(got, ["opspilot"])

    def test_mysql_maintenance_databases_hidden(self):
        from app.services.db_ops import filter_selectable_databases

        got = filter_selectable_databases(
            "mysql",
            ["business", "information_schema", "mysql", "performance_schema", "sys"],
            "business",
        )
        self.assertEqual(got, ["business"])

    def test_instance_own_database_always_kept(self):
        """实例自己配的库即便叫 postgres 也要保留，否则默认库下拉会空掉。"""
        from app.services.db_ops import filter_selectable_databases

        got = filter_selectable_databases("postgresql", ["postgres", "opspilot"], "postgres")
        self.assertIn("postgres", got)
        self.assertEqual(got[0], "postgres")

    def test_no_maintenance_names_left(self):
        from app.services.db_ops import filter_selectable_databases

        got = filter_selectable_databases(
            "postgresql", ["a", "postgres", "b", "template0", "c"], "a"
        )
        self.assertEqual(got, ["a", "b", "c"])


class CteReadonlyBypassContract(unittest.TestCase):
    """CTE 只读绕过修复：WITH 体内（以及主查询）的写语句必须判非只读。

    PostgreSQL 允许 `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`
    这类"体内写"，旧实现只看首关键字 WITH 会直接放行。
    """

    def test_cte_delete_body_rejected(self):
        sql = "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"
        self.assertFalse(is_readonly_statement(sql))

    def test_cte_update_body_rejected(self):
        self.assertFalse(is_readonly_statement("WITH x AS (UPDATE t SET a=1) SELECT * FROM x"))

    def test_cte_insert_and_merge_body_rejected(self):
        self.assertFalse(is_readonly_statement("WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM x"))
        self.assertFalse(is_readonly_statement("WITH x AS (MERGE INTO t USING s ON t.id=s.id) SELECT 1"))

    def test_multi_cte_one_write_rejected(self):
        sql = "WITH a AS (SELECT 1), b AS (DELETE FROM t WHERE id=1) SELECT * FROM a"
        self.assertFalse(is_readonly_statement(sql))

    def test_nested_cte_write_rejected(self):
        sql = "WITH a AS (WITH b AS (DELETE FROM t RETURNING *) SELECT * FROM b) SELECT * FROM a"
        self.assertFalse(is_readonly_statement(sql))

    def test_recursive_normal_query_allowed(self):
        sql = (
            "WITH RECURSIVE n AS ("
            "SELECT 1 UNION ALL SELECT n+1 FROM n WHERE n < 5) SELECT * FROM n"
        )
        self.assertTrue(is_readonly_statement(sql))

    def test_main_query_write_after_cte_rejected(self):
        # PG 合法写法：WITH ... DELETE FROM t USING x。
        self.assertFalse(is_readonly_statement("WITH x AS (SELECT id FROM t) DELETE FROM t USING x"))

    def test_write_hidden_in_string_not_flagged(self):
        sql = "WITH x AS (SELECT 'delete from t' AS msg) SELECT * FROM x"
        self.assertTrue(is_readonly_statement(sql))

    def test_write_keyword_in_comment_not_flagged(self):
        sql = "WITH x AS (SELECT 1 /* delete from t */ -- update t\n) SELECT * FROM x"
        self.assertTrue(is_readonly_statement(sql))

    def test_cte_into_outfile_rejected(self):
        self.assertFalse(
            is_readonly_statement("WITH x AS (SELECT 1 INTO OUTFILE '/tmp/f') SELECT * FROM x")
        )

    def test_paren_wrapped_cte_write_rejected(self):
        self.assertFalse(
            is_readonly_statement("(WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x)")
        )

    def test_plain_cte_select_still_allowed(self):
        # 回归：正常 CTE 查询不受影响；列名列表形态（name (a,b) AS）也能解析。
        self.assertTrue(is_readonly_statement("WITH x AS (SELECT 1) SELECT * FROM x"))
        self.assertTrue(
            is_readonly_statement("WITH x(a, b) AS (SELECT 1, 2) SELECT a, b FROM x")
        )

    def test_case_and_whitespace_insensitive(self):
        sql = "with x\nas\n(delete from t returning *) select * from x"
        self.assertFalse(is_readonly_statement(sql))


class SessionReadonlyExecutionContract(unittest.TestCase):
    """自助查询的会话级只读兜底：readonly=True 时先发 SET 再执行用户 SQL。"""

    class _RecordingCursor:
        def __init__(self, executed):
            self._executed = executed
            self.rowcount = 1
            self.description = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt):
            self._executed.append(stmt)

        def fetchmany(self, _n):
            return []

    class _RecordingConn:
        def __init__(self, fail_on=None):
            self.executed = []
            self.commits = 0
            self.fail_on = fail_on

        def cursor(self):
            return SessionReadonlyExecutionContract._RecordingCursor(self.executed)

        def commit(self):
            self.commits += 1

        def rollback(self):
            pass

        def close(self):
            pass

    def _run(self, db_type: str, conn):
        from app.services import db_ops

        with unittest.mock.patch.object(db_ops, "_build_connection", lambda *a, **k: conn):
            return db_ops.execute_statements(
                db_type=db_type, host="h", port=0, username="u", password="p",
                database="d", statements=["SELECT 1"], fetch_rows=True, readonly=True,
            )

    def test_pg_sets_default_transaction_read_only_before_user_sql(self):
        conn = self._RecordingConn()
        result = self._run("postgresql", conn)
        self.assertTrue(result["ok"])
        self.assertEqual(result["readonly_notice"], "")
        self.assertEqual(conn.executed[0], "SET default_transaction_read_only = on")
        self.assertEqual(conn.executed[1], "SELECT 1")
        # SET 只对后续事务生效，必须立即 commit 让用户语句开在新只读事务里。
        self.assertGreaterEqual(conn.commits, 2)

    def test_mysql_sets_session_transaction_read_only(self):
        conn = self._RecordingConn()
        result = self._run("mysql", conn)
        self.assertTrue(result["ok"])
        self.assertEqual(conn.executed[0], "SET SESSION transaction_read_only = 1")
        self.assertEqual(conn.executed[1], "SELECT 1")

    def test_mysql_unsupported_version_skips_with_notice(self):
        """MySQL 5.6 等不支持该变量时：跳过、不阻塞执行、结果附提示。"""

        class _LegacyConn(self._RecordingConn):
            def cursor(self):
                cursor = SessionReadonlyExecutionContract._RecordingCursor(self.executed)

                original_execute = cursor.execute

                def execute(stmt):
                    if "transaction_read_only" in stmt:
                        raise Exception(1193, "Unknown system variable 'transaction_read_only'")
                    original_execute(stmt)

                cursor.execute = execute
                return cursor

        conn = _LegacyConn()
        result = self._run("mysql", conn)
        self.assertTrue(result["ok"])  # 只读设置失败不阻塞查询
        self.assertEqual(conn.executed, ["SELECT 1"])
        self.assertIn("不支持会话级只读", result["readonly_notice"])

    def test_readonly_false_keeps_legacy_behavior(self):
        """默认不改变行为：不发 SET，响应里也没有 readonly_notice 字段。"""
        from app.services import db_ops

        conn = self._RecordingConn()
        with unittest.mock.patch.object(db_ops, "_build_connection", lambda *a, **k: conn):
            result = db_ops.execute_statements(
                db_type="postgresql", host="h", port=0, username="u", password="p",
                database="d", statements=["SELECT 1"], fetch_rows=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(conn.executed, ["SELECT 1"])
        self.assertNotIn("readonly_notice", result)
