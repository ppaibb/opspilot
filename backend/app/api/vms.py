"""
虚拟机管理接口（资产管理子模块）

一期目标：
1. 对接 PVE API 同步节点/虚机数据；
2. 支持虚机开关机、重启、删除；
3. 支持基础创建与编辑（名称/CPU/内存/描述）；
4. 保留本地数据兜底（PVE 未配置时仍可手工维护）。
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional
import json
import re
import threading
import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
import requests
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.notifications import create_notification, NotificationCreate
from app.core.crypto import decrypt_secret
from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import SessionLocal, get_db
from app.models.system_config import SystemConfig
from app.models.user import User
from app.models.vm import VirtualMachine
from app.schemas.vm import (
    VMBatchDeleteRequest,
    VMBridgeItem,
    VMCloneRequest,
    VMCreate,
    VMDiskItem,
    VMHostManageRequest,
    VMIsoItem,
    VMNodeItem,
    VMPowerRequest,
    VMResponse,
    VMSelfCheckItem,
    VMStorageItem,
    VMSyncJobResponse,
    VMTemplateItem,
)
from app.services.vm_disks import (
    fallback_vm_disks,
    format_vm_disk_label,
    normalize_vm_disks,
    parse_pve_config_disks,
    parse_pve_disk_size_gb,
    parse_vm_disks_json,
    serialize_vm_disks,
)
from app.services.vm_host_management import (
    build_vm_host_asset_id,
    manage_vm_host,
    serialize_managed_host,
    validate_vm_host_manage_input,
    write_vm_host_manage_audit,
)
from app.services.vmware_provider import (
    VMWARE_PROVIDER_KEYS,
    VMwareConnectionConfig,
    normalize_vmware_provider,
    power_vmware_vm,
    sync_from_vmware,
    vmware_config_ready,
)

router = APIRouter()

PVE_TIMEOUT_SECONDS = 20
PVE_PROVIDER_KEYS = {"proxmox", "pve"}
SYNC_PROVIDER_KEYS = PVE_PROVIDER_KEYS | VMWARE_PROVIDER_KEYS
PVE_ACTION_MAP = {
    "start": "start",
    "stop": "stop",
    "restart": "reboot",
}
PVE_TASK_POLL_INTERVAL_SECONDS = 1.0
PVE_TASK_WAIT_TIMEOUT_SECONDS = 180
SYNC_JOB_TTL_SECONDS = 60 * 60

SYNC_JOB_LOCK = threading.Lock()
SYNC_JOBS: dict[str, dict[str, Any]] = {}


def vm_to_dict(vm: VirtualMachine) -> dict:
    return {
        "id": vm.vm_id,
        "name": vm.name,
        "provider": vm.provider,
        "node": vm.node,
        "status": vm.status,
        "ip": vm.ip,
        "os": vm.os,
        "cpu_cores": float(vm.cpu_cores or 0.0),
        "memory_gb": float(vm.memory_gb or 0.0),
        "system_disk_gb": float(vm.system_disk_gb or 0.0),
        "disks": parse_vm_disks_json(vm.disks_json, float(vm.system_disk_gb or 0.0)),
        "description": vm.description or "",
    }


def validate_vm_input(vm: VMCreate) -> None:
    if not vm.name.strip():
        raise HTTPException(status_code=400, detail="虚拟机名称不能为空")
    if vm.cpu_cores < 0 or vm.memory_gb < 0 or vm.system_disk_gb < 0:
        raise HTTPException(status_code=400, detail="CPU/内存/系统盘大小不能为负数")
    # 状态改为后端主导：前端不再手工录入状态，仅当请求显式携带时做兼容校验。
    if vm.status is not None and vm.status not in {"running", "stopped", "pending", "error"}:
        raise HTTPException(status_code=400, detail="状态值非法，仅支持 running/stopped/pending/error")


def read_pve_config(db: Session) -> dict:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == "pve_config").first()
    if not row or not row.config_value:
        return {}
    try:
        payload = json.loads(row.config_value)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_vmware_config(db: Session) -> dict:
    """读取 vCenter 连接配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == "vmware_config").first()
    if not row or not row.config_value:
        return {}
    try:
        payload = json.loads(row.config_value)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_vmware_connection_config(cfg: dict) -> VMwareConnectionConfig:
    """构造 vCenter 连接配置，密码只在后端解密后使用。"""
    encrypted = str(cfg.get("password") or "").strip()
    password = decrypt_secret(encrypted) if encrypted else ""
    return VMwareConnectionConfig(
        base_url=str(cfg.get("base_url") or "").strip(),
        username=str(cfg.get("username") or "").strip(),
        password=password,
        verify_tls=bool(cfg.get("verify_tls", True)),
        default_datacenter=str(cfg.get("default_datacenter") or "").strip(),
    )


def pve_config_ready(cfg: dict) -> bool:
    base_url = str(cfg.get("base_url") or "").strip()
    token_id = str(cfg.get("token_id") or "").strip()
    token_secret = str(cfg.get("token_secret") or "").strip()
    return bool(base_url and token_id and token_secret)


def get_pve_token_secret(cfg: dict) -> str:
    encrypted = str(cfg.get("token_secret") or "").strip()
    if not encrypted:
        return ""
    return decrypt_secret(encrypted)


