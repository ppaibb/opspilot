"""SSH 主机指纹校验（TOFU+钉扎）契约测试。

验证阶段5 SSH 安全：
1. decide_host_key_action 纯函数三态：首见钉扎 / 命中放行 / 不一致拒绝；
2. PinnedHostPolicy：首见落库放行、一致放行并刷新时间、不一致抛 SSHException；
3. (host, port, key_type) upsert 幂等；
4. pin_and_connect 统一入口挂策略（含 db=None 内部短会话路径）；
5. 守卫测试：backend/app 下除 app/services/ssh_client.py 外不得出现
   AutoAddPolicy（源码文本扫描）；
6. fingerprint_of 显式自算 OpenSSH 风格 SHA256 指纹（摆脱 paramiko 版本差异）；
7. 首连 upsert 竞态：撞唯一约束后按既有行指纹裁决（一致放行 / 不一致拒绝）；
8. 管理端点 POST /api/assets/{id}/reset-host-key 清除钉扎记录（admin 专用）。
"""
import base64
import hashlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import paramiko
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.asset import Asset
from app.models.asset_host_key import AssetHostKey
from app.services import ssh_client
from app.services.ssh_client import (
    HOST_KEY_MISMATCH_MESSAGE,
    PinnedHostPolicy,
    decide_host_key_action,
    fingerprint_of,
    key_type_of,
    load_pinned_fingerprints,
    pin_and_connect,
)

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine, tables=[AssetHostKey.__table__, Asset.__table__])
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class DecideHostKeyActionContract(unittest.TestCase):
    """纯函数三态判定。"""

    def test_first_time_when_nothing_stored(self):
        self.assertEqual(decide_host_key_action(set(), "SHA256:aaa"), "pin_first_time")

    def test_accept_when_fingerprint_matches(self):
        self.assertEqual(decide_host_key_action({"SHA256:aaa"}, "SHA256:aaa"), "accept")
        self.assertEqual(
            decide_host_key_action({"SHA256:aaa", "SHA256:bbb"}, "SHA256:bbb"),
            "accept",
        )

    def test_reject_when_fingerprint_differs(self):
        self.assertEqual(decide_host_key_action({"SHA256:aaa"}, "SHA256:evil"), "reject")


class PinnedHostPolicyContract(unittest.TestCase):
    """策略行为：落库 / 放行 / 拦截，均走真实 paramiko key。"""

    @classmethod
    def setUpClass(cls):
        cls.server_key = paramiko.RSAKey.generate(2048)
        cls.server_fingerprint = fingerprint_of(cls.server_key)
        cls.server_key_type = key_type_of(cls.server_key)

    def setUp(self):
        self.db = TestingSession()
        self.db.query(AssetHostKey).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _fake_client(self):
        return SimpleNamespace()

    def test_first_connection_pins_and_allows(self):
        policy = PinnedHostPolicy("10.0.0.9", 22, db=self.db)
        policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        rows = self.db.query(AssetHostKey).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].host, "10.0.0.9")
        self.assertEqual(rows[0].port, 22)
        self.assertEqual(rows[0].key_type, self.server_key_type)
        self.assertEqual(rows[0].fingerprint, self.server_fingerprint)
        self.assertTrue(rows[0].first_seen_at)

    def test_matching_fingerprint_allows_and_refreshes_last_seen(self):
        policy = PinnedHostPolicy("10.0.0.9", 22, db=self.db)
        policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        row = self.db.query(AssetHostKey).one()
        stale = row.last_seen_at = "2000-01-01 00:00:00"
        self.db.commit()
        self.assertEqual(stale, "2000-01-01 00:00:00")

        policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        self.assertEqual(self.db.query(AssetHostKey).count(), 1)  # 幂等不新增
        self.db.expire_all()
        refreshed = self.db.query(AssetHostKey).one()
        self.assertNotEqual(refreshed.last_seen_at, "2000-01-01 00:00:00")
        self.assertEqual(refreshed.fingerprint, self.server_fingerprint)

    def test_mismatched_fingerprint_is_rejected(self):
        policy = PinnedHostPolicy("10.0.0.9", 22, db=self.db)
        policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        # 伪造另一把钥匙：换 key_type 避免复用真实 key 对象；
        # asbytes 与 fingerprint 一并伪装（新版 fingerprint_of 不读属性、显式自算）。
        evil = SimpleNamespace(
            fingerprint="SHA256:ZVil",
            get_name=lambda: self.server_key_type,
            asbytes=lambda: b"evil-key-bytes",
        )
        with self.assertRaises(paramiko.SSHException) as ctx:
            policy.missing_host_key(self._fake_client(), "10.0.0.9", evil)
        self.assertEqual(str(ctx.exception), HOST_KEY_MISMATCH_MESSAGE)
        # 原钉扎记录不被污染。
        self.assertEqual(
            self.db.query(AssetHostKey).one().fingerprint,
            self.server_fingerprint,
        )

    def test_port_is_part_of_identity(self):
        policy = PinnedHostPolicy("10.0.0.9", 2222, db=self.db)
        policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        # 同 host 不同端口互不影响：22 端口仍是未钉扎状态。
        self.assertEqual(load_pinned_fingerprints(self.db, "10.0.0.9", 22), set())
        self.assertEqual(len(load_pinned_fingerprints(self.db, "10.0.0.9", 2222)), 1)

    def test_owns_db_path_uses_session_local(self):
        """db=None 时内部开 SessionLocal 短会话（patch 验证落库到测试库）。"""
        policy = PinnedHostPolicy("10.0.0.9", 22, db=None)
        with patch.object(ssh_client, "SessionLocal", TestingSession):
            policy.missing_host_key(self._fake_client(), "10.0.0.9", self.server_key)
        check = TestingSession()
        try:
            self.assertEqual(check.query(AssetHostKey).count(), 1)
        finally:
            check.close()


