"""虚机磁盘明细契约测试。"""
from __future__ import annotations

import json
import unittest

from app.api.vms import parse_pve_config_disks
from app.models.vm import VirtualMachine
from app.api.vms import vm_to_dict


class VmDiskDetailsContractTest(unittest.TestCase):
    """验证 PVE 多磁盘配置能被安全地同步到接口响应。"""

    def test_parse_pve_config_disks_keeps_multiple_data_disks(self) -> None:
        """应解析真实磁盘并忽略 CD-ROM，同时不暴露存储卷路径。"""
        disks = parse_pve_config_disks(
            {
                "scsi0": "local-lvm:vm-309-disk-0,size=100G",
                "scsi1": "data-store:vm-309-disk-1,size=200G",
                "ide2": "local:iso/ubuntu.iso,media=cdrom",
            }
        )

        self.assertEqual(
            [
                {"key": "scsi0", "label": "系统盘", "size_gb": 100.0},
                {"key": "scsi1", "label": "数据盘 1", "size_gb": 200.0},
            ],
            disks,
        )

    def test_vm_to_dict_returns_disk_details(self) -> None:
        """接口响应必须包含 disks 字段，旧 system_disk_gb 继续兼容。"""
        vm = VirtualMachine(
            vm_id="pve-309",
            name="testserver",
            provider="proxmox",
            node="pve03",
            status="running",
            ip="",
            os="",
            cpu_cores=8,
            memory_gb=32,
            system_disk_gb=100,
            disks_json=json.dumps(
                [
                    {"key": "scsi0", "label": "系统盘", "size_gb": 100.0},
                    {"key": "scsi1", "label": "数据盘 1", "size_gb": 200.0},
                ],
                ensure_ascii=False,
            ),
        )

        payload = vm_to_dict(vm)

        self.assertEqual(100.0, payload["system_disk_gb"])
        self.assertEqual(2, len(payload["disks"]))
        self.assertEqual(200.0, payload["disks"][1]["size_gb"])


if __name__ == "__main__":
    unittest.main()
