"""finance 模块 API 端到端验证（直接调用路由函数 + 内存库 + 管理员用户桩）。

不使用 TestClient：当前环境 httpx 0.28 与 starlette TestClient(app=...) 不兼容，
而路由函数本身就是可直调的依赖注入函数，直接调用等价且更轻。

验证内容：
1. 云账号 CRUD + 凭证下拉（不触网：凭证内容为空时创建成功但状态 error）；
2. 账单明细分页、关键词筛选与合计；
3. 总览聚合（total/环比/维度/日趋势/月趋势）；
4. group-by 聚合与非法维度 422；
5. 同步记录接口与不存在账号 404。

用法: .venv/Scripts/python.exe -m unittest test_finance_api_e2e -v
"""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.models.system_config import SystemConfig  # noqa: F401  确保 create_all 建表
from app.models.user import User

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
# 只建本套件需要的表：与其他测试模块同进程运行时，共享 Base 里可能混入
# PostgreSQL 专属类型（JSONB）的模型，全量 create_all 会在 SQLite 上编译失败。
Base.metadata.create_all(
    bind=engine,
    tables=[
        SystemConfig.__table__,
        Credential.__table__,
        CloudAccount.__table__,
        CloudBill.__table__,
        BillSyncRun.__table__,
        User.__table__,
    ],
)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

ADMIN = User(id=1, username="admin", role="管理员", status="active")

from app.api import finance  # noqa: E402


def db_session():
    db = TestingSession()
    try:
        return db
    finally:
        pass