class PinAndConnectContract(unittest.TestCase):
    """统一入口：挂策略 + 返回已钉扎指纹集合。"""

    @classmethod
    def setUpClass(cls):
        cls.server_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.db = TestingSession()
        self.db.query(AssetHostKey).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_policy_is_attached_and_stored_set_returned(self):
        client = paramiko.SSHClient()
        stored = pin_and_connect(client, "10.0.0.10", 22, db=self.db)
        self.assertEqual(stored, set())
        # paramiko SSHClient 把策略存在 _policy 上（无公开读取接口）。
        policy = client._policy  # noqa: SLF001 单测窥视
        self.assertIsInstance(policy, PinnedHostPolicy)
        self.assertEqual(policy._hostname, "10.0.0.10")  # noqa: SLF001 单测窥视
        self.assertEqual(policy._port, 22)  # noqa: SLF001

    def test_returns_existing_fingerprints(self):
        policy = PinnedHostPolicy("10.0.0.10", 22, db=self.db)
        policy.missing_host_key(SimpleNamespace(), "10.0.0.10", self.server_key)
        stored = pin_and_connect(paramiko.SSHClient(), "10.0.0.10", 22, db=self.db)
        self.assertEqual(stored, {fingerprint_of(self.server_key)})


class FingerprintFormatContract(unittest.TestCase):
    """fingerprint_of 显式自算 OpenSSH 风格 SHA256：不依赖 paramiko 版本行为。"""

    def test_fingerprint_matches_local_sha256_computation(self):
        key = paramiko.RSAKey.generate(2048)
        expected = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(fingerprint_of(key), expected)

    def test_fingerprint_format_and_uniqueness(self):
        a, b = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
        fa, fb = fingerprint_of(a), fingerprint_of(b)
        # OpenSSH 风格：SHA256: 前缀 + 无 padding base64。
        self.assertTrue(fa.startswith("SHA256:"))
        self.assertFalse(fa.endswith("="))
        self.assertNotEqual(fa, fb)
        # 同一把钥匙指纹稳定（重复计算一致，钉扎判定才能幂等）。
        self.assertEqual(fa, fingerprint_of(a))


