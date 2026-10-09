"""metrics-server 数量解析契约测试。

验证 `_parse_cpu_quantity` / `_parse_memory_quantity` 的边界行为：
- 支持 metrics-server 常见单位（n/u/m/Ki/Mi/Gi/...）；
- 脏数据返回 0，不抛异常；
- 容器数组能正确聚合成 Pod 级别。
"""
import unittest

from app.api.clusters import (
    _parse_cpu_quantity,
    _parse_memory_quantity,
    _sum_container_metrics,
)


class CpuQuantityTest(unittest.TestCase):
    def test_nanocores(self):
        # 1_500_000_000n = 1500m
        self.assertEqual(_parse_cpu_quantity("1500000000n"), 1500)

    def test_microcores(self):
        # 2500u = 2.5m
        self.assertEqual(_parse_cpu_quantity("2500u"), 2)

    def test_millicores(self):
        self.assertEqual(_parse_cpu_quantity("250m"), 250)
        self.assertEqual(_parse_cpu_quantity("1500m"), 1500)

    def test_plain_cores(self):
        self.assertEqual(_parse_cpu_quantity("2"), 2000)
        self.assertEqual(_parse_cpu_quantity("0.5"), 500)

    def test_empty_or_invalid(self):
        self.assertEqual(_parse_cpu_quantity(""), 0)
        self.assertEqual(_parse_cpu_quantity(None), 0)
        self.assertEqual(_parse_cpu_quantity("garbage"), 0)


class MemoryQuantityTest(unittest.TestCase):
    def test_binary_suffix_ki(self):
        self.assertEqual(_parse_memory_quantity("1024Ki"), 1024 * 1024)

    def test_binary_suffix_mi(self):
        self.assertEqual(_parse_memory_quantity("256Mi"), 256 * 1024 * 1024)

    def test_binary_suffix_gi(self):
        self.assertEqual(_parse_memory_quantity("2Gi"), 2 * 1024 ** 3)

    def test_decimal_suffix(self):
        self.assertEqual(_parse_memory_quantity("500M"), 500_000_000)

    def test_plain_bytes(self):
        self.assertEqual(_parse_memory_quantity("123456"), 123456)

    def test_empty_or_invalid(self):
        self.assertEqual(_parse_memory_quantity(""), 0)
        self.assertEqual(_parse_memory_quantity(None), 0)
        self.assertEqual(_parse_memory_quantity("garbage"), 0)


class ContainerMetricsAggregateTest(unittest.TestCase):
    def test_sums_across_containers(self):
        containers = [
            {"usage": {"cpu": "100m", "memory": "128Mi"}},
            {"usage": {"cpu": "250m", "memory": "64Mi"}},
        ]
        cpu, memory = _sum_container_metrics(containers)
        self.assertEqual(cpu, 350)
        self.assertEqual(memory, (128 + 64) * 1024 * 1024)

    def test_empty_returns_zero(self):
        self.assertEqual(_sum_container_metrics([]), (0, 0))
        self.assertEqual(_sum_container_metrics(None), (0, 0))  # type: ignore[arg-type]

    def test_missing_usage_is_tolerated(self):
        containers = [{"name": "c1"}, {"usage": {}}]
        self.assertEqual(_sum_container_metrics(containers), (0, 0))


if __name__ == "__main__":
    unittest.main()
