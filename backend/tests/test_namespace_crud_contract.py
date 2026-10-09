"""命名空间保护名单与标签合并语义的契约测试。

删除路由本身需要真实集群连接，这里只测纯函数：
1. `_check_namespace_deletion_allowed` 的保护名单拦截与放行；
2. 标签合并语义的参考实现（kubernetes.io 前缀跳过、None 删除）——
   与 patch_namespace_labels 路由内联逻辑保持一致的约定验证。
"""
import unittest

from app.api.clusters import _check_namespace_deletion_allowed, PROTECTED_NAMESPACES


class NamespaceProtectionContract(unittest.TestCase):
    def test_protected_namespaces_blocked(self):
        for name in ("kube-system", "kube-public", "kube-node-lease", "default"):
            reason = _check_namespace_deletion_allowed(name)
            self.assertIsNotNone(reason, f"{name} 应被保护名单拦截")
            self.assertIn(name, reason)

    def test_regular_namespace_allowed(self):
        self.assertIsNone(_check_namespace_deletion_allowed("demo-team"))
        self.assertIsNone(_check_namespace_deletion_allowed("prod-payment"))

    def test_protection_list_membership(self):
        # 保护名单恰好是四个核心命名空间，防止后续误增误删。
        self.assertEqual(
            PROTECTED_NAMESPACES,
            {"kube-system", "kube-public", "kube-node-lease", "default"},
        )

    def test_label_merge_semantics_reference(self):
        """路由内联合并逻辑的参考实现：新增/覆盖/删除/系统前缀跳过。"""
        existing = {"app": "demo", "kubernetes.io/metadata.name": "demo-team", "old": "x"}
        patch = {"app": None, "team": "backend", "kubernetes.io/managed-by": "ops", "old": None}
        for key, value in patch.items():
            if key.startswith(("kubernetes.io/", "k8s.io/")):
                continue
            if value is None:
                existing.pop(key, None)
            else:
                existing[key] = value
        # app 被删除；team 新增；old 删除；kubernetes.io 前缀全部跳过。
        self.assertNotIn("app", existing)
        self.assertNotIn("old", existing)
        self.assertEqual(existing["team"], "backend")
        self.assertNotIn("kubernetes.io/managed-by", existing)
        self.assertEqual(existing["kubernetes.io/metadata.name"], "demo-team")


if __name__ == "__main__":
    unittest.main()
