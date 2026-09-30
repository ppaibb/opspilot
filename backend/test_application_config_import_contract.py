"""应用配置 `.env` 批量导入契约测试。

只覆盖纯函数：
- 解析器对注释、空行、引号、等号、`export` 前缀的处理；
- 非法键、缺等号、空键等错误路径；
- 敏感键正则的识别结果。

不触碰数据库，避免引入 FastAPI 依赖。
"""
import unittest

from app.api.applications import (
    CONFIG_IMPORT_SENSITIVE_PATTERN,
    _parse_env_import_content,
)


class EnvImportParserTest(unittest.TestCase):
    def test_parses_plain_kv(self):
        entries, errors = _parse_env_import_content("FOO=bar\nBAZ=qux\n")
        self.assertEqual([(e[1], e[2]) for e in entries], [("FOO", "bar"), ("BAZ", "qux")])
        self.assertEqual(errors, [])

    def test_ignores_comments_and_blank_lines(self):
        text = "\n# a comment\n\nFOO=bar\n# another\n"
        entries, errors = _parse_env_import_content(text)
        self.assertEqual([e[1] for e in entries], ["FOO"])
        self.assertEqual(errors, [])

    def test_strips_export_prefix(self):
        entries, _ = _parse_env_import_content("export FOO=bar\nexport BAZ=1\n")
        self.assertEqual([e[1] for e in entries], ["FOO", "BAZ"])

    def test_strips_outer_quotes(self):
        entries, _ = _parse_env_import_content('FOO="hello world"\nBAR=\'x y\'\n')
        values = {e[1]: e[2] for e in entries}
        self.assertEqual(values["FOO"], "hello world")
        self.assertEqual(values["BAR"], "x y")

    def test_inline_comment_for_unquoted_value(self):
        entries, _ = _parse_env_import_content("FOO=bar   # a trailing comment\n")
        self.assertEqual(entries[0][2], "bar")

    def test_inline_comment_preserved_when_quoted(self):
        """引号内的 `#` 属于值本身，不该被裁掉。"""
        entries, _ = _parse_env_import_content('FOO="value#with_hash"\n')
        self.assertEqual(entries[0][2], "value#with_hash")

    def test_missing_equals_is_reported(self):
        _, errors = _parse_env_import_content("FOO\nBAR=1\n")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 1)
        self.assertIn("=", errors[0][1])

    def test_invalid_key_is_reported(self):
        _, errors = _parse_env_import_content("1FOO=bar\nFOO-BAR=1\n")
        reported_keys = {msg for _, msg in errors}
        self.assertTrue(any("1FOO" in msg for msg in reported_keys))
        self.assertTrue(any("FOO-BAR" in msg for msg in reported_keys))

    def test_empty_key_is_reported(self):
        _, errors = _parse_env_import_content("=value\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("键为空", errors[0][1])

    def test_empty_value_is_allowed(self):
        entries, errors = _parse_env_import_content("FOO=\n")
        self.assertEqual(entries, [(1, "FOO", "")])
        self.assertEqual(errors, [])


class EnvImportSensitivePatternTest(unittest.TestCase):
    def test_common_sensitive_keys_match(self):
        for key in [
            "DB_PASSWORD",
            "JWT_SECRET",
            "GITHUB_TOKEN",
            "API_KEY",
            "APIKEY",
            "AWS_ACCESS_KEY",
            "STRIPE_PRIVATE_KEY",
        ]:
            self.assertIsNotNone(
                CONFIG_IMPORT_SENSITIVE_PATTERN.search(key),
                f"{key} 应该命中敏感键正则",
            )

    def test_ordinary_keys_do_not_match(self):
        for key in ["LOG_LEVEL", "PORT", "HOSTNAME", "FEATURE_FLAG"]:
            self.assertIsNone(
                CONFIG_IMPORT_SENSITIVE_PATTERN.search(key),
                f"{key} 不应命中敏感键正则",
            )


if __name__ == "__main__":
    unittest.main()
