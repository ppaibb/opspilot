"""R8 只读判定对抗测试：写副作用关键字无处藏身。

修复点：is_readonly_statement 的 CTE 体内 + 主查询此前只检查
INSERT/UPDATE/DELETE/MERGE，CALL/DO/GRANT/REVOKE/LOCK/VACUUM/ANALYZE/SET
藏进 WITH 包裹形态会被放行。本套件全量对抗：首关键字形态（回归守卫）、
WITH 包裹形态（修复点）、多 CTE / 嵌套 CTE / 注释伪装，以及反向用例
（数据文本里的关键字不得误伤）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.services.db_ops import audit_sql, is_readonly_statement

# 有写副作用、必须判非只读的语句（首关键字形态 + WITH 包裹形态共用）。
WRITE_STATEMENTS = [
    "CALL do_backup()",
    "DO $$ BEGIN UPDATE t SET a = 1; END $$",
    "GRANT SELECT ON orders TO evil",
    "REVOKE SELECT ON orders FROM evil",
    "LOCK TABLE orders IN ACCESS EXCLUSIVE MODE",
    "VACUUM ANALYZE orders",
    "ANALYZE orders",
    "SET default_transaction_read_only = off",
]


class FirstKeywordWritesAreNotReadonly(unittest.TestCase):
    """首关键字形态：白名单外的语句一律非只读（回归守卫）。"""

    def test_write_statements_are_rejected(self):
        for sql in WRITE_STATEMENTS:
            with self.subTest(sql=sql):
                self.assertFalse(is_readonly_statement(sql))


class WithWrappedWritesAreNotReadonly(unittest.TestCase):
    """WITH 包裹形态：体内 / 主查询都不能藏写副作用关键字（修复点）。"""

    def test_write_hidden_in_cte_body(self):
        for sql in WRITE_STATEMENTS:
            wrapped = f"WITH x AS ({sql}) SELECT * FROM x"
            with self.subTest(wrapped=wrapped):
                self.assertFalse(is_readonly_statement(wrapped))

    def test_write_in_main_query_after_cte(self):
        for sql in WRITE_STATEMENTS:
            wrapped = f"WITH x AS (SELECT 1) {sql}"
            with self.subTest(wrapped=wrapped):
                self.assertFalse(is_readonly_statement(wrapped))

    def test_write_hidden_in_second_cte(self):
        sql = "WITH a AS (SELECT 1), b AS (CALL collect_stats()) SELECT * FROM a"
        self.assertFalse(is_readonly_statement(sql))

    def test_write_hidden_in_nested_cte(self):
        sql = "WITH a AS (WITH b AS (GRANT SELECT ON t TO u) SELECT 1) SELECT * FROM a"
        self.assertFalse(is_readonly_statement(sql))

    def test_comment_camouflage_still_rejected(self):
        sql = "/* looks safe */ WITH x AS (SELECT 1) -- read only?\nGRANT SELECT ON t TO u"
        self.assertFalse(is_readonly_statement(sql))


class ReadableStatementsStayReadonly(unittest.TestCase):
    """反向对抗：数据文本里的关键字 / 纯查询不得被误伤。"""

    def test_keyword_inside_string_literal_is_data(self):
        sql = "WITH x AS (SELECT 'CALL p(); GRANT SELECT ON t TO u' AS note) SELECT * FROM x"
        self.assertTrue(is_readonly_statement(sql))

    def test_plain_cte_select_is_readonly(self):
        self.assertTrue(is_readonly_statement("WITH x AS (SELECT 1) SELECT * FROM x"))
        self.assertTrue(
            is_readonly_statement(
                "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b"
            )
        )

    def test_cte_dml_still_rejected(self):
        """既有防线不回退：体内 DML 与主查询 DML 依旧拒绝。"""
        self.assertFalse(
            is_readonly_statement("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x")
        )
        self.assertFalse(is_readonly_statement("WITH x AS (SELECT 1) DELETE FROM t"))


class AuditR8Integration(unittest.TestCase):
    """R8 规则集成：查询工单带 WITH 包裹写语句必须 block。"""

    def test_query_ticket_with_wrapped_grant_is_blocked(self):
        result = audit_sql("WITH x AS (SELECT 1) GRANT SELECT ON t TO u", "query")
        self.assertFalse(result["passed"])
        self.assertTrue(any(f["rule"] == "R8" for f in result["findings"]))

    def test_query_ticket_with_wrapped_call_is_blocked(self):
        result = audit_sql("WITH x AS (CALL collect_stats()) SELECT * FROM x", "query")
        self.assertFalse(result["passed"])

    def test_query_ticket_plain_select_passes(self):
        result = audit_sql("SELECT id FROM orders WHERE id = 1", "query")
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
