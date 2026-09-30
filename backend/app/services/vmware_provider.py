"""VMware vCenter 虚拟机 Provider。

本模块只承载 vCenter 对接细节，API 层不直接依赖 pyVmomi 对象。
这样后续新增 OpenStack、libvirt、Hyper-V 时，只需要新增同形态 provider，
虚机管理主流程仍然保持“同步 -> 落库 -> 生命周期动作”的统一模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import atexit
import ssl
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.vm import VirtualMachine
from app.services.vm_disks import serialize_vm_disks


try:  # pyVmomi 是可选运行依赖，未安装时给出明确提示，避免后端启动失败。
    from pyVim import connect
    from pyVmomi import vim, vmodl
except Exception:  # pragma: no cover - 本地未安装 pyVmomi 时走运行时提示。
    connect = None
    vim = None
    vmodl = None


VMWARE_PROVIDER_KEYS = {"vmware", "vcenter", "vsphere"}


@dataclass
class VMwareConnectionConfig:
    """vCenter 连接配置。

    password 必须来自后端解密结果，不能由前端回显。verify_tls 默认开启，
    自签证书环境可以临时关闭，但生产环境应保持开启。
    """

    base_url: str
    username: str
    password: str
    verify_tls: bool = True
    default_datacenter: str = ""


def normalize_vmware_provider(value: str) -> str:
    """规范化虚拟化平台标识。"""
    provider = (value or "").strip().lower()
    return "vmware" if provider in VMWARE_PROVIDER_KEYS else provider


def vmware_config_ready(cfg: dict[str, Any]) -> bool:
    """判断 vCenter 配置是否足以发起连接。"""
    return bool(
        str(cfg.get("base_url") or "").strip()
        and str(cfg.get("username") or "").strip()
        and str(cfg.get("password") or "").strip()
    )


def _parse_vcenter_endpoint(base_url: str) -> tuple[str, int]:
    """从 URL 或 host:port 中解析 vCenter 主机和端口。"""
    raw = (base_url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="vCenter 地址不能为空")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="vCenter 地址格式不正确")
    return host, int(parsed.port or 443)


def _ensure_pyvmomi() -> None:
    """确保 pyVmomi 可用，并给出可操作的安装提示。"""
    if connect is None or vim is None:
        raise HTTPException(status_code=500, detail="后端未安装 pyVmomi，请先安装依赖后重启服务")


def connect_vcenter(cfg: VMwareConnectionConfig):
    """建立 vCenter 连接。

    返回 pyVmomi ServiceInstance。调用方不接触密码，只接触已经建立的连接对象。
    """
    _ensure_pyvmomi()
    host, port = _parse_vcenter_endpoint(cfg.base_url)
    context = None
    if not cfg.verify_tls:
        context = ssl._create_unverified_context()  # noqa: SLF001 - pyVmomi 需要原生 SSLContext。
    try:
        service_instance = connect.SmartConnect(
            host=host,
            user=cfg.username,
            pwd=cfg.password,
            port=port,
            sslContext=context,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"连接 vCenter 失败: {exc}") from exc
    atexit.register(connect.Disconnect, service_instance)
    return service_instance


def _wait_task(task: Any, timeout_seconds: int = 180) -> None:
    """等待 vCenter 异步任务完成。"""
    import time

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = task.info.state
        if state == vim.TaskInfo.State.success:
            return
        if state == vim.TaskInfo.State.error:
            error = getattr(task.info, "error", None)
            raise HTTPException(status_code=502, detail=f"vCenter 任务失败: {error}")
        time.sleep(1)
    raise HTTPException(status_code=504, detail="vCenter 任务等待超时")


def _iter_vms(service_instance: Any) -> list[Any]:
    """遍历 vCenter 中所有虚机。"""
    content = service_instance.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        return list(view.view or [])
    finally:
        try:
            view.Destroy()
        except Exception:
            pass


def _vm_identity(vm_obj: Any) -> str:
    """优先使用 instanceUuid，回退到 MoID，保证本地 VM ID 稳定。"""
    config = getattr(vm_obj, "config", None)
    value = str(getattr(config, "instanceUuid", "") or getattr(config, "uuid", "") or getattr(vm_obj, "_moId", "")).strip()
    if not value:
        raise ValueError("虚机缺少 vCenter 唯一标识")
    return value


def _map_power_state(raw: str) -> str:
    """映射 vCenter 电源状态到 OpsPilot 通用状态。"""
    value = str(raw or "").lower()
    if value == "poweredon":
        return "running"
    if value == "poweredoff":
        return "stopped"
    if value == "suspended":
        return "pending"
    return "error"


def _disk_payload(vm_obj: Any) -> list[dict[str, Any]]:
    """提取虚拟磁盘容量，只保留展示所需字段。"""
    hardware = getattr(getattr(vm_obj, "config", None), "hardware", None)
    devices = list(getattr(hardware, "device", []) or [])
    result: list[dict[str, Any]] = []
    for index, device in enumerate(devices, start=1):
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        size_gb = round(float(getattr(device, "capacityInKB", 0) or 0) / 1024 / 1024, 1)
        label = str(getattr(getattr(device, "deviceInfo", None), "label", "") or f"Hard disk {index}")
        result.append({"key": f"disk{index}", "label": label, "size_gb": size_gb})
    return result


def _vm_to_payload(vm_obj: Any) -> dict[str, Any]:
    """把 pyVmomi 虚机对象转换为本地 VirtualMachine 字段。"""
    config = getattr(vm_obj, "config", None)
    summary = getattr(vm_obj, "summary", None)
    runtime = getattr(vm_obj, "runtime", None)
    guest = getattr(vm_obj, "guest", None)
    hardware = getattr(config, "hardware", None)
    host = getattr(runtime, "host", None)
    identity = _vm_identity(vm_obj)
    disks = _disk_payload(vm_obj)
    system_disk_gb = float(disks[0]["size_gb"] or 0.0) if disks else 0.0
    return {
        "vm_id": f"vmware-{identity}",
        "name": str(getattr(vm_obj, "name", "") or getattr(config, "name", "") or identity),
        "provider": "vmware",
        "node": str(getattr(host, "name", "") or ""),
        "status": _map_power_state(str(getattr(runtime, "powerState", "") or "")),
        "ip": str(getattr(guest, "ipAddress", "") or ""),
        "os": str(getattr(config, "guestFullName", "") or getattr(summary, "guest", None) or ""),
        "cpu_cores": float(getattr(hardware, "numCPU", 0) or 0),
        "memory_gb": round(float(getattr(hardware, "memoryMB", 0) or 0) / 1024, 1),
        "system_disk_gb": system_disk_gb,
        "disks": disks,
        "description": str(getattr(config, "annotation", "") or ""),
    }


def _find_vm(service_instance: Any, vm_id: str) -> Any:
    """按本地 vmware-{instanceUuid} 标识查找 vCenter 虚机。"""
    target = (vm_id or "").removeprefix("vmware-")
    for vm_obj in _iter_vms(service_instance):
        try:
            if _vm_identity(vm_obj) == target:
                return vm_obj
        except Exception:
            continue
    raise HTTPException(status_code=404, detail="vCenter 中未找到该虚机")


def upsert_vmware_vm(db: Session, payload: dict[str, Any]) -> None:
    """按平台唯一 ID 写入或更新本地虚机记录。"""
    row = db.query(VirtualMachine).filter(VirtualMachine.vm_id == payload["vm_id"]).first()
    if not row:
        row = VirtualMachine(vm_id=payload["vm_id"])
        db.add(row)
    row.name = payload["name"]
    row.provider = payload["provider"]
    row.node = payload["node"]
    row.status = payload["status"]
    row.ip = payload["ip"]
    row.os = payload["os"]
    row.cpu_cores = payload["cpu_cores"]
    row.memory_gb = payload["memory_gb"]
    row.system_disk_gb = payload["system_disk_gb"]
    row.disks_json = serialize_vm_disks(payload["disks"], payload["system_disk_gb"])
    row.description = payload["description"]


def sync_from_vmware(
    db: Session,
    cfg: VMwareConnectionConfig,
    on_progress: Callable[[int, str, str], None] | None = None,
) -> dict[str, Any]:
    """同步 vCenter 虚机数据到本地表。"""
    def report(progress: int, step: str, message: str) -> None:
        if on_progress:
            on_progress(max(0, min(progress, 100)), step, message)

    report(5, "连接 vCenter", "正在建立 vCenter 连接")
    si = connect_vcenter(cfg)
    report(20, "读取虚机", "正在遍历 vCenter 虚机清单")
    vm_items = _iter_vms(si)
    touched_ids: list[str] = []
    for index, vm_obj in enumerate(vm_items, start=1):
        payload = _vm_to_payload(vm_obj)
        upsert_vmware_vm(db, payload)
        touched_ids.append(payload["vm_id"])
        report(20 + int(70 * index / max(len(vm_items), 1)), "处理虚机", f"已解析 {index}/{len(vm_items)} 台")
    db.commit()
    report(100, "同步完成", f"已同步 {len(set(touched_ids))} 台 VMware 虚机")
    node_names: set[str] = set()
    for vm_obj in vm_items:
        host = getattr(getattr(vm_obj, "runtime", None), "host", None)
        host_name = str(getattr(host, "name", "") or "").strip()
        if host_name:
            node_names.add(host_name)
    return {
        "success_count": len(set(touched_ids)),
        "total": len(vm_items),
        "nodes": len(node_names),
        "lxc_seen": 0,
        "source": "vmware-vcenter",
        "hint": "",
        "provider": "vmware",
    }


def power_vmware_vm(cfg: VMwareConnectionConfig, vm_id: str, action: str) -> str:
    """执行 VMware 虚机生命周期动作，并返回本地状态。"""
    si = connect_vcenter(cfg)
    vm_obj = _find_vm(si, vm_id)
    normalized = (action or "").strip().lower()
    if normalized == "start":
        _wait_task(vm_obj.PowerOnVM_Task(), 180)
        return "running"
    if normalized == "stop":
        try:
            vm_obj.ShutdownGuest()
        except Exception:
            _wait_task(vm_obj.PowerOffVM_Task(), 180)
        return "stopped"
    if normalized == "restart":
        try:
            vm_obj.RebootGuest()
        except Exception:
            _wait_task(vm_obj.ResetVM_Task(), 180)
        return "running"
    raise HTTPException(status_code=400, detail="action 仅支持 start/stop/restart")