def map_pve_status(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value == "running":
        return "running"
    if value in {"stopped", "shutdown"}:
        return "stopped"
    if value in {"paused", "suspended", "stopping"}:
        return "pending"
    return "error"


def parse_pve_disk_gb(item: dict[str, Any]) -> float:
    """
    从 PVE 列表数据中抽取系统盘容量（GB）。
    优先 maxdisk，回退 0。
    """
    raw = item.get("maxdisk")
    try:
        val = float(raw or 0.0)
    except Exception:
        val = 0.0
    if val <= 0:
        return 0.0
    return round(val / 1024 / 1024 / 1024, 1)


def extract_vmid(vm_id: str) -> int:
    """
    支持 pve-100 / pve-node-100 两类样式，提取末尾数字 VMID。
    """
    text = (vm_id or "").strip().lower()
    match = re.search(r"(\d+)$", text)
    if not match:
        raise HTTPException(status_code=400, detail="PVE 虚机ID格式非法")
    return int(match.group(1))


def now_epoch_ms() -> int:
    """返回当前毫秒级时间戳。"""
    return int(time.time() * 1000)


def cleanup_sync_jobs_locked(now_ts: float) -> None:
    """清理过期任务，避免内存态任务表无限增长。"""
    expired_ids: list[str] = []
    for job_id, job in SYNC_JOBS.items():
        finished_at = int(job.get("finished_at") or 0)
        # 仅清理已结束任务，运行中任务由轮询方持有。
        if finished_at <= 0:
            continue
        if now_ts - (finished_at / 1000.0) > SYNC_JOB_TTL_SECONDS:
            expired_ids.append(job_id)
    for job_id in expired_ids:
        SYNC_JOBS.pop(job_id, None)


def create_sync_job() -> dict[str, Any]:
    """创建同步任务记录并返回初始状态。"""
    job_id = f"sync-{str(uuid.uuid4())[:8]}"
    record = {
        "job_id": job_id,
        "status": "queued",
        "current_step": "任务已创建",
        "progress": 0,
        "message": "等待执行",
        "started_at": now_epoch_ms(),
        "finished_at": 0,
        "result": {},
    }
    with SYNC_JOB_LOCK:
        cleanup_sync_jobs_locked(time.time())
        SYNC_JOBS[job_id] = record
    return record.copy()


def update_sync_job(job_id: str, **kwargs) -> None:
    """更新同步任务状态。"""
    with SYNC_JOB_LOCK:
        record = SYNC_JOBS.get(job_id)
        if not record:
            return
        record.update(kwargs)


def finish_sync_job(job_id: str, status: str, message: str, result: dict[str, Any]) -> None:
    """结束同步任务并写入最终结果，并发送站内通知。"""
    update_sync_job(
        job_id,
        status=status,
        progress=100 if status == "completed" else int(result.get("progress") or 0),
        current_step="同步完成" if status == "completed" else "同步失败",
        message=message,
        finished_at=now_epoch_ms(),
        result=result or {},
    )
    with SYNC_JOB_LOCK:
        record = SYNC_JOBS.get(job_id)
        if not record:
            return
        user_id = record.get("user_id")
    if not user_id:
        return
    db = SessionLocal()
    try:
        create_notification(
            db,
            data=NotificationCreate(
                user_id=user_id,
                type="success" if status == "completed" else "error",
                category="vm",
                title="虚拟机同步" + ("完成" if status == "completed" else "失败"),
                content=message,
                link="/assets/vms",
            ),
        )
    except Exception:
        pass
    finally:
        db.close()


def get_sync_job(job_id: str) -> dict[str, Any] | None:
    """读取任务状态。"""
    with SYNC_JOB_LOCK:
        cleanup_sync_jobs_locked(time.time())
        record = SYNC_JOBS.get((job_id or "").strip())
        return record.copy() if record else None


def request_pve(
    cfg: dict,
    method: str,
    api_path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """
    统一封装 PVE API 调用，返回 payload.data。
    """
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    token_id = str(cfg.get("token_id") or "").strip()
    token_secret = get_pve_token_secret(cfg)
    verify_tls = bool(cfg.get("verify_tls", True))
    if not base_url or not token_id or not token_secret:
        raise HTTPException(status_code=400, detail="PVE 配置不完整，请先在系统管理保存连接信息")

    url = f"{base_url}/api2/json{api_path}"
    headers = {
        "Authorization": f"PVEAPIToken={token_id}={token_secret}",
    }

    try:
        if method.upper() == "GET":
            resp = requests.request(
                method.upper(),
                url,
                headers=headers,
                params=params or {},
                verify=verify_tls,
                timeout=PVE_TIMEOUT_SECONDS,
            )
        else:
            resp = requests.request(
                method.upper(),
                url,
                headers=headers,
                data=params or {},
                verify=verify_tls,
                timeout=PVE_TIMEOUT_SECONDS,
            )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"PVE 接口调用失败（网络异常）: {str(exc)}")

    try:
        payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"PVE 返回非法响应（HTTP {resp.status_code}）")

    if resp.status_code >= 400:
        detail = ""
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, dict):
                detail = "; ".join([f"{k}:{v}" for k, v in errors.items()])
            detail = detail or str(payload.get("message") or payload.get("error") or "").strip()
        raise HTTPException(
            status_code=400 if resp.status_code < 500 else 502,
            detail=detail or f"PVE 请求失败（HTTP {resp.status_code}）",
        )

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="PVE 返回结构异常")
    return payload.get("data")


def wait_pve_task(
    cfg: dict,
    node: str,
    upid: str,
    timeout_seconds: int = PVE_TASK_WAIT_TIMEOUT_SECONDS,
) -> None:
    """
    轮询 PVE 异步任务状态，直到任务结束。

    规则：
    - `status=stopped` 且 `exitstatus=OK` 视为成功；
    - 其他 exitstatus 统一视为失败并回传简要原因。
    """
    safe_upid = quote((upid or "").strip(), safe="")
    safe_node = quote((node or "").strip(), safe="")
    if not safe_upid or not safe_node:
        raise HTTPException(status_code=400, detail="任务跟踪参数不完整")

    deadline = time.time() + max(int(timeout_seconds or 0), 5)
    while time.time() <= deadline:
        info = request_pve(cfg, "GET", f"/nodes/{safe_node}/tasks/{safe_upid}/status", None)
        if isinstance(info, dict):
            status = str(info.get("status") or "").strip().lower()
            exit_status = str(info.get("exitstatus") or "").strip()
            if status == "stopped":
                if not exit_status or exit_status.upper() == "OK":
                    return
                raise HTTPException(status_code=400, detail=f"PVE 任务执行失败: {exit_status}")
        time.sleep(PVE_TASK_POLL_INTERVAL_SECONDS)
    raise HTTPException(status_code=504, detail="等待 PVE 任务结果超时")


def wait_pve_task_if_needed(
    cfg: dict,
    node: str,
    task_result: Any,
    timeout_seconds: int = PVE_TASK_WAIT_TIMEOUT_SECONDS,
) -> None:
    """
    当接口返回 UPID 时自动进入任务轮询，兼容不同返回结构。
    """
    if isinstance(task_result, str) and task_result.startswith("UPID:"):
        wait_pve_task(cfg, node, task_result, timeout_seconds)
        return
    if isinstance(task_result, dict):
        upid = str(task_result.get("upid") or task_result.get("taskid") or "").strip()
        if upid.startswith("UPID:"):
            wait_pve_task(cfg, node, upid, timeout_seconds)


def upsert_vm_row(db: Session, data: dict[str, Any]) -> None:
    row = db.query(VirtualMachine).filter(VirtualMachine.vm_id == data["vm_id"]).first()
    if not row:
        row = VirtualMachine(vm_id=data["vm_id"])
        db.add(row)
    row.name = data["name"]
    row.provider = data["provider"]
    row.node = data["node"]
    row.status = data["status"]
    row.ip = data["ip"]
    row.os = data["os"]
    row.cpu_cores = float(data["cpu_cores"] or 0.0)
    row.memory_gb = float(data["memory_gb"] or 0.0)
    row.system_disk_gb = float(data["system_disk_gb"] or 0.0)
    row.disks_json = serialize_vm_disks(data.get("disks"), row.system_disk_gb)
    row.description = data["description"]


def build_local_vm_from_pve_item(node_name: str, item: dict[str, Any]) -> dict[str, Any]:
    vmid = int(item.get("vmid") or 0)
    vm_name = str(item.get("name") or f"vm-{vmid}")
    cpu_cores = float(item.get("cpus") or item.get("maxcpu") or 0.0)
    maxmem = float(item.get("maxmem") or 0.0)
    memory_gb = round(maxmem / 1024 / 1024 / 1024, 1) if maxmem > 0 else 0.0
    system_disk_gb = parse_pve_disk_gb(item)
    return {
        "vm_id": f"pve-{vmid}",
        "name": vm_name,
        "provider": "proxmox",
        "node": node_name,
        "status": map_pve_status(str(item.get("status") or "")),
        "ip": "",
        "os": str(item.get("ostype") or ""),
        "cpu_cores": cpu_cores,
        "memory_gb": memory_gb,
        "system_disk_gb": system_disk_gb,
        "disks": fallback_vm_disks(system_disk_gb),
        "description": "",
    }