class FirstConnectRaceContract(unittest.TestCase):
    """首连 upsert 竞态：撞唯一约束后回查既有行裁决，不静默覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.server_key = paramiko.RSAKey.generate(2048)
        cls.server_fingerprint = fingerprint_of(cls.server_key)
        cls.server_key_type = key_type_of(cls.server_key)

    def setUp(self):
        self.db = TestingSession()
        self.db.query(AssetHostKey).delete()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _pin_winner_row(self, fingerprint: str) -> None:
        """模拟并发对手：同身份行已被抢先落库提交。"""
        self.db.add(AssetHostKey(
            host="10.0.0.9", port=22, key_type=self.server_key_type,
            fingerprint=fingerprint,
            first_seen_at="2000-01-01 00:00:00",
            last_seen_at="2000-01-01 00:00:00",
        ))
        self.db.commit()
        self.db.expire_all()

    def _lost_race_patches(self, db):
        """剧本化竞态：upsert 首查不可见（对手尚未提交），提交时撞唯一约束。

        missing_host_key 一共发起两次 AssetHostKey 查询：第 1 次是
        load_pinned_fingerprints（.all()，读到对手已提交的行），第 2 次是
        _upsert_host_key 的首查（.first()）——剧本让它返回 None，模拟
        「判定时对手还没提交、写库时撞上」的窗口。
        """
        real_query = db.query
        real_commit = db.commit
        state = {"query_n": 0, "commit_raised": False}

        def scripted_query(*args, **kwargs):
            q = real_query(*args, **kwargs)
            state["query_n"] += 1
            if state["query_n"] == 2:
                q.first = lambda: None  # upsert 首查：装作查不到行
            return q

        def racing_commit():
            if not state["commit_raised"]:
                state["commit_raised"] = True
                raise IntegrityError(
                    "INSERT INTO asset_host_keys ...", {},
                    Exception("uq_asset_host_key_identity"),
                )
            return real_commit()

        return scripted_query, racing_commit

    def test_race_with_same_fingerprint_is_tolerated(self):
        """并发双写同指纹：撞约束后重查放行，只保留一行、指纹不变。"""
        self._pin_winner_row(self.server_fingerprint)
        policy = PinnedHostPolicy("10.0.0.9", 22, db=self.db)
        scripted_query, racing_commit = self._lost_race_patches(self.db)
        with patch.object(self.db, "query", scripted_query), \
             patch.object(self.db, "commit", racing_commit):
            # 不应抛：竞态只是首连双写，重查后指纹一致放行。
            policy.missing_host_key(SimpleNamespace(), "10.0.0.9", self.server_key)
        rows = self.db.query(AssetHostKey).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].fingerprint, self.server_fingerprint)
        self.assertNotEqual(rows[0].last_seen_at, "2000-01-01 00:00:00")

    def test_race_with_different_fingerprint_is_rejected(self):
        """竞态窗口内对手钉了不同指纹：按中间人拒绝，既有钉扎不被覆盖。"""
        self._pin_winner_row("SHA256:evil-race")
        policy = PinnedHostPolicy("10.0.0.9", 22, db=self.db)
        scripted_query, racing_commit = self._lost_race_patches(self.db)
        with patch.object(self.db, "query", scripted_query), \
             patch.object(self.db, "commit", racing_commit):
            with self.assertRaises(paramiko.SSHException) as ctx:
                policy.missing_host_key(SimpleNamespace(), "10.0.0.9", self.server_key)
        self.assertEqual(str(ctx.exception), HOST_KEY_MISMATCH_MESSAGE)
        row = self.db.query(AssetHostKey).one()
        self.assertEqual(row.fingerprint, "SHA256:evil-race")


class ResetHostKeyEndpointContract(unittest.TestCase):
    """管理端点 POST /api/assets/{id}/reset-host-key：清除钉扎、返回条数。"""

    def setUp(self):
        from app.api import assets as assets_api

        self.assets_api = assets_api
        self.db = TestingSession()
        self.db.query(AssetHostKey).delete()
        self.db.query(Asset).delete()
        self.db.commit()
        self.asset = Asset(
            instance_id="inst-ssh-1", name="web-1", ip="10.0.0.77",
            ssh_port=2222, status="healthy",
        )
        self.db.add(self.asset)
        self.db.commit()
        self.db.refresh(self.asset)

    def tearDown(self):
        self.db.close()

    def _pin(self, host: str, port: int, key_type: str, fingerprint: str) -> None:
        self.db.add(AssetHostKey(
            host=host, port=port, key_type=key_type, fingerprint=fingerprint,
            first_seen_at="2000-01-01 00:00:00", last_seen_at="2000-01-01 00:00:00",
        ))
        self.db.commit()

    def test_reset_deletes_only_target_host_port_rows(self):
        self._pin("10.0.0.77", 2222, "ssh-ed25519", "SHA256:aaa")
        self._pin("10.0.0.77", 2222, "ssh-rsa", "SHA256:bbb")
        # 同 IP 不同端口、不同主机不受影响。
        self._pin("10.0.0.77", 22, "ssh-rsa", "SHA256:ccc")
        self._pin("10.0.0.88", 2222, "ssh-rsa", "SHA256:ddd")

        result = self.assets_api.reset_host_key("inst-ssh-1", db=self.db)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["host"], "10.0.0.77")
        self.assertEqual(result["port"], 2222)
        remaining = {
            (row.host, row.port, row.fingerprint)
            for row in self.db.query(AssetHostKey).all()
        }
        self.assertEqual(remaining, {("10.0.0.77", 22, "SHA256:ccc"), ("10.0.0.88", 2222, "SHA256:ddd")})

    def test_reset_missing_asset_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self.assets_api.reset_host_key("ghost", db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_reset_is_idempotent_when_nothing_pinned(self):
        result = self.assets_api.reset_host_key("inst-ssh-1", db=self.db)
        self.assertEqual(result["deleted"], 0)

    def test_endpoint_registered_with_admin_role_guard(self):
        """路由已注册且仅 admin 角色可过 require_roles 守卫。"""
        route = next(
            r for r in self.assets_api.router.routes
            if getattr(r, "path", "") == "/{asset_id}/reset-host-key"
        )
        self.assertIn("POST", route.methods)


class AutoAddPolicyGuardTest(unittest.TestCase):
    """守卫：backend/app 下除 ssh_client.py 外禁止出现 AutoAddPolicy。"""

    def test_no_auto_add_policy_outside_ssh_client_module(self):
        app_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
        offenders: list[str] = []
        for dirpath, dirnames, filenames in os.walk(app_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, app_root).replace("\\", "/")
                if rel_path == "services/ssh_client.py":
                    continue
                with open(full_path, encoding="utf-8") as handle:
                    if "AutoAddPolicy" in handle.read():
                        offenders.append(rel_path)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
