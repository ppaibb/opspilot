"""虚机纳管主机契约测试。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.vms import VMHostManageRequest, manage_vm_host
from app.db.database import Base
from app.models.asset import Asset
from app.models.audit import AuditLog
from app.models.vm import VirtualMachine


class VMHostManagementContractTest(unittest.TestCase):
    """验证虚机页面只发起主机纳管，不承载 SSH 终端能力。"""

    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        self.Session = sessionmaker(bind=engine)

    def test_manage_vm_host_creates_pending_host_without_password_echo(self) -> None:
        """纳管应创建主机资产，响应不回显 SSH 密码，并写入审计。"""
        db = self.Session()
        db.add(
            VirtualMachine(
                vm_id="pve-101",
                name="api-vm",
                provider="proxmox",
                node="pve01",
                status="running",
                ip="10.0.0.11",
                os="Ubuntu",
                cpu_cores=4,
                memory_gb=8,
                system_disk_gb=80,
                description="业务虚机",
            )
        )
        db.commit()

        payload = VMHostManageRequest(ssh_user="ops", ssh_port=2222, ssh_password="secret")
        response = manage_vm_host(
            "pve-101",
            payload,
            db=db,
            current_user=SimpleNamespace(username="admin", name="管理员"),
        )

        self.assertEqual("vmhost-pve-101", response["host"]["id"])
        self.assertEqual("api-vm", response["host"]["name"])
        self.assertEqual("10.0.0.11", response["host"]["ip"])
        self.assertEqual("ops", response["host"]["ssh_user"])
        self.assertEqual("", response["host"]["ssh_password"])
        self.assertEqual("pending", response["host"]["type"])
        self.assertIn("虚机纳管", ",".join(response["host"]["status"]))
        self.assertEqual(1, db.query(AuditLog).count())

    def test_manage_vm_host_rejects_vm_without_ip(self) -> None:
        """没有管理 IP 的虚机不能直接进入主机纳管。"""
        db = self.Session()
        db.add(VirtualMachine(vm_id="pve-102", name="no-ip", provider="proxmox", ip=""))
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            manage_vm_host(
                "pve-102",
                VMHostManageRequest(),
                db=db,
                current_user=SimpleNamespace(username="admin", name="管理员"),
            )

        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("缺少管理 IP", str(ctx.exception.detail))

    def test_manage_vm_host_rejects_existing_host_ip(self) -> None:
        """同 IP 已存在主机时返回冲突，避免重复资产。"""
        db = self.Session()
        db.add(VirtualMachine(vm_id="pve-103", name="dup-vm", provider="proxmox", ip="10.0.0.12"))
        db.add(
            Asset(
                instance_id="ins-existing",
                name="existing-host",
                ip="10.0.0.12",
                status="SSH 登录认证成功,默认分组",
                type="healthy",
                provider="on-premise",
                region="local",
                os="Linux",
            )
        )
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            manage_vm_host(
                "pve-103",
                VMHostManageRequest(),
                db=db,
                current_user=SimpleNamespace(username="admin", name="管理员"),
            )

        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("ins-existing", str(ctx.exception.detail))

    def test_manage_vm_host_requires_existing_credential(self) -> None:
        """传入凭证引用时必须校验凭证存在，不能保存悬空引用。"""
        db = self.Session()
        db.add(VirtualMachine(vm_id="pve-104", name="cred-vm", provider="proxmox", ip="10.0.0.13"))
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            manage_vm_host(
                "pve-104",
                VMHostManageRequest(credential_ref="missing-cred"),
                db=db,
                current_user=SimpleNamespace(username="admin", name="管理员", role="管理员"),
            )

        self.assertEqual(404, ctx.exception.status_code)
        self.assertIn("凭证不存在", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
