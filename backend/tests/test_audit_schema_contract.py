import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app import main
from app.db.database import Base


class AuditSchemaContractTest(unittest.TestCase):
    def test_ensure_audit_columns_adds_risk_fields_for_legacy_sqlite(self):
        """老库 audit_logs 缺 risk_level/risk_reason 时，启动补列必须补上。

        审计中间件每次写操作都写这两列，缺列会让所有写操作报错。
        """
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE audit_logs (
                    id INTEGER PRIMARY KEY,
                    time DATETIME,
                    user VARCHAR(100),
                    action VARCHAR(100),
                    resource VARCHAR(255),
                    ip VARCHAR(64),
                    result VARCHAR(20)
                )
                """
            )

        with patch.object(main, "engine", engine):
            main.ensure_model_columns()

        with engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(audit_logs)").fetchall()}
        self.assertIn("risk_level", columns)
        self.assertIn("risk_reason", columns)

    def test_ensure_model_columns_is_idempotent(self):
        """列已存在时不能再执行一次（补列必须幂等，否则启动即报错）。"""
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        with patch.object(main, "engine", engine):
            main.ensure_model_columns()
            main.ensure_model_columns()
        self.assertTrue(True)

    def test_ensure_model_columns_backfills_model_added_column(self):
        """回归：模型后加的列必须被补上。

        db_instances.allowed_roles 曾因 create_all 不加列而缺失，
        导致 GET /api/db/query-logs 整页 500（UndefinedColumn）。
        """
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE db_instances DROP COLUMN allowed_roles")

        with patch.object(main, "engine", engine):
            main.ensure_model_columns()

        with engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(db_instances)").fetchall()}
        self.assertIn("allowed_roles", columns)

        # 补上的列要真的能写能读
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO db_instances (name, host, username, allowed_roles) VALUES ('t','h','u','[]')"))
        with engine.begin() as conn:
            value = conn.execute(text("SELECT allowed_roles FROM db_instances WHERE name='t'")).scalar()
        self.assertEqual(value, "[]")


if __name__ == "__main__":
    unittest.main()
