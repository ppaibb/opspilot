"""WireGuard 组网核心服务的契约测试。

全部为纯函数，不需要真实集群/服务器：
1. 密钥：X25519 生成、格式校验、加密往返；
2. IP 分配：跳过 .1（出口保留）、跳过已占用、网段耗尽返回 None；
3. 配置渲染：服务端/成员段完整性、禁用成员被排除、全隧道与分流 AllowedIPs；
4. 部署脚本：配置内嵌、heredoc 封闭、systemctl 启用。
"""
import unittest
from types import SimpleNamespace

from app.core.crypto import decrypt_secret, encrypt_secret
from app.services.wireguard_status import parse_wg_dump
from app.services.wireguard import (
    allocate_peer_ip,
    first_host_ip,
    generate_keypair,
    is_valid_private_key,
    network_hosts,
    render_peer_config,
    render_peer_install_script,
    render_server_config,
    render_server_install_script,
    seal_key,
)


def _network(**overrides):
    base = {
        "id": 1,
        "name": "生产组网",
        "cidr": "10.8.0.0/24",
        "endpoint": "wg.example.com",
        "port": 51820,
        "server_public_key": "SERVER_PUB_KEY=",
        "forward_iface": "eth0",
        "full_tunnel": 0,
        "dns": "1.1.1.1",
        "server_private_key": seal_key("SERVER_PRIV_KEY_AAAA="),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _peer(**overrides):
    base = {
        "id": 2,
        "network_id": 1,
        "name": "上海节点-A",
        "allowed_ip": "10.8.0.2/24",
        "public_key": "PEER_PUB_KEY=",
        "private_key": seal_key("PEER_PRIV_KEY_AAAA="),
        "enabled": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class WireGuardKeyContract(unittest.TestCase):
    def test_keypair_format_and_uniqueness(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        for key in (priv1, pub1, priv2, pub2):
            self.assertEqual(len(key), 44)
            self.assertTrue(key.endswith("="))
        self.assertNotEqual(priv1, priv2)

    def test_key_validation(self):
        priv, _ = generate_keypair()
        self.assertTrue(is_valid_private_key(priv))
        self.assertFalse(is_valid_private_key("short"))
        self.assertFalse(is_valid_private_key("not-base64!!!"))

    def test_seal_roundtrip(self):
        priv, _ = generate_keypair()
        sealed = seal_key(priv)
        self.assertNotEqual(sealed, priv)  # 落库必须是密文
        self.assertEqual(decrypt_secret(sealed), priv)


class WireGuardIPAllocationContract(unittest.TestCase):
    def test_first_host_reserved_for_server(self):
        hosts = network_hosts("10.8.0.0/24")
        self.assertEqual(first_host_ip("10.8.0.0/24"), "10.8.0.1/24")
        self.assertGreater(len(hosts), 250)

    def test_allocation_skips_server_and_taken(self):
        taken = ["10.8.0.1/24", "10.8.0.2/24", "10.8.0.3/24"]
        self.assertEqual(allocate_peer_ip("10.8.0.0/24", taken), "10.8.0.4/24")

    def test_allocation_exhaustion(self):
        # /30 只有 2 个可用位：.1 给出口、.2 给唯一成员，之后耗尽。
        self.assertEqual(allocate_peer_ip("10.8.0.0/30", ["10.8.0.1/30", "10.8.0.2/30"]), None)


class WireGuardConfigRenderContract(unittest.TestCase):
    def test_server_config_contains_enabled_peers_only(self):
        peers = [
            _peer(name="启用成员", public_key="ENABLED_PUB=", enabled=1),
            _peer(name="禁用成员", public_key="DISABLED_PUB=", enabled=0, allowed_ip="10.8.0.3/24"),
        ]
        config = render_server_config(_network(), peers)
        self.assertIn("[Interface]", config)
        self.assertIn("PrivateKey = SERVER_PRIV", decrypt_check(config))
        self.assertIn("PublicKey = ENABLED_PUB=", config)
        self.assertNotIn("DISABLED_PUB=", config)  # 禁用成员被排除
        self.assertIn("ListenPort = 51820", config)
        self.assertIn("MASQUERADE", config)

    def test_peer_config_split_tunnel(self):
        config = render_peer_config(_network(full_tunnel=0), _peer())
        self.assertIn("Address = 10.8.0.2/24", config)
        self.assertIn("AllowedIPs = 10.8.0.0/24", config)  # 分流：只路由网段
        self.assertIn("Endpoint = wg.example.com:51820", config)
        self.assertIn("PersistentKeepalive = 25", config)
        self.assertIn("DNS = 1.1.1.1", config)

    def test_peer_config_full_tunnel(self):
        config = render_peer_config(_network(full_tunnel=1), _peer())
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", config)

    def test_private_keys_never_plaintext_in_source_fields(self):
        """落库侧约定：私钥必须以密文存储（渲染时才解密）。"""
        peer = _peer()
        self.assertNotIn("PEER_PRIV", peer.private_key)
        self.assertIn("PEER_PRIV", decrypt_secret(peer.private_key))


def decrypt_check(config: str) -> str:
    """测试辅助：配置内的私钥是解密后的明文，直接返回原文供断言。"""
    return config


class WireGuardDeployScriptContract(unittest.TestCase):
    def test_server_script_embeds_config(self):
        peers = [_peer()]
        config = render_server_config(_network(), peers)
        script = render_server_install_script(_network(), config)
        self.assertIn("apt-get install -y -qq wireguard", script)
        self.assertIn("<<'WGCONF_EOF'", script)
        self.assertIn("WGCONF_EOF", script.split("cat >")[1])  # heredoc 有闭合
        self.assertIn("systemctl enable --now wg-quick@wg0", script)

    def test_peer_script_mentions_server_ip(self):
        network = _network()
        config = render_peer_config(network, _peer())
        script = render_peer_install_script(network, _peer(), config)
        self.assertIn("ping 10.8.0.1", script)


if __name__ == "__main__":
    unittest.main()


class WireGuardStatusParseContract(unittest.TestCase):
    """wg show dump 解析与在线判定的契约测试（纯解析，不依赖 SSH）。"""

    def test_parse_dump_basic(self):
        dump = (
            "SERVER_PRIV\tSERVER_PUB\t51820\t10.8.0.1/24\t0\t0\t0\toff\n"
            "PEER1_PUB\t(none)\t203.0.113.5:40001\t10.8.0.2/32\t1700000000\t1024\t2048\t25\n"
            "PEER2_PUB\t(none)\t198.51.100.7:51000\t10.8.0.3/32\t0\t0\t0\t25\n"
        )
        peers = parse_wg_dump(dump)
        self.assertEqual(len(peers), 2)  # 接口行被跳过
        self.assertEqual(peers["PEER1_PUB"]["rx_bytes"], 1024)
        self.assertEqual(peers["PEER1_PUB"]["tx_bytes"], 2048)
        self.assertEqual(peers["PEER1_PUB"]["latest_handshake"], 1700000000)
        self.assertEqual(peers["PEER2_PUB"]["endpoint"], "198.51.100.7:51000")

    def test_parse_dump_garbage_tolerant(self):
        # 短行/空输出不抛异常。
        self.assertEqual(parse_wg_dump(""), {})
        self.assertEqual(parse_wg_dump("only-interface-line"), {})
        self.assertEqual(parse_wg_dump("a\tb"), {})

    def test_handshake_format(self):
        import time as _time
        from app.services.wireguard_status import _format_handshake
        now = int(_time.time())
        self.assertEqual(_format_handshake(0, now), "从未握手")
        self.assertIn("秒前", _format_handshake(now - 30, now))
        self.assertIn("分钟前", _format_handshake(now - 120, now))
        self.assertIn("小时前", _format_handshake(now - 7200, now))
