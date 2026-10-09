"""定时任务配置与注册表契约测试。"""
import asyncio
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import scheduler_registry as registry
from app.db.database import Base
from app.models.system_config import SystemConfig


class SchedulerConfigTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine, tables=[SystemConfig.__table__])
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def test_defaults_and_persistence(self):
        configs, updated_at = registry.read_configs(self.db)
        self.assertEqual(updated_at, "")
        self.assertEqual(len(configs), 9)
        self.assertTrue(configs["certificate_auto_renew"]["enabled"])

        configs["certificate_auto_renew"] = {"enabled": False, "interval_seconds": 86400}
        registry.persist_configs(self.db, configs)
        loaded, loaded_at = registry.read_configs(self.db)
        self.assertFalse(loaded["certificate_auto_renew"]["enabled"])
        self.assertEqual(loaded["certificate_auto_renew"]["interval_seconds"], 86400)
        self.assertTrue(loaded_at)

    def test_validation_rejects_unknown_and_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "未知定时任务"):
            registry.validate_configs({"unknown": {"enabled": True, "interval_seconds": 60}})
        with self.assertRaisesRegex(ValueError, "周期必须"):
            registry.validate_configs({"alert_engine": {"enabled": True, "interval_seconds": 1}})
        with self.assertRaisesRegex(ValueError, "必须是布尔值"):
            registry.validate_configs({"alert_engine": {"enabled": 1, "interval_seconds": 30}})


class SchedulerManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = registry.SchedulerManager()
        self.calls = 0

        async def cycle():
            self.calls += 1

        self.manager._cycles = {item.key: cycle for item in registry.TASK_DEFINITIONS}

    async def asyncTearDown(self):
        await self.manager.shutdown()

    async def test_enable_disable_and_interval_change_keep_single_handle(self):
        configs = registry.default_configs()
        for key in configs:
            configs[key]["enabled"] = False
        configs["alert_engine"] = {"enabled": True, "interval_seconds": 10}
        await self.manager.reconcile(configs)
        first = self.manager._handles["alert_engine"]
        await self.manager.reconcile(configs)
        self.assertIs(first, self.manager._handles["alert_engine"])

        configs["alert_engine"]["interval_seconds"] = 20
        await self.manager.reconcile(configs)
        self.assertIs(first, self.manager._handles["alert_engine"])
        self.assertEqual(self.manager._configs["alert_engine"]["interval_seconds"], 20)

        configs["alert_engine"]["enabled"] = False
        await self.manager.reconcile(configs)
        await asyncio.sleep(0)
        self.assertNotIn("alert_engine", self.manager._handles)
        self.assertTrue(first.cancelled())


if __name__ == "__main__":
    unittest.main()
