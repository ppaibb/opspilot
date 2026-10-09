"""app.timefmt 统一时间工具的契约测试。"""
import unittest
from datetime import datetime, timedelta, timezone


class TestToLocalStr(unittest.TestCase):
    def test_naive_datetime_treated_as_utc(self):
        from app.timefmt import to_local_str

        naive = datetime(2026, 10, 3, 15, 14, 50)  # utcnow 落库约定
        offset = datetime.now().astimezone().utcoffset()
        expected = (naive + offset).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(to_local_str(naive), expected)

    def test_iso_string_with_z(self):
        from app.timefmt import to_local_str

        offset = datetime.now().astimezone().utcoffset()
        naive = datetime(2026, 10, 3, 15, 14, 50)
        expected = (naive + offset).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(to_local_str("2026-10-03T15:14:50Z", "%Y-%m-%d %H:%M"), expected)

    def test_aware_datetime_converts_by_own_zone(self):
        from app.timefmt import to_local_str

        aware = datetime(2026, 10, 3, 23, 14, 50, tzinfo=timezone(timedelta(hours=8)))
        expected = aware.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(to_local_str(aware), expected)

    def test_garbage_passthrough_and_empty(self):
        from app.timefmt import to_local_str

        self.assertEqual(to_local_str("不是时间"), "不是时间")
        self.assertEqual(to_local_str(""), "")
        self.assertEqual(to_local_str(None), "")


class TestLocalStrNow(unittest.TestCase):
    def test_format(self):
        from app.timefmt import local_str_now

        self.assertRegex(local_str_now(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestLocalStrToUtcNaive(unittest.TestCase):
    """筛选用：本地时间 → naive UTC（to_local_str 的逆向，落到 utcnow 约定的列上比较）。"""

    def test_roundtrip_with_to_local_str(self):
        from app.timefmt import local_str_to_utc_naive, to_local_str

        # 本地串 → naive UTC → 再序列化回本地串，必须原样
        local = "2026-10-08 16:28:03"
        utc_naive = local_str_to_utc_naive(local)
        self.assertIsNotNone(utc_naive)
        self.assertIsNone(utc_naive.tzinfo)
        self.assertEqual(to_local_str(utc_naive), local)

    def test_aware_value_converted_by_own_zone(self):
        from app.timefmt import local_str_to_utc_naive

        aware = datetime(2026, 10, 8, 23, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        utc_naive = local_str_to_utc_naive(aware)
        self.assertEqual(utc_naive, datetime(2026, 10, 8, 15, 0, 0))
        self.assertIsNone(utc_naive.tzinfo)

    def test_empty_and_garbage_return_none(self):
        from app.timefmt import local_str_to_utc_naive

        self.assertIsNone(local_str_to_utc_naive(""))
        self.assertIsNone(local_str_to_utc_naive("   "))
        self.assertIsNone(local_str_to_utc_naive("not-a-date"))
        self.assertIsNone(local_str_to_utc_naive(None))


if __name__ == "__main__":
    unittest.main()
