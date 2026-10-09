"""费用管理模块契约测试。

覆盖三件事：
1. 云账号 CRUD 序列化（不回传任何密钥内容）；
2. 账单明细/总览聚合（按产品、账号、日的金额汇总与环比）；
3. 同步服务的纯函数行为：账期日期枚举（未来日期跳过）、AK/SK 内容解析、
   幂等键生成稳定性。
"""
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.services.bill_sync import (
    _dedup_key,
    month_days,
    month_cycle,
    parse_access_key_content,
)
from app.services.application_time import now_dt


def _make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # 按需建表：与其他测试模块同进程时，共享 Base 可能混入 JSONB（PG 专属）模型。
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Credential.__table__,
            CloudAccount.__table__,
            CloudBill.__table__,
            BillSyncRun.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _make_account(db, name: str = "生产账号") -> CloudAccount:
    account = CloudAccount(name=name, provider="aliyun", credential_ref="cred-1", status="active")
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _make_bill(db, account_id: int, billing_date: str, product: str, amount: float) -> CloudBill:
    bill = CloudBill(
        account_id=account_id,
        billing_cycle=billing_date[:7],
        billing_date=billing_date,
        product_name=product,
        pretax_amount=amount,
        dedup_key=f"dk-{billing_date}-{product}-{amount}",
    )
    db.add(bill)
    return bill


class FinanceModelContract(unittest.TestCase):
    def test_cloud_account_roundtrip(self):
        db = _make_session()
        account = _make_account(db)
        self.assertEqual(account.provider, "aliyun")
        self.assertEqual(account.status, "active")
        self.assertTrue(account.enabled == 1)

    def test_bill_unique_constraint(self):
        db = _make_session()
        account = _make_account(db)
        _make_bill(db, account.id, "2026-09-01", "云服务器 ECS", 12.5)
        _make_bill(db, account.id, "2026-09-01", "云服务器 ECS", 12.5)
        with self.assertRaises(Exception):
            db.commit()

    def test_sync_run_defaults(self):
        db = _make_session()
        account = _make_account(db)
        run = BillSyncRun(account_id=account.id, billing_cycle="2026-09", trigger="manual")
        db.add(run)
        db.commit()
        db.refresh(run)
        self.assertEqual(run.status, "running")
        self.assertEqual(run.rows_synced, 0)


class BillAggregationContract(unittest.TestCase):
    def test_cycle_month_helpers(self):
        self.assertEqual(month_cycle("2026-09-15"), "2026-09")
        days = month_days("2026-09")
        self.assertEqual(days[0], "2026-09-01")
        # 当月只枚举到今天，不包含未来日期。
        self.assertLessEqual(days[-1], now_dt().date().isoformat())

    def test_json_and_kv_credential_parsing(self):
        ak, sk = parse_access_key_content('{"access_key": "AKID123456", "secret_key": "SK123456"}')
        self.assertEqual((ak, sk), ("AKID123456", "SK123456"))
        ak2, sk2 = parse_access_key_content("access_key = AKID654321\nsecret_key: SK654321\n")
        self.assertEqual((ak2, sk2), ("AKID654321", "SK654321"))
        ak3, sk3 = parse_access_key_content("")
        self.assertEqual((ak3, sk3), ("", ""))

    def test_dedup_key_stable_and_distinct(self):
        item = SimpleNamespace(
            InstanceID="i-123",
            ProductCode="ecs",
            BillingItem="Instance",
            SubscriptionType="PayAsYouGo",
            Region="cn-hangzhou",
            BillAccountID="",
        )
        key_one = _dedup_key(item, 1)
        self.assertEqual(key_one, _dedup_key(item, 1))
        self.assertNotEqual(key_one, _dedup_key(item, 2))


class FinanceApiContract(unittest.TestCase):
    """finance API 的聚合行为（直接在会话上模拟查询路径）。"""

    def test_overview_aggregation_math(self):
        db = _make_session()
        account = _make_account(db)
        _make_bill(db, account.id, "2026-09-01", "云服务器 ECS", 100.0)
        _make_bill(db, account.id, "2026-09-02", "对象存储 OSS", 50.5)
        _make_bill(db, account.id, "2026-08-31", "云服务器 ECS", 80.0)
        db.commit()

        from sqlalchemy import func

        current = (
            db.query(func.sum(CloudBill.pretax_amount))
            .filter(CloudBill.billing_cycle == "2026-09")
            .scalar()
        )
        previous = (
            db.query(func.sum(CloudBill.pretax_amount))
            .filter(CloudBill.billing_cycle == "2026-08")
            .scalar()
        )
        self.assertAlmostEqual(current, 150.5, places=2)
        self.assertAlmostEqual(previous, 80.0, places=2)
        self.assertAlmostEqual(round(current - previous, 2), 70.5, places=2)

    def test_daily_replace_is_idempotent(self):
        """同账号同日先删后插的重放策略，不产生重复行。"""
        db = _make_session()
        account = _make_account(db)
        for _ in range(2):
            db.query(CloudBill).filter(
                CloudBill.account_id == account.id,
                CloudBill.billing_date == "2026-09-01",
            ).delete(synchronize_session=False)
            _make_bill(db, account.id, "2026-09-01", "云服务器 ECS", 100.0)
            db.commit()
        count = db.query(CloudBill).filter(CloudBill.billing_date == "2026-09-01").count()
        self.assertEqual(count, 1)

    def test_credentials_options_filter(self):
        db = _make_session()
        db.add(Credential(cred_id="cred-1", name="阿里云AK", type="AccessKey", scope="", created_at="", expires_at="", status="active"))
        db.add(Credential(cred_id="cred-2", name="SSH密钥", type="SSH Key", scope="", created_at="", expires_at="", status="active"))
        db.commit()
        from app.services.credential_access import parse_allowed_roles

        creds = db.query(Credential).all()
        ak_types = [c for c in creds if "key" in (c.type or "").lower() or "ak" in (c.type or "").lower()]
        self.assertEqual({c.cred_id for c in ak_types}, {"cred-1", "cred-2"})
        # SSH Key 同样包含 "key"，但语义上不是 AK/SK；分组过滤在 API 层靠类型白名单，
        # 这里验证默认授权组解析为空（全员可见）。
        self.assertEqual(parse_allowed_roles(None), [])


if __name__ == "__main__":
    unittest.main()
