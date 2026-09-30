"""根因反馈闭环的契约测试（提示词构建 / JSON 提取 / 准确率统计 / 历史检索）。"""
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.root_cause import AlertRootCause  # noqa: F401  注册进 Base
from app.services.alert_root_cause import (
    accuracy_stats,
    build_analysis_prompt,
    find_history_root_causes,
    _extract_json,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[AlertRootCause.__table__])
    return sessionmaker(bind=engine)()


def _record(db, alert_name="CPUHigh", rating="pending", human="", instance="node-1"):
    row = AlertRootCause(
        alert_name=alert_name, instance=instance, severity="warning",
        ai_rating=rating, human_root_cause=human, human_resolution="重启了",
        username="tester", created_at="2026-09-19 10:00:00",
        ai_diagnosis={"root_cause": "cpu 高", "impact": "", "suggestions": []},
    )
    db.add(row)
    db.commit()
    return row


class PromptContract(unittest.TestCase):
    def test_prompt_with_history(self):
        history = [{"human_root_cause": "内存泄漏", "instance": "a", "time": "t", "human_resolution": "重启"}]
        prompt = build_analysis_prompt({"alert_name": "CPUHigh"}, history)
        self.assertIn("历史人工根因", prompt)
        self.assertIn("内存泄漏", prompt)
        self.assertIn("CPUHigh", prompt)

    def test_prompt_without_history(self):
        prompt = build_analysis_prompt({"alert_name": "X"}, [])
        self.assertIn("暂无历史人工根因", prompt)


class ExtractJsonContract(unittest.TestCase):
    def test_plain_json(self):
        parsed = _extract_json('{"root_cause": "r", "impact": "i", "suggestions": []}')
        self.assertEqual(parsed["root_cause"], "r")

    def test_markdown_wrapped(self):
        parsed = _extract_json('```json\n{"root_cause": "r"}\n```')
        self.assertIsNotNone(parsed)

    def test_garbage_returns_none(self):
        self.assertIsNone(_extract_json("no json here"))
        self.assertIsNone(_extract_json('{"unrelated": 1}'))
        self.assertIsNone(_extract_json(""))


class HistoryAndStatsContract(unittest.TestCase):
    def test_history_only_human_feedback(self):
        db = _make_db()
        _record(db, human="内存泄漏")           # 有人工根因 → 应命中
        _record(db, human="")                   # 无人工根因 → 不应命中
        rows = find_history_root_causes(db, "CPUHigh")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["human_root_cause"], "内存泄漏")

    def test_accuracy_stats_math(self):
        db = _make_db()
        # CPUHigh: 2 准确 + 1 部分 + 1 不准 → 加权准确率 (2+0.5)/4 = 0.625
        _record(db, human="a", rating="accurate")
        _record(db, human="b", rating="accurate")
        _record(db, human="c", rating="partial")
        _record(db, human="d", rating="inaccurate")
        # MemHigh: pending 不计入统计
        _record(db, alert_name="MemHigh", human="x", rating="pending")
        stats = accuracy_stats(db)
        self.assertEqual(stats["total_rated"], 4)
        self.assertAlmostEqual(stats["overall_accuracy"], 0.625, places=3)
        self.assertEqual(stats["by_alert"][0]["alert_name"], "CPUHigh")
        self.assertEqual(stats["by_alert"][0]["total"], 4)

    def test_stats_empty(self):
        stats = accuracy_stats(_make_db())
        self.assertIsNone(stats["overall_accuracy"])


if __name__ == "__main__":
    unittest.main()