class FinanceApiE2E(unittest.TestCase):
    def setUp(self):
        self.db = TestingSession()
        self.db.query(CloudBill).delete()
        self.db.query(CloudAccount).delete()
        self.db.query(Credential).delete()
        self.db.commit()
        self.db.add(Credential(
            cred_id="cred-ak", name="阿里云AK", type="AccessKey", scope="",
            created_at="", expires_at="", status="active", content="{}",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_credentials_options_lists_ak_only(self):
        options = finance.list_credential_options(db=self.db, current_user=ADMIN)
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].cred_id, "cred-ak")

    def test_account_crud_flow(self):
        created = finance.create_cloud_account(
            finance.CloudAccountCreate(name="生产账号", credential_ref="cred-ak", enabled=True),
            db=self.db, current_user=ADMIN,
        )
        self.assertEqual(created.name, "生产账号")
        # 凭证内容为空 JSON → 解析不出 AK/SK → 不阻断创建，状态 error。
        self.assertEqual(created.status, "error")
        self.assertIn("AccessKey", created.status_message)

        listing = finance.list_cloud_accounts(db=self.db, current_user=ADMIN)
        self.assertEqual(len(listing), 1)

        updated = finance.update_cloud_account(
            created.id,
            finance.CloudAccountCreate(name="生产账号-改名", credential_ref="cred-ak", enabled=False),
            db=self.db, current_user=ADMIN,
        )
        self.assertEqual(updated.name, "生产账号-改名")
        self.assertFalse(updated.enabled)

        result = finance.delete_cloud_account(created.id, db=self.db, current_user=ADMIN)
        self.assertIn("已删除", result["detail"])
        self.assertEqual(len(finance.list_cloud_accounts(db=self.db, current_user=ADMIN)), 0)

    def test_account_validation(self):
        with self.assertRaises(HTTPException) as ctx:
            finance.create_cloud_account(
                finance.CloudAccountCreate(name="", credential_ref="cred-ak"),
                db=self.db, current_user=ADMIN,
            )
        self.assertEqual(ctx.exception.status_code, 422)

        with self.assertRaises(HTTPException) as ctx:
            finance.create_cloud_account(
                finance.CloudAccountCreate(name="腾讯", credential_ref="cred-ak", provider="tencent"),
                db=self.db, current_user=ADMIN,
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_bills_list_and_filter(self):
        self.db.add(CloudAccount(id=9, name="测试账号", provider="aliyun", credential_ref="cred-ak", status="active"))
        for date, product, amount in [
            ("2026-09-01", "云服务器 ECS", 100.0),
            ("2026-09-02", "云服务器 ECS", 60.0),
            ("2026-09-02", "对象存储 OSS", 40.5),
            ("2026-08-20", "云服务器 ECS", 80.0),
        ]:
            self.db.add(CloudBill(
                account_id=9, billing_cycle=date[:7], billing_date=date,
                product_name=product, pretax_amount=amount, dedup_key=f"{date}-{product}-{amount}",
            ))
        self.db.commit()

        bills = finance.list_cloud_bills(month="2026-09", db=self.db, current_user=ADMIN,
                                         account_id=None, product="", keyword="",
                                         subscription_type="", page=1, page_size=20)
        self.assertEqual(bills.total, 3)
        self.assertAlmostEqual(bills.grand_total, 200.5, places=2)
        self.assertAlmostEqual(bills.items[0].pretax_amount, 100.0, places=2)
        self.assertEqual(bills.items[0].account_name, "测试账号")

        filtered = finance.list_cloud_bills(month="2026-09", keyword="OSS", db=self.db, current_user=ADMIN,
                                            account_id=None, product="", subscription_type="",
                                            page=1, page_size=20)
        self.assertEqual(filtered.total, 1)
        self.assertEqual(filtered.items[0].product_name, "对象存储 OSS")

    def test_overview_aggregation(self):
        self.db.add(CloudAccount(id=9, name="测试账号", provider="aliyun", credential_ref="cred-ak", status="active"))
        for date, product, amount in [
            ("2026-09-01", "云服务器 ECS", 100.0),
            ("2026-09-02", "云服务器 ECS", 60.0),
            ("2026-09-02", "对象存储 OSS", 40.5),
            ("2026-08-20", "云服务器 ECS", 80.0),
        ]:
            self.db.add(CloudBill(
                account_id=9, billing_cycle=date[:7], billing_date=date,
                product_name=product, pretax_amount=amount, dedup_key=f"k-{date}-{product}-{amount}",
            ))
        self.db.commit()

        data = finance.finance_overview(month="2026-09", db=self.db, current_user=ADMIN)
        self.assertAlmostEqual(data.total, 200.5, places=2)
        self.assertAlmostEqual(data.prev_total, 80.0, places=2)
        self.assertAlmostEqual(data.mom_delta, 120.5, places=2)
        self.assertEqual(len(data.by_product), 2)
        self.assertEqual(data.by_product[0].name, "云服务器 ECS")
        self.assertAlmostEqual(data.by_product[0].amount, 160.0, places=2)
        self.assertEqual(len(data.daily_trend), 2)
        self.assertEqual(len(data.monthly_trend), 6)
        self.assertEqual(len(data.by_account), 1)

    def test_group_by_dimensions(self):
        self.db.add(CloudAccount(id=9, name="测试账号", provider="aliyun", credential_ref="cred-ak", status="active"))
        self.db.add(CloudBill(account_id=9, billing_cycle="2026-09", billing_date="2026-09-01",
                              product_name="云服务器 ECS", instance_id="i-1", pretax_amount=30.0, dedup_key="g1"))
        self.db.add(CloudBill(account_id=9, billing_cycle="2026-09", billing_date="2026-09-01",
                              product_name="云服务器 ECS", instance_id="i-2", pretax_amount=70.0, dedup_key="g2"))
        self.db.commit()

        grouped = finance.group_cloud_bills(dimension="product", month="2026-09", db=self.db,
                                            current_user=ADMIN, account_id=None)
        self.assertEqual(len(grouped.items), 1)
        self.assertAlmostEqual(grouped.items[0].amount, 100.0, places=2)
        self.assertAlmostEqual(grouped.items[0].ratio, 1.0, places=4)

        instances = finance.group_cloud_bills(dimension="instance", month="2026-09", db=self.db,
                                              current_user=ADMIN, account_id=None)
        self.assertEqual({i.name for i in instances.items}, {"i-1", "i-2"})

        with self.assertRaises(HTTPException) as ctx:
            finance.group_cloud_bills(dimension="nope", month="2026-09", db=self.db,
                                      current_user=ADMIN, account_id=None)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_sync_records_and_404(self):
        records = finance.list_sync_records(account_id=999, limit=20, db=self.db, current_user=ADMIN)
        self.assertEqual(records, [])
        with self.assertRaises(HTTPException) as ctx:
            finance.trigger_account_sync(
                999, finance.BillSyncRequest(), db=self.db, current_user=ADMIN,
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FinanceBudgetAndInstanceCostContract(unittest.TestCase):
    """预算配置与实例费用联动接口的契约测试。"""

    def setUp(self):
        self.db = TestingSession()
        self.db.query(CloudBill).delete()
        self.db.query(CloudAccount).delete()
        from app.models.system_config import SystemConfig as SC
        self.db.query(SC).filter(SC.config_key == "finance_budget_config").delete()
        self.db.commit()
        # 造数：i-123 实例 9 月两笔、8 月一笔。
        self.db.add(CloudAccount(id=9, name="测试账号", provider="aliyun", credential_ref="cred-ak", status="active"))
        for date, item, amount in [
            ("2026-09-01", "CPU 费用", 60.0),
            ("2026-09-02", "磁盘费用", 40.0),
            ("2026-08-15", "CPU 费用", 80.0),
        ]:
            self.db.add(CloudBill(
                account_id=9, billing_cycle=date[:7], billing_date=date, instance_id="i-123",
                billing_item_name=item, pretax_amount=amount, dedup_key=f"ic-{date}-{item}",
            ))
        self.db.commit()

    def tearDown(self):
        from app.models.system_config import SystemConfig as SC
        self.db.query(SC).filter(SC.config_key == "finance_budget_config").delete()
        self.db.rollback()
        self.db.close()

    def test_instance_cost_summary(self):
        data = finance.get_instance_cost("i-123", months=2, db=self.db, current_user=ADMIN)
        self.assertTrue(data.matched)
        self.assertEqual(data.current_month, "2026-09")
        self.assertAlmostEqual(data.current_month_amount, 100.0, places=2)
        self.assertAlmostEqual(data.previous_month_amount, 80.0, places=2)
        self.assertEqual(len(data.monthly), 2)
        # 计费项分解按金额降序。
        self.assertEqual(data.by_billing_item[0].name, "CPU 费用")
        self.assertAlmostEqual(data.by_billing_item[0].amount, 60.0, places=2)

    def test_instance_cost_no_match(self):
        data = finance.get_instance_cost("i-not-exist", months=1, db=self.db, current_user=ADMIN)
        self.assertFalse(data.matched)
        self.assertEqual(data.current_month_amount, 0.0)

    def test_budget_crud_and_validation(self):
        # 默认：未启用。
        default = finance.get_budget_config(db=self.db, current_user=ADMIN)
        self.assertFalse(default.enabled)
        self.assertEqual(default.monthly_budget, 0.0)

        # 保存合法配置。
        saved = finance.update_budget_config(
            finance.BudgetConfigUpdate(monthly_budget=500.0, warn_ratio=0.8, critical_ratio=1.0, enabled=True),
            db=self.db, current_user=ADMIN,
        )
        self.assertTrue(saved.enabled)
        self.assertAlmostEqual(saved.monthly_budget, 500.0, places=2)

        # 阈值顺序校验。
        with self.assertRaises(HTTPException) as ctx:
            finance.update_budget_config(
                finance.BudgetConfigUpdate(monthly_budget=100, warn_ratio=1.2, critical_ratio=0.8),
                db=self.db, current_user=ADMIN,
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # 启用但预算为 0 → 422。
        with self.assertRaises(HTTPException) as ctx:
            finance.update_budget_config(
                finance.BudgetConfigUpdate(monthly_budget=0, enabled=True),
                db=self.db, current_user=ADMIN,
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_budget_config_read_write_helpers(self):
        # 未写入时回落默认值（enabled=False）。
        config = finance._read_budget_config(self.db)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["monthly_budget"], 0.0)
        # 写入后读回保持一致。
        finance._write_budget_config(self.db, {
            "monthly_budget": 888.0, "warn_ratio": 0.8, "critical_ratio": 1.0,
            "enabled": True, "updated_at": "2026-09-18 10:00:00",
        })
        config = finance._read_budget_config(self.db)
        self.assertTrue(config["enabled"])
        self.assertAlmostEqual(config["monthly_budget"], 888.0, places=2)
        self.assertAlmostEqual(config["critical_ratio"], 1.0, places=4)