def parse_resource_vmid(item: dict[str, Any]) -> int:
    """
    从 `/cluster/resources?type=vm` 的数据中提取 VMID。

    兼容两种来源：
    1. 直接字段 `vmid`;
    2. 组合字段 `id`（示例：`qemu/100`）。
    """
    raw_vmid = item.get("vmid")
    try:
        vmid = int(raw_vmid)
        if vmid > 0:
            return vmid
    except Exception:
        pass

    raw_id = str(item.get("id") or "").strip()
    match = re.search(r"(\d+)$", raw_id)
    if not match:
        return 0
    return int(match.group(1))


def build_local_vm_from_cluster_resource(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    把 `/cluster/resources?type=vm` 结果转换为本地虚机结构。

    说明：
    - 当前“虚机管理”仅同步 QEMU，LXC 计数但不入表，避免功能语义混淆；
    - 若后续新增“容器管理”模块，可直接复用该解析过程。
    """
    vm_type = str(item.get("type") or "").strip().lower()
    if vm_type != "qemu":
        return None

    vmid = parse_resource_vmid(item)
    if vmid <= 0:
        return None

    node_name = str(item.get("node") or "").strip()
    vm_name = str(item.get("name") or f"vm-{vmid}")
    cpu_cores = float(item.get("maxcpu") or item.get("cpus") or 0.0)
    maxmem = float(item.get("maxmem") or 0.0)
    memory_gb = round(maxmem / 1024 / 1024 / 1024, 1) if maxmem > 0 else 0.0

    system_disk_gb = parse_pve_disk_gb(item)
    return {
        "vm_id": f"pve-{vmid}",
        "name": vm_name,
        "provider": "proxmox",
        "node": node_name,
        "status": map_pve_status(str(item.get("status") or "")),
        "ip": "",
        "os": str(item.get("template") or item.get("ostype") or ""),
        "cpu_cores": cpu_cores,
        "memory_gb": memory_gb,
        "system_disk_gb": system_disk_gb,
        "disks": fallback_vm_disks(system_disk_gb),
        "description": "",
    }


def extract_ip_from_agent(data: Any) -> str:
    """提取 QEMU Guest Agent 返回的 IPv4 地址（排除环回地址）。"""
    if not isinstance(data, dict):
        return ""
    result = data.get("result", [])
    if not isinstance(result, list):
        return ""
    for iface in result:
        if not isinstance(iface, dict):
            continue
        # 排除类似 lo 的环回网卡
        if str(iface.get("name") or "").startswith("lo"):
            continue
        for ip_info in iface.get("ip-addresses", []):
            if not isinstance(ip_info, dict):
                continue
            ip_type = str(ip_info.get("ip-address-type", "")).lower()
            ip_val = str(ip_info.get("ip-address", ""))
            if ip_type == "ipv4" and ip_val and not ip_val.startswith("127."):
                return ip_val
    return ""


def enrich_pve_vm_details(cfg: dict, payload: dict[str, Any]) -> dict[str, Any]:
    """读取 PVE VM config 并补齐多磁盘明细，同时尝试获取 IP 地址。

    如果某项信息获取失败，保持原有数据不动，以兼容各种异常情况。
    """
    node = str(payload.get("node") or "").strip()
    if not node:
        return payload
    
    try:
        vmid = extract_vmid(str(payload.get("vm_id") or ""))
    except Exception:
        return payload

    # 1. 补齐磁盘明细
    try:
        config = request_pve(cfg, "GET", f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config", None)
        if isinstance(config, dict):
            disks = parse_pve_config_disks(config)
            if disks:
                payload["disks"] = disks
                payload["system_disk_gb"] = float(disks[0]["size_gb"] or payload.get("system_disk_gb") or 0.0)
    except Exception:
        pass

    # 2. 尝试获取 IP（仅运行中的机器且安装了 qemu-guest-agent 才有效）
    if payload.get("status") == "running":
        try:
            agent_data = request_pve(cfg, "GET", f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/network-get-interfaces", None)
            ip_val = extract_ip_from_agent(agent_data)
            if ip_val:
                payload["ip"] = ip_val
        except Exception:
            pass

    return payload


def sync_from_pve(
    db: Session,
    cfg: dict,
    on_progress: Optional[Callable[[int, str, str], None]] = None,
) -> dict:
    """
    同步 PVE 虚机数据到本地表。

    同步策略：
    1. 优先读取 `/cluster/resources?type=vm`（覆盖集群视角，兼容性更好）；
    2. 若未取到 QEMU，再回退按节点读取 `/nodes/{node}/qemu`；
    3. 返回诊断提示，帮助前端在“0 台”时给出可操作原因。
    """
    def report(progress: int, step: str, message: str) -> None:
        if on_progress:
            on_progress(max(0, min(progress, 100)), step, message)

    report(5, "准备同步", "正在读取节点列表")
    nodes_data = request_pve(cfg, "GET", "/nodes", None)
    if not isinstance(nodes_data, list):
        raise HTTPException(status_code=502, detail="PVE 节点返回异常")

    touched_ids: list[str] = []
    total = 0
    lxc_seen = 0
    hint = ""
    source = "node-qemu"

    # 第一阶段：优先走集群汇总接口。
    report(20, "拉取集群资源", "正在读取 cluster/resources")
    try:
        resources = request_pve(cfg, "GET", "/cluster/resources", {"type": "vm"})
    except HTTPException:
        resources = None

    if isinstance(resources, list):
        source = "cluster-resources"
        for raw in resources:
            if not isinstance(raw, dict):
                continue
            vm_type = str(raw.get("type") or "").strip().lower()
            if vm_type == "lxc":
                lxc_seen += 1
                continue
            payload = build_local_vm_from_cluster_resource(raw)
            if not payload:
                continue
            payload = enrich_pve_vm_details(cfg, payload)
            upsert_vm_row(db, payload)
            touched_ids.append(payload["vm_id"])
            total += 1
            if total % 5 == 0:
                report(40, "处理集群资源", f"已解析 {total} 台 QEMU")
        if total > 0:
            db.commit()
            report(100, "同步完成", f"已同步 {len(set(touched_ids))} 台虚机")
            return {
                "success_count": len(set(touched_ids)),
                "total": total,
                "nodes": len([n for n in nodes_data if isinstance(n, dict)]),
                "lxc_seen": lxc_seen,
                "source": source,
                "hint": "",
            }

    # 第二阶段：回退按节点读取 qemu。
    report(55, "节点回退模式", "cluster/resources 未返回 QEMU，回退到节点接口")
    node_count = len([n for n in nodes_data if isinstance(n, dict)])
    scanned_node = 0
    for node_item in nodes_data:
        if not isinstance(node_item, dict):
            continue
        node = str(node_item.get("node") or "").strip()
        if not node:
            continue
        scanned_node += 1
        report(55 + int(35 * scanned_node / max(node_count, 1)), "扫描节点", f"正在扫描节点 {node}")
        try:
            vm_items = request_pve(cfg, "GET", f"/nodes/{quote(node, safe='')}/qemu", None)
        except HTTPException:
            # 单节点失败不影响其他节点，最终由 hint 汇总提示。
            continue
        if not isinstance(vm_items, list):
            continue
        for raw in vm_items:
            if not isinstance(raw, dict):
                continue
            payload = build_local_vm_from_pve_item(node, raw)
            if not payload:
                continue
            payload = enrich_pve_vm_details(cfg, payload)
            upsert_vm_row(db, payload)
            touched_ids.append(payload["vm_id"])
            total += 1

    if total == 0:
        if lxc_seen > 0:
            hint = f"检测到 {lxc_seen} 台 LXC 容器，当前页面仅同步 QEMU 虚机。"
        else:
            hint = "未获取到 QEMU 虚机，请检查 PVE Token 权限（建议包含 VM.Audit，路径 /）。"
    db.commit()
    report(100, "同步完成", f"同步结束：{len(set(touched_ids))} 台虚机")
    return {
        "success_count": len(set(touched_ids)),
        "total": total,
        "nodes": len([n for n in nodes_data if isinstance(n, dict)]),
        "lxc_seen": lxc_seen,
        "source": source,
        "hint": hint,
    }


@router.get("", response_model=List[VMResponse])
def get_vms(q: str = "", db: Session = Depends(get_db)):
    query = db.query(VirtualMachine)
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                VirtualMachine.vm_id.ilike(f"%{keyword}%"),
                VirtualMachine.name.ilike(f"%{keyword}%"),
                VirtualMachine.ip.ilike(f"%{keyword}%"),
                VirtualMachine.node.ilike(f"%{keyword}%"),
                VirtualMachine.provider.ilike(f"%{keyword}%"),
            )
        )
    rows = query.order_by(VirtualMachine.id.desc()).all()
    return [vm_to_dict(row) for row in rows]


@router.post("/{vm_id}/host-management")
def create_managed_host_from_vm(
    vm_id: str,
    payload: VMHostManageRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _vm_permission=Depends(require_permissions("action.assets.vms.manage")),
    _host_permission=Depends(require_permissions("action.assets.hosts.manage")),
):
    """从虚机生命周期页面发起主机纳管。"""
    return manage_vm_host(
        vm_id,
        payload,
        request=request,
        db=db,
        current_user=current_user,
    )


@router.get("/nodes", response_model=List[VMNodeItem])
def get_vm_nodes(db: Session = Depends(get_db)):
    """
    节点列表优先从 PVE 拉取，失败时回退本地虚机表。
    """
    cfg = read_pve_config(db)
    if pve_config_ready(cfg):
        try:
            data = request_pve(cfg, "GET", "/nodes", None)
            if isinstance(data, list):
                result: list[dict] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    node = str(item.get("node") or "").strip()
                    if not node:
                        continue
                    result.append(
                        {
                            "node": node,
                            "status": str(item.get("status") or "unknown"),
                        }
                    )
                if result:
                    return result
        except HTTPException:
            pass

    # 回退：根据本地虚机数据推断节点。
    nodes = (
        db.query(VirtualMachine.node)
        .filter(VirtualMachine.node != "")
        .distinct()
        .all()
    )
    return [{"node": n[0], "status": "unknown"} for n in nodes if n and n[0]]


def normalize_storage_row(raw: dict[str, Any], node: str) -> dict[str, Any] | None:
    """
    规范化 PVE 存储记录，只保留可用于 QEMU 镜像的存储池。
    """
    storage = str(raw.get("storage") or "").strip()
    if not storage:
        return None
    content = str(raw.get("content") or "").strip()
    content_items = [item.strip().lower() for item in content.split(",") if item.strip()]
    # 仅保留可存放虚机磁盘镜像的存储池，避免 ISO/备份等无关项干扰。
    if content_items and "images" not in content_items:
        return None
    return {
        "storage": storage,
        "node": node,
        "storage_type": str(raw.get("type") or "").strip(),
        "content": content,
        "active": bool(int(raw.get("active") or 0)),
        "enabled": bool(int(raw.get("enabled") or 0)),
        "shared": bool(int(raw.get("shared") or 0)),
    }


def normalize_iso_row(raw: dict[str, Any], node: str, storage: str) -> dict[str, Any] | None:
    """
    规范化 ISO 记录，统一返回前端所需字段。
    """
    volid = str(raw.get("volid") or "").strip()
    content = str(raw.get("content") or "").strip().lower()
    if not volid or content != "iso":
        return None
    file_name = volid.split("/")[-1] if "/" in volid else volid.split(":")[-1]
    try:
        size_mb = round(float(raw.get("size") or 0.0) / 1024 / 1024, 1)
    except Exception:
        size_mb = 0.0
    try:
        ctime = int(raw.get("ctime") or 0)
    except Exception:
        ctime = 0
    return {
        "volid": volid,
        "storage": storage,
        "node": node,
        "file_name": file_name,
        "size_mb": size_mb,
        "ctime": ctime,
    }


def normalize_iso_row_fallback(raw: dict[str, Any], node: str, storage: str) -> dict[str, Any] | None:
    """
    ISO 解析兜底：
    部分 PVE 版本/存储在 content 过滤条件下返回字段不稳定，
    此处通过 volid/format/文件名后缀做二次判定，降低漏扫概率。
    """
    volid = str(raw.get("volid") or "").strip()
    if not volid:
        return None
    content = str(raw.get("content") or "").strip().lower()
    fmt = str(raw.get("format") or "").strip().lower()
    file_name = volid.split("/")[-1] if "/" in volid else volid.split(":")[-1]
    lower_name = file_name.lower()
    is_iso = content == "iso" or fmt == "iso" or lower_name.endswith(".iso")
    if not is_iso:
        return None
    try:
        size_mb = round(float(raw.get("size") or 0.0) / 1024 / 1024, 1)
    except Exception:
        size_mb = 0.0
    try:
        ctime = int(raw.get("ctime") or 0)
    except Exception:
        ctime = 0
    return {
        "volid": volid,
        "storage": storage,
        "node": node,
        "file_name": file_name,
        "size_mb": size_mb,
        "ctime": ctime,
    }


def resolve_target_nodes(cfg: dict, explicit_node: str) -> list[str]:
    """
    解析扫描节点：
    - 指定 node 时仅扫描该节点；
    - 未指定时扫描全部节点，避免“默认节点无 ISO 导致误判为空”。
    """
    node = (explicit_node or "").strip()
    if node:
        return [node]

    result: list[str] = []
    try:
        nodes_data = request_pve(cfg, "GET", "/nodes", None)
        if isinstance(nodes_data, list):
            for item in nodes_data:
                if not isinstance(item, dict):
                    continue
                val = str(item.get("node") or "").strip()
                if val:
                    result.append(val)
    except HTTPException:
        return []
    return result


def normalize_bridge_row(raw: dict[str, Any], node: str) -> dict[str, Any] | None:
    """
    规范化网桥记录，仅返回 bridge 类型接口（如 vmbr0）。
    """
    iface = str(raw.get("iface") or "").strip()
    iface_type = str(raw.get("type") or "").strip().lower()
    if not iface:
        return None
    if iface_type != "bridge" and not iface.lower().startswith("vmbr"):
        return None
    def as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "on", "up"}

    return {
        "bridge": iface,
        "node": node,
        "active": as_bool(raw.get("active", False)),
        "autostart": as_bool(raw.get("autostart", False)),
        "comments": str(raw.get("comments") or "").strip(),
    }


@router.get("/storages", response_model=List[VMStorageItem])
def get_vm_storages(
    node: str = "",
    db: Session = Depends(get_db),
):
    """
    获取指定节点可用于虚机镜像的存储池。

    参数：
    - node: 可选；为空时优先取默认节点，仍为空则自动选第一个可用节点。
    """
    cfg = read_pve_config(db)
    if not pve_config_ready(cfg):
        return []

    target_node = (node or "").strip()
    if not target_node:
        target_node = str(cfg.get("default_node") or "").strip()
    if not target_node:
        try:
            nodes_data = request_pve(cfg, "GET", "/nodes", None)
            if isinstance(nodes_data, list):
                for item in nodes_data:
                    if isinstance(item, dict):
                        val = str(item.get("node") or "").strip()
                        if val:
                            target_node = val
                            break
        except HTTPException:
            return []
    if not target_node:
        return []

    try:
        storage_rows = request_pve(cfg, "GET", f"/nodes/{quote(target_node, safe='')}/storage", None)
    except HTTPException:
        return []

    result: list[dict[str, Any]] = []
    if isinstance(storage_rows, list):
        for raw in storage_rows:
            if not isinstance(raw, dict):
                continue
            parsed = normalize_storage_row(raw, target_node)
            if not parsed:
                continue
            result.append(parsed)
    # 活跃+启用存储优先展示，便于快速选择。
    result.sort(key=lambda x: (not x["active"], not x["enabled"], x["storage"]))
    return result


@router.get("/isos", response_model=List[VMIsoItem])
def get_vm_isos(
    node: str = "",
    db: Session = Depends(get_db),
):
    """
    获取指定节点可用于安装的 ISO 镜像列表。

    说明：
    - 仅返回 `content` 包含 `iso` 的存储池内镜像；
    - 仅用于“全新创建虚机”场景。
    """
    cfg = read_pve_config(db)
    if not pve_config_ready(cfg):
        return []

    # 未指定节点时扫描全部节点，避免“默认节点没有 ISO”导致误判为空。
    target_nodes = resolve_target_nodes(cfg, node)
    if not target_nodes:
        fallback = str(cfg.get("default_node") or "").strip()
        if fallback:
            target_nodes = [fallback]
    if not target_nodes:
        return []

    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for target_node in target_nodes:
        try:
            storage_rows = request_pve(cfg, "GET", f"/nodes/{quote(target_node, safe='')}/storage", None)
        except HTTPException:
            continue
        if not isinstance(storage_rows, list):
            continue
        for row in storage_rows:
            if not isinstance(row, dict):
                continue
            storage = str(row.get("storage") or "").strip()
            if not storage:
                continue
            content = str(row.get("content") or "").strip().lower()
            content_items = [item.strip() for item in content.split(",") if item.strip()]
            # content 声明不包含 iso 的存储直接跳过，减少无效请求。
            if content_items and "iso" not in content_items:
                continue
            iso_rows: list[dict[str, Any]] = []
            try:
                first_rows = request_pve(
                    cfg,
                    "GET",
                    f"/nodes/{quote(target_node, safe='')}/storage/{quote(storage, safe='')}/content",
                    {"content": "iso"},
                )
                if isinstance(first_rows, list):
                    iso_rows = [item for item in first_rows if isinstance(item, dict)]
            except HTTPException:
                iso_rows = []

            # 兜底：部分环境使用 content=iso 过滤会返回空，回退全量列表后本地筛选。
            if len(iso_rows) == 0:
                try:
                    fallback_rows = request_pve(
                        cfg,
                        "GET",
                        f"/nodes/{quote(target_node, safe='')}/storage/{quote(storage, safe='')}/content",
                        None,
                    )
                    if isinstance(fallback_rows, list):
                        iso_rows = [item for item in fallback_rows if isinstance(item, dict)]
                except HTTPException:
                    iso_rows = []

            for raw in iso_rows:
                parsed = normalize_iso_row(raw, target_node, storage) or normalize_iso_row_fallback(raw, target_node, storage)
                if not parsed:
                    continue
                dedup_key = f"{target_node}|{parsed['volid']}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                result.append(parsed)

    result.sort(key=lambda x: (x["node"].lower(), x["file_name"].lower(), x["storage"].lower()))
    return result


@router.get("/bridges", response_model=List[VMBridgeItem])
def get_vm_bridges(
    node: str = "",
    db: Session = Depends(get_db),
):
    """
    获取节点可用网桥列表（用于全新创建虚机时选择 net0 bridge）。
    """
    cfg = read_pve_config(db)
    if not pve_config_ready(cfg):
        return []

    target_node = (node or "").strip() or str(cfg.get("default_node") or "").strip()
    if not target_node:
        nodes = resolve_target_nodes(cfg, "")
        if nodes:
            target_node = nodes[0]
    if not target_node:
        return []

    try:
        rows = request_pve(cfg, "GET", f"/nodes/{quote(target_node, safe='')}/network", None)
    except HTTPException:
        return []
    if not isinstance(rows, list):
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        parsed = normalize_bridge_row(raw, target_node)
        if not parsed:
            continue
        key = parsed["bridge"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(parsed)

    # vmbr0 优先，其次按名称排序。
    result.sort(key=lambda x: (x["bridge"] != "vmbr0", x["bridge"]))
    return result


@router.post("/sync")
def sync_vms(
    provider: str = "proxmox",
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    provider_key = normalize_vmware_provider((provider or "proxmox").strip().lower())
    if provider_key in PVE_PROVIDER_KEYS:
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，请先到系统管理填写连接信息")
        result = sync_from_pve(db, cfg)
    elif provider_key in VMWARE_PROVIDER_KEYS:
        cfg = read_vmware_config(db)
        conn = build_vmware_connection_config(cfg)
        if not vmware_config_ready({"base_url": conn.base_url, "username": conn.username, "password": conn.password}):
            raise HTTPException(status_code=400, detail="vCenter 配置未完成，请先到系统管理填写连接信息")
        result = sync_from_vmware(db, conn)
    else:
        raise HTTPException(status_code=400, detail=f"暂不支持的虚拟化平台: {provider}")
    message = compose_sync_message(result)
    return {
        "message": message,
        **result,
    }


def compose_sync_message(result: dict[str, Any]) -> str:
    """拼装同步结果文案，供同步接口与任务接口复用。"""
    provider = str(result.get("provider") or result.get("source") or "proxmox")
    provider_label = "VMware" if provider == "vmware" else "PVE"
    message = f"{provider_label} 同步完成：{int(result.get('success_count') or 0)} 台虚机，覆盖 {int(result.get('nodes') or 0)} 个节点"
    if int(result.get("lxc_seen") or 0) > 0:
        message += f"（检测到 {result['lxc_seen']} 台 LXC）"
    hint = str(result.get("hint") or "").strip()
    if hint:
        message += f"。{hint}"
    return message


def run_sync_job(job_id: str, provider: str = "proxmox") -> None:
    """后台执行同步任务，持续更新任务进度。"""
    db = SessionLocal()
    try:
        provider_key = normalize_vmware_provider((provider or "proxmox").strip().lower())

        update_sync_job(job_id, status="running", current_step="开始同步", progress=3, message="任务已开始")
        progress_callback = lambda progress, step, message: update_sync_job(
            job_id,
            status="running",
            current_step=step,
            progress=progress,
            message=message,
        )
        if provider_key in PVE_PROVIDER_KEYS:
            cfg = read_pve_config(db)
            if not pve_config_ready(cfg):
                finish_sync_job(job_id, "failed", "PVE 配置未完成，请先到系统管理填写连接信息", {})
                return
            result = sync_from_pve(db, cfg, on_progress=progress_callback)
            result["provider"] = "proxmox"
        elif provider_key in VMWARE_PROVIDER_KEYS:
            cfg = read_vmware_config(db)
            conn = build_vmware_connection_config(cfg)
            if not vmware_config_ready({"base_url": conn.base_url, "username": conn.username, "password": conn.password}):
                finish_sync_job(job_id, "failed", "vCenter 配置未完成，请先到系统管理填写连接信息", {})
                return
            result = sync_from_vmware(db, conn, on_progress=progress_callback)
        else:
            finish_sync_job(job_id, "failed", f"暂不支持的虚拟化平台: {provider}", {})
            return
        message = compose_sync_message(result)
        finish_sync_job(job_id, "completed", message, result)
    except HTTPException as exc:
        finish_sync_job(job_id, "failed", str(exc.detail), {"error": str(exc.detail)})
    except Exception as exc:
        finish_sync_job(job_id, "failed", f"同步失败: {str(exc)}", {"error": str(exc)})
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.post("/sync/jobs", response_model=VMSyncJobResponse)
def start_sync_job(
    background_tasks: BackgroundTasks,
    provider: str = "proxmox",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    provider_key = normalize_vmware_provider((provider or "proxmox").strip().lower())
    if provider_key in PVE_PROVIDER_KEYS:
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，请先到系统管理填写连接信息")
    elif provider_key in VMWARE_PROVIDER_KEYS:
        cfg = read_vmware_config(db)
        conn = build_vmware_connection_config(cfg)
        if not vmware_config_ready({"base_url": conn.base_url, "username": conn.username, "password": conn.password}):
            raise HTTPException(status_code=400, detail="vCenter 配置未完成，请先到系统管理填写连接信息")
    else:
        raise HTTPException(status_code=400, detail=f"暂不支持的虚拟化平台: {provider}")
    record = create_sync_job(current_user.id)
    background_tasks.add_task(run_sync_job, record["job_id"], provider_key)
    return record


@router.get("/sync/jobs/{job_id}", response_model=VMSyncJobResponse)
def get_sync_job_status(
    job_id: str,
):
    record = get_sync_job(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="同步任务不存在或已过期")
    return record


@router.get("/self-check")
def pve_self_check(
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    """
    执行 PVE 连接与权限自检（只读检查，不触发写操作）。
    """
    cfg = read_pve_config(db)
    if not pve_config_ready(cfg):
        raise HTTPException(status_code=400, detail="PVE 配置未完成，请先到系统管理填写连接信息")

    checks: list[dict[str, Any]] = []
    hints: list[str] = []
    qemu_count = 0
    lxc_count = 0
    nodes: list[dict[str, Any]] = []

    def append_check(name: str, ok: bool, message: str) -> None:
        checks.append({"name": name, "ok": ok, "message": message})

    try:
        version = request_pve(cfg, "GET", "/version", None)
        release = str((version or {}).get("release") or (version or {}).get("version") or "")
        append_check("PVE 连接", True, f"连接成功 {release}".strip())
    except HTTPException as exc:
        append_check("PVE 连接", False, str(exc.detail))
        return {"overall_ok": False, "checks": checks, "hints": ["请先确认 PVE 地址、Token 和 TLS 配置。"], "qemu_count": 0, "lxc_count": 0}

    try:
        nodes_data = request_pve(cfg, "GET", "/nodes", None)
        if isinstance(nodes_data, list):
            nodes = [item for item in nodes_data if isinstance(item, dict) and str(item.get("node") or "").strip()]
        append_check("节点列表", len(nodes) > 0, f"检测到 {len(nodes)} 个节点")
    except HTTPException as exc:
        append_check("节点列表", False, str(exc.detail))

    try:
        resources = request_pve(cfg, "GET", "/cluster/resources", {"type": "vm"})
        if isinstance(resources, list):
            qemu_count = len([r for r in resources if isinstance(r, dict) and str(r.get("type") or "").strip().lower() == "qemu"])
            lxc_count = len([r for r in resources if isinstance(r, dict) and str(r.get("type") or "").strip().lower() == "lxc"])
        append_check("虚机资源读取", qemu_count >= 0, f"QEMU {qemu_count} 台，LXC {lxc_count} 台")
    except HTTPException as exc:
        append_check("虚机资源读取", False, str(exc.detail))

    try:
        next_id = request_pve(cfg, "GET", "/cluster/nextid", None)
        append_check("创建前置能力", True, f"nextid 可用（{next_id}）")
    except HTTPException as exc:
        append_check("创建前置能力", False, str(exc.detail))
        hints.append("若需创建虚机，请补充 Datacenter/VM 相关写权限。")

    if qemu_count == 0:
        hints.append("当前未检测到 QEMU 虚机，可能只有 LXC，或 Token 仅有节点读取权限。")

    if not any(item.get("ok") for item in checks if item.get("name") == "虚机资源读取"):
        hints.append("建议在 ACL 的 / 路径补充 VM.Audit 权限后重试。")

    overall_ok = all(bool(item.get("ok")) for item in checks)
    return {
        "overall_ok": overall_ok,
        "checks": checks,
        "hints": hints,
        "qemu_count": qemu_count,
        "lxc_count": lxc_count,
    }


@router.get("/templates", response_model=List[VMTemplateItem])
def get_vm_templates(db: Session = Depends(get_db)):
    """
    获取 PVE QEMU 模板清单（用于快速克隆创建）。
    """
    cfg = read_pve_config(db)
    if not pve_config_ready(cfg):
        return []

    result: list[dict[str, Any]] = []
    try:
        resources = request_pve(cfg, "GET", "/cluster/resources", {"type": "vm"})
        if isinstance(resources, list):
            for raw in resources:
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("type") or "").strip().lower() != "qemu":
                    continue
                if int(raw.get("template") or 0) != 1:
                    continue
                vmid = parse_resource_vmid(raw)
                if vmid <= 0:
                    continue
                result.append(
                    {
                        "vmid": vmid,
                        "name": str(raw.get("name") or f"template-{vmid}"),
                        "node": str(raw.get("node") or ""),
                        "status": str(raw.get("status") or ""),
                    }
                )
    except HTTPException:
        return []

    result.sort(key=lambda x: (x["node"], x["vmid"]))
    return result


@router.post("", response_model=VMResponse)
def create_vm(
    vm: VMCreate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    validate_vm_input(vm)
    provider = (vm.provider or "").strip().lower() or "proxmox"
    node = (vm.node or "").strip()
    vm_id = f"vm-{str(uuid.uuid4())[:8]}"
    final_status = vm.status or "stopped"
    created_disks: list[dict[str, Any]] | None = None

    if provider in PVE_PROVIDER_KEYS:
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，请先在系统管理保存连接信息")
        if not node:
            node = str(cfg.get("default_node") or "").strip()
        if not node:
            raise HTTPException(status_code=400, detail="未指定 PVE 节点，请填写节点或设置默认节点")

        vmid = int(vm.vmid) if vm.vmid and vm.vmid > 0 else 0
        if vmid <= 0:
            nextid = request_pve(cfg, "GET", "/cluster/nextid", None)
            try:
                vmid = int(nextid)
            except Exception:
                raise HTTPException(status_code=502, detail="获取 PVE nextid 失败")

        cores = max(1, int(vm.cpu_cores or 1))
        memory_mb = max(512, int((vm.memory_gb or 1) * 1024))
        disk_gb = max(4, int(vm.system_disk_gb or 20))
        storage = (vm.storage or "local-lvm").strip()
        bridge = (vm.bridge or "vmbr0").strip()
        template_vmid = int(vm.template_vmid or 0)
        target_storage = (vm.target_storage or "").strip()
        boot_iso = (vm.boot_iso or "").strip()
        if not storage:
            storage = "local-lvm"
        if not bridge:
            bridge = "vmbr0"

        if template_vmid > 0:
            # 模板克隆：优先满足快速交付场景，创建后再补基础配置参数。
            clone_payload: dict[str, Any] = {
                "newid": str(vmid),
                "name": vm.name.strip(),
                "target": node,
                "full": "1" if bool(vm.full_clone) else "0",
            }
            if target_storage:
                clone_payload["storage"] = target_storage
            clone_result = request_pve(
                cfg,
                "POST",
                f"/nodes/{quote(node, safe='')}/qemu/{template_vmid}/clone",
                clone_payload,
            )
            wait_pve_task_if_needed(cfg, node, clone_result, 300)

            cfg_result = request_pve(
                cfg,
                "PUT",
                f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config",
                {
                    "cores": str(cores),
                    "memory": str(memory_mb),
                    "description": vm.description.strip(),
                    "agent": "1",
                    "onboot": "1",
                },
            )
            wait_pve_task_if_needed(cfg, node, cfg_result, 120)
        else:
            if not boot_iso:
                raise HTTPException(status_code=400, detail="全新创建虚机时请选择 ISO 镜像")
            create_params = {
                "vmid": str(vmid),
                "name": vm.name.strip(),
                "cores": str(cores),
                "memory": str(memory_mb),
                "sockets": "1",
                "onboot": "1",
                "agent": "1",
                "ostype": "l26",
                "scsihw": "virtio-scsi-pci",
                "scsi0": f"{storage}:size={disk_gb}G",
                "net0": f"virtio,bridge={bridge}",
                "ide2": f"{boot_iso},media=cdrom",
                "boot": "order=ide2;scsi0;net0",
            }
            create_result = request_pve(
                cfg,
                "POST",
                f"/nodes/{quote(node, safe='')}/qemu",
                create_params,
            )
            wait_pve_task_if_needed(cfg, node, create_result, 300)
        # 可选开关：创建成功后立即执行开机动作，满足“创建即上线”场景。
        if bool(vm.start_after_create):
            start_result = request_pve(
                cfg,
                "POST",
                f"/nodes/{quote(node, safe='')}/qemu/{vmid}/status/start",
                {},
            )
            wait_pve_task_if_needed(cfg, node, start_result, 180)

        vm_id = f"pve-{vmid}"
        provider = "proxmox"
        final_status = "running" if bool(vm.start_after_create) else "stopped"
        try:
            config = request_pve(cfg, "GET", f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config", None)
            if isinstance(config, dict):
                created_disks = parse_pve_config_disks(config) or None
        except Exception:
            created_disks = None

    system_disk_gb = (
        float(created_disks[0]["size_gb"] or 0.0)
        if created_disks
        else float(vm.system_disk_gb or 0.0)
    )

    new_vm = VirtualMachine(
        vm_id=vm_id,
        name=vm.name.strip(),
        provider=provider,
        node=node,
        status=final_status,
        ip=vm.ip.strip(),
        os=vm.os.strip(),
        cpu_cores=float(vm.cpu_cores or 0.0),
        memory_gb=float(vm.memory_gb or 0.0),
        system_disk_gb=system_disk_gb,
        disks_json=serialize_vm_disks(created_disks, system_disk_gb),
        description=vm.description.strip(),
    )
    db.add(new_vm)
    db.commit()
    db.refresh(new_vm)
    return vm_to_dict(new_vm)


@router.post("/batch-delete")
def batch_delete_vms(
    payload: VMBatchDeleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
    _risk=Depends(require_risk_ack("action.assets.vms.manage", "delete")),
):
    """批量删除虚机（与单删同键同动作：vms:delete 二次确认）。"""
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "failed": []}

    cfg = read_pve_config(db)
    failed: List[dict] = []
    success_count = 0
    for vm_id in ids:
        target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
        if not target:
            failed.append({"id": vm_id, "reason": "虚拟机不存在"})
            continue
        try:
            provider = (target.provider or "").strip().lower()
            if provider in PVE_PROVIDER_KEYS and target.vm_id.startswith("pve-"):
                if not pve_config_ready(cfg):
                    failed.append({"id": vm_id, "reason": "PVE 配置未完成"})
                    continue
                vmid = extract_vmid(target.vm_id)
                node = (target.node or "").strip() or str(cfg.get("default_node") or "").strip()
                if not node:
                    failed.append({"id": vm_id, "reason": "缺少节点信息"})
                    continue
                delete_result = request_pve(
                    cfg,
                    "DELETE",
                    f"/nodes/{quote(node, safe='')}/qemu/{vmid}",
                    {"purge": "1"},
                )
                wait_pve_task_if_needed(cfg, node, delete_result, 300)
            db.delete(target)
            success_count += 1
            db.commit()
        except HTTPException as exc:
            db.rollback()
            failed.append({"id": vm_id, "reason": exc.detail})
        except Exception as exc:
            db.rollback()
            failed.append({"id": vm_id, "reason": f"删除失败: {str(exc)}"})

    return {"success_count": success_count, "failed": failed}


@router.put("/{vm_id}", response_model=VMResponse)
def update_vm(
    vm_id: str,
    vm: VMCreate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    validate_vm_input(vm)
    target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="虚拟机不存在")

    provider = (target.provider or "").strip().lower()
    synced_disks: list[dict[str, Any]] | None = None
    if provider in PVE_PROVIDER_KEYS and target.vm_id.startswith("pve-"):
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，无法编辑平台虚机")
        vmid = extract_vmid(target.vm_id)
        node = (target.node or "").strip() or str(cfg.get("default_node") or "").strip()
        if not node:
            raise HTTPException(status_code=400, detail="缺少节点信息，无法编辑")
        update_result = request_pve(
            cfg,
            "PUT",
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config",
            {
                "name": vm.name.strip(),
                "cores": str(max(1, int(vm.cpu_cores or 1))),
                "memory": str(max(512, int((vm.memory_gb or 1) * 1024))),
                "description": vm.description.strip(),
            },
        )
        wait_pve_task_if_needed(cfg, node, update_result, 120)
        try:
            config = request_pve(cfg, "GET", f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config", None)
            if isinstance(config, dict):
                synced_disks = parse_pve_config_disks(config) or None
        except Exception:
            synced_disks = None

    target.name = vm.name.strip()
    target.provider = (vm.provider.strip() or target.provider or "proxmox").lower()
    target.node = vm.node.strip() or target.node
    # 前端已移除“手工状态输入”，编辑场景仅在显式传入时才覆盖状态。
    if vm.status is not None:
        target.status = vm.status
    target.ip = vm.ip.strip()
    target.os = vm.os.strip()
    target.cpu_cores = float(vm.cpu_cores or 0.0)
    target.memory_gb = float(vm.memory_gb or 0.0)
    if synced_disks:
        target.system_disk_gb = float(synced_disks[0]["size_gb"] or 0.0)
        target.disks_json = serialize_vm_disks(synced_disks, target.system_disk_gb)
    else:
        target.system_disk_gb = float(vm.system_disk_gb or 0.0)
        target.disks_json = serialize_vm_disks(
            parse_vm_disks_json(target.disks_json, target.system_disk_gb),
            target.system_disk_gb,
        )
    target.description = vm.description.strip()
    db.commit()
    db.refresh(target)
    return vm_to_dict(target)


@router.post("/{vm_id}/power")
def power_vm(
    vm_id: str,
    payload: VMPowerRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.power")),
    _risk=Depends(require_risk_ack("action.assets.vms.power", "poweroff")),
):
    target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="虚拟机不存在")

    action = (payload.action or "").strip().lower()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=400, detail="action 仅支持 start/stop/restart")

    provider = (target.provider or "").strip().lower()
    if provider in PVE_PROVIDER_KEYS and target.vm_id.startswith("pve-"):
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，无法执行电源操作")
        vmid = extract_vmid(target.vm_id)
        node = (target.node or "").strip() or str(cfg.get("default_node") or "").strip()
        if not node:
            raise HTTPException(status_code=400, detail="缺少节点信息，无法执行电源操作")
        pve_action = PVE_ACTION_MAP[action]
        power_result = request_pve(
            cfg,
            "POST",
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/status/{pve_action}",
            {},
        )
        wait_pve_task_if_needed(cfg, node, power_result, 180)
    elif provider in VMWARE_PROVIDER_KEYS and target.vm_id.startswith("vmware-"):
        cfg = read_vmware_config(db)
        conn = build_vmware_connection_config(cfg)
        if not vmware_config_ready({"base_url": conn.base_url, "username": conn.username, "password": conn.password}):
            raise HTTPException(status_code=400, detail="vCenter 配置未完成，无法执行电源操作")
        target.status = power_vmware_vm(conn, target.vm_id, action)

    if action == "start":
        target.status = "running"
        message = "已启动虚拟机"
    elif action == "stop":
        target.status = "stopped"
        message = "已停止虚拟机"
    else:
        target.status = "running"
        message = "已重启虚拟机"

    db.commit()
    return {"message": message, "status": target.status, "id": target.vm_id}


@router.delete("/{vm_id}")
def delete_vm(
    vm_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
    _risk=Depends(require_risk_ack("action.assets.vms.manage", "delete")),
):
    target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="虚拟机不存在")

    provider = (target.provider or "").strip().lower()
    if provider in PVE_PROVIDER_KEYS and target.vm_id.startswith("pve-"):
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，无法删除平台虚机")
        vmid = extract_vmid(target.vm_id)
        node = (target.node or "").strip() or str(cfg.get("default_node") or "").strip()
        if not node:
            raise HTTPException(status_code=400, detail="缺少节点信息，无法删除")
        delete_result = request_pve(
            cfg,
            "DELETE",
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}",
            {"purge": "1"},
        )
        wait_pve_task_if_needed(cfg, node, delete_result, 180)

    db.delete(target)
    db.commit()
    return {"message": "success"}


@router.post("/{vm_id}/clone", response_model=VMResponse)
def clone_vm(
    vm_id: str,
    payload: VMCloneRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.vms.manage")),
):
    """
    虚拟机独立克隆接口。

    参数说明：
    - vm_id: 源虚拟机 ID（例如 `pve-100` 或 `vm-uuid`）
    - payload: 克隆参数模型（包含新名称、是否 Full Clone、目标节点、目标存储池、自动开机等）

    处理流程：
    1. 校验源虚机是否存在与输入名称合法性；
    2. 若为 PVE/VMware 底座，自动请求集群分配下一个可用 VMID；
    3. 调用底座 /nodes/{node}/qemu/{vmid}/clone 发起物理/逻辑克隆任务；
    4. 可选触发开机操作，随后在本地数据库建立 VirtualMachine 资产记录并返回。
    """
    # 1. 校验源虚拟机是否存在
    target = db.query(VirtualMachine).filter(VirtualMachine.vm_id == vm_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="源虚拟机不存在")

    new_name = (payload.name or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="新虚拟机名称不能为空")

    provider = (target.provider or "proxmox").strip().lower()
    node = (payload.target_node or target.node or "").strip()
    new_vm_id = f"vm-{str(uuid.uuid4())[:8]}"
    final_status = "stopped"

    # 2. 针对 PVE 平台发起底层 API 克隆流程
    if provider in PVE_PROVIDER_KEYS and target.vm_id.startswith("pve-"):
        cfg = read_pve_config(db)
        if not pve_config_ready(cfg):
            raise HTTPException(status_code=400, detail="PVE 配置未完成，无法执行克隆")
        source_vmid = extract_vmid(target.vm_id)
        if not node:
            node = str(cfg.get("default_node") or "").strip()
        if not node:
            raise HTTPException(status_code=400, detail="缺少节点信息，无法执行克隆")

        # 向上获取可用 newid
        nextid = request_pve(cfg, "GET", "/cluster/nextid", None)
        try:
            new_vmid = int(nextid)
        except Exception:
            raise HTTPException(status_code=502, detail="获取 PVE nextid 失败")

        clone_payload: dict[str, Any] = {
            "newid": str(new_vmid),
            "name": new_name,
            "target": node,
            "full": "1" if bool(payload.full_clone) else "0",
        }
        if payload.target_storage.strip():
            clone_payload["storage"] = payload.target_storage.strip()

        # 发起 PVE 异步克隆任务并轮询等待
        clone_result = request_pve(
            cfg,
            "POST",
            f"/nodes/{quote(node, safe='')}/qemu/{source_vmid}/clone",
            clone_payload,
        )
        wait_pve_task_if_needed(cfg, node, clone_result, 300)

        # 3. 可选：克隆完成后自动开机
        if bool(payload.start_after_clone):
            start_result = request_pve(
                cfg,
                "POST",
                f"/nodes/{quote(node, safe='')}/qemu/{new_vmid}/status/start",
                {},
            )
            wait_pve_task_if_needed(cfg, node, start_result, 180)
            final_status = "running"

        new_vm_id = f"pve-{new_vmid}"

    # 4. 新虚机落库
    new_vm = VirtualMachine(
        vm_id=new_vm_id,
        name=new_name,
        provider=provider,
        node=node,
        status=final_status,
        ip="",
        os=target.os or "",
        cpu_cores=float(target.cpu_cores or 0.0),
        memory_gb=float(target.memory_gb or 0.0),
        system_disk_gb=float(target.system_disk_gb or 0.0),
        disks_json=target.disks_json,
        description=f"克隆自 {target.name}",
    )
    db.add(new_vm)
    db.commit()
    db.refresh(new_vm)
    return vm_to_dict(new_vm)

