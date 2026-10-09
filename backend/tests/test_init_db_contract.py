from pathlib import Path
import unittest


class InitDbContractTest(unittest.TestCase):
    def test_init_db_does_not_seed_default_password_or_mock_credentials(self):
        """初始化脚本不得创建弱口令管理员或注入演示凭据。"""
        init_db_path = Path("backend/app/db/init_db.py")
        if not init_db_path.exists():
            init_db_path = Path("app/db/init_db.py")
        content = init_db_path.read_text(encoding="utf-8")

        self.assertNotIn("admin123", content)
        self.assertNotIn("mock_creds", content)
        self.assertNotIn("mock_assets", content)
        self.assertIn("ensure_bootstrap_admin", content)


if __name__ == "__main__":
    unittest.main()
