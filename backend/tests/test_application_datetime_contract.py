"""应用中心 DateTime 字段兼容契约测试。"""
from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.application import Application, ApplicationHealthSnapshot, ApplicationRelease
from app.services.application_health import run_application_health_snapshot_cycle
from app.services.application_releases import serialize_release


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class ApplicationDateTimeContractTest(unittest.TestCase):
    """验证应用中心内部使用 DateTime，外部响应仍兼容字符串。"""

    def setUp(self):
        self.db = _make_session()

    def test_release_serializer_formats_datetime_for_frontend(self):
        row = ApplicationRelease(
            release_id="REL-DT",
            app_id="APP-DT",
            version="1.0.0",
            env="prod",
            release_type="external",
            status="success",
            started_at=datetime(2026, 5, 14, 10, 30, 0),
            finished_at=datetime(2026, 5, 14, 10, 31, 2),
        )

        payload = serialize_release(row, {"APP-DT": "时间测试"})

        self.assertEqual("2026-05-14 10:30:00", payload["started_at"])
        self.assertEqual("2026-05-14 10:31:02", payload["finished_at"])

    def test_health_snapshot_cycle_writes_datetime_value(self):
        self.db.add(Application(app_id="APP-DT", name="时间测试", status="active", environment="prod"))
        self.db.commit()

        with patch("app.db.database.SessionLocal", return_value=self.db):
            written = run_application_health_snapshot_cycle()

        snapshot = self.db.query(ApplicationHealthSnapshot).first()
        self.assertEqual(1, written)
        self.assertIsInstance(snapshot.snapshot_time, datetime)


if __name__ == "__main__":
    unittest.main()
