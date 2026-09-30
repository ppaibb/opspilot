"""虚机纳管主机资产服务。"""
from __future__ import annotations

from typing import Any

import re
import uuid

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret
from app.models.asset import Asset
from app.models.audit import AuditLog
from app.models.credential import Credential
from app.models.user import User
from app.models.vm import VirtualMachine
from app.schemas.vm import VMHostManageRequest
from app.services.credential_access import ensure_credential_access
from app.services.credential_usage import sync_credential_usage


def serialize_managed_host(asset: Asset) -> dict[str, Any]:
    """序列化纳管结果，明确不向虚机页面回传 SSH 密码。"""
    return {
        "id": asset.instance_id or "",
        "name": asset.name or "",
        "ip": asset.ip or "",
        "status": asset.status.split(",") if asset.status else [],
        "cpu": round(float(asset.cpu or 0.0), 1),
        "uptime": asset.uptime or "",
        "type": asset.type or "pending",
        "provider": asset.provider or "on-premise",
        "region": asset.region or "local",
        "cloud_resource_type": asset.cloud_resource_type or "",
        "source": "vm_managed",
        "remark": asset.remark or "",
        "os": asset.os or "Linux",
        "ssh_port": asset.ssh_port or 22,
        "ssh_user": asset.ssh_user or "root",
        "ssh_password": "",
        "credential_ref": asset.credential_ref or "",
        "cpu_cores": float(asset.cpu_cores or 0.0),
        "memory_gb": float(asset.memory_gb or 0.0),
        "system_disk_gb": float(asset.system_disk_gb or 0.0),
        "config_task_status": asset.config_task_status or "idle",
        "config_task_updated_at": asset.config_task_updated_at or "",
        "config_task_error": asset.config_task_error or "",
        "health_last_checked_at": asset.health_last_checked_at or "",
    }


def build_vm_host_asset_id(vm_id: str) -> str:
    """生成稳定的虚机纳管主机 ID，便于幂等重试和前端定位。"""
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(vm_id or "").strip()).strip("-")
    if not normalized:
        normalized = str(uuid.uuid4())[:8]
    return f"vmhost-{normalized}"[:50]


def validate_vm_host_manage_input(payload: VMHostManageRequest) -> None:
    """校验主机纳管输入，避免无效 SSH 元数据进入资产库。"""
    if payload.ssh_port < 1 or payload.ssh_port > 65535:
        raise HTTPException(status_code=400, detail="SSH 端口需在 1-65535 之间")
    if not payload.ssh_user.strip():
        raise HTTPException(status_code=400, detail="SSH 用户不能为空")


def write_vm_host_manage_audit(
    db: Session,
    current_user: User,
    request: Request | None,
    vm: VirtualMachine,
    asset: Asset,
    result: str = "success",
) -> None:
    """记录虚机到主机纳管审计，只写标识信息，不写凭证明文。"""
    client_ip = "-"
    if request is not None and request.client is not None:
        client_ip = str(request.client.host or "-")
    db.add(
        AuditLog(
            user=str(getattr(current_user, "name", "") or getattr(current_user, "username", "") or "未知用户"),
            action="虚机纳管主机",
            resource=f"/vms/{vm.vm_id}/hosts/{asset.instance_id}"[:255],
            ip=client_ip,
            result=result,
            risk_level="medium",
            risk_reason="虚机纳管会写入主机资产和 SSH 连接元数据",
        )
    )


def manage_vm_host(
    vm_id: str,
    payload: VMHostManageRequest,
    request: Request | None = None,
    *,
    db: Session,
    current_user: User,
) -> dict[str, Any]:
    """将虚机登记为主机资产。

    安全边界：
    1. VM 页面不建立终端连接，只创建待验证主机资产；
    2. 路由层同时要求虚机管理和主机管理权限；
    3. 已存在同 IP 主机时拒绝重复创建，交由主机管理处理合并；
    4. 响应永不回显 SSH 密码。
    """
    validate_vm_host_manage_input(payload)
    target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="虚拟机不存在")

    ip = str(target.ip or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="虚拟机缺少管理 IP，无法纳管为主机")

    host_id = build_vm_host_asset_id(target.vm_id)
    existing_by_id = db.query(Asset).filter(Asset.instance_id == host_id).first()
    if existing_by_id:
        return {"message": "虚机已纳管为主机", "host": serialize_managed_host(existing_by_id)}

    existing_by_ip = db.query(Asset).filter(Asset.ip == ip).first()
    if existing_by_ip:
        raise HTTPException(
            status_code=409,
            detail=f"该 IP 已存在主机资产：{existing_by_ip.instance_id}",
        )

    credential_ref = payload.credential_ref.strip()
    if credential_ref:
        credential = db.query(Credential).filter(Credential.cred_id == credential_ref).first()
        ensure_credential_access(
            credential,
            current_user,
            not_found_message="凭证不存在",
            denied_message="当前账号无权使用该凭证",
        )

    group = payload.group.strip() or "虚拟机纳管"
    status = f"虚机纳管待验证,{group}"
    remark = payload.remark.strip() or f"由虚机 {target.vm_id} 纳管创建"
    asset = Asset(
        instance_id=host_id,
        name=target.name or target.vm_id,
        ip=ip,
        status=status,
        type="pending",
        cpu=0.0,
        uptime="待验证",
        provider=target.provider or "proxmox",
        region=target.node or "local",
        cloud_resource_type="VM",
        remark=remark,
        os=target.os or "Linux",
        ssh_port=int(payload.ssh_port or 22),
        ssh_user=payload.ssh_user.strip() or "root",
        ssh_password=encrypt_secret(payload.ssh_password) if payload.ssh_password else "",
        credential_ref=credential_ref,
        cpu_cores=float(target.cpu_cores or 0.0),
        memory_gb=float(target.memory_gb or 0.0),
        system_disk_gb=float(target.system_disk_gb or 0.0),
        config_task_status="idle",
        config_task_updated_at="",
        config_task_error="",
        health_last_checked_at="",
    )
    db.add(asset)
    write_vm_host_manage_audit(db, current_user, request, target, asset)
    sync_credential_usage(db, {credential_ref})
    db.commit()
    db.refresh(asset)
    return {"message": "已纳管为主机", "host": serialize_managed_host(asset)}
