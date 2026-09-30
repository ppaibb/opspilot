"""
资产查询接口
"""
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import asyncio
import threading
import uuid
import paramiko
import io
import ipaddress
import json
import posixpath
import stat
from urllib.parse import quote

from app.db.database import SessionLocal, get_db
from app.models.audit import AuditLog
from app.models.asset import Asset
from app.models.asset_host_key import AssetHostKey
from app.models.credential import Credential
from app.models.resource_group_member import RESOURCE_TYPE_HOST, ResourceGroupMember
from app.models.user import User
from app.api.notifications import create_notification, NotificationCreate
from app.core.config import settings
from app.core.security import get_current_user, require_permissions, require_risk_ack, require_roles
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.rbac import resolve_effective_permissions
from app.services.credential_access import ensure_credential_access
from app.services.credential_usage import sync_credential_usage
from app.services.asset_auth import load_credential_ssh_auth, resolve_asset_ssh_auth
from app.services.ssh_client import HOST_KEY_MISMATCH_MESSAGE, pin_and_connect
from app.services.terminal_session import create_terminal_session
from app.services.vm_disks import (
    serialize_vm_disks as serialize_host_disks,
    parse_vm_disks_json as parse_host_disks_json,
)

import socket

COLLECT_LOCK = threading.Lock()
COLLECTING_ASSET_IDS: set[str] = set()
SFTP_STREAM_CHUNK_SIZE = 1024 * 1024


def ensure_asset_ping_write_permission(asset_id: str, current_user: User) -> None:
    """校验 `/assets/ping` 写库场景的管理权限。

    纯连接探测不携带 asset_id，只需要 endpoint 级别的 connect 权限；
    一旦携带 asset_id，接口会更新资产连接配置和健康状态，必须具备 manage 权限。
    """
    if not str(asset_id or "").strip():
        return
    permissions = resolve_effective_permissions(current_user)
    if not permissions.get("action.assets.hosts.manage", False):
        raise HTTPException(status_code=403, detail="更新主机连接配置需要资产管理权限")


def write_terminal_file_audit(
    db: Session,
    request: Request,
    current_user: User,
    action: str,
    asset_id: str,
    remote_path: str,
    result: str,
) -> None:
    """写入 WebSSH 文件操作审计，不记录文件内容。"""
    client_ip = request.client.host if request.client else "-"
    db.add(
        AuditLog(
            user=str(current_user.name or current_user.username or "未知用户"),
            action=f"WebSSH文件{action}",
            resource=f"/terminal/files/{asset_id}:{remote_path}"[:255],
            ip=str(client_ip or "-"),
            result=result,
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()


def is_access_key_type(cred_type: str) -> bool:
    normalized = (cred_type or "").strip().lower()
    return (
        normalized == "accesskey"
        or "access key" in normalized
        or "ak/sk" in normalized
        or "云商密钥" in normalized
    )


def parse_access_key_content(raw: str) -> tuple[str, str]:
    """兼容历史存量格式: JSON / key=value / YAML-like。"""
    text = (raw or "").strip()
    if not text:
        return "", ""

    # 1) 优先 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            access_key = str(
                parsed.get("access_key")
                or parsed.get("accessKey")
                or parsed.get("AccessKeyId")
                or ""
            ).strip()
            secret_key = str(
                parsed.get("secret_key")
                or parsed.get("secretKey")
                or parsed.get("AccessKeySecret")
                or ""
            ).strip()
            return access_key, secret_key
    except Exception:
        pass

    # 2) key=value / key: value
    kv = {}
    for line in text.splitlines():
        row = line.strip()
        if not row:
            continue
        if "=" in row:
            k, v = row.split("=", 1)
        elif ":" in row:
            k, v = row.split(":", 1)
        else:
            continue
        kv[k.strip().lower()] = v.strip().strip('"').strip("'")

    if kv:
        access_key = (
            kv.get("access_key")
            or kv.get("accesskey")
            or kv.get("accesskeyid")
            or ""
        ).strip()
        secret_key = (
            kv.get("secret_key")
            or kv.get("secretkey")
            or kv.get("accesskeysecret")
            or ""
        ).strip()
        return access_key, secret_key

    return "", ""


def resolve_aliyun_keys(
    db: Session,
    credential_id: str,
    access_key: str,
    secret_key: str,
    current_user: User | None = None,
) -> tuple[str, str, str]:
    """统一解析 AK/SK 来源，优先凭证。"""
    final_access_key = (access_key or "").strip()
    final_secret_key = (secret_key or "").strip()

    if credential_id:
        cred = db.query(Credential).filter(Credential.cred_id == credential_id).first()
        try:
            cred = ensure_credential_access(
                cred,
                current_user,
                not_found_message="所选凭证不存在",
                denied_message="当前账号无权使用所选云账号凭证",
            )
        except HTTPException as exc:
            return "", "", str(exc.detail)
        if not is_access_key_type(cred.type):
            return "", "", "所选凭证类型不是 AccessKey/AKSK"

        raw = get_credential_content(db, credential_id, current_user)
        final_access_key, final_secret_key = parse_access_key_content(raw)
        if not final_access_key or not final_secret_key:
            return "", "", "凭证内容无法解析出 AccessKey/SecretKey，请在凭证管理中按 AK/SK 结构重存"

    if len(final_access_key) < 10 or len(final_secret_key) < 10:
        return "", "", "AccessKey 或 SecretKey 格式验证不通过（过短）"

    return final_access_key, final_secret_key, ""


def fetch_aliyun_region_ids(access_key: str, secret_key: str) -> list[str]:
    """实时拉取阿里云 ECS 可用区域列表。"""
    from alibabacloud_ecs20140526.client import Client as EcsClient
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_ecs20140526 import models as ecs_models

    config = open_api_models.Config(
        access_key_id=access_key,
        access_key_secret=secret_key,
        endpoint="ecs.aliyuncs.com",
    )
    client = EcsClient(config)
    response = client.describe_regions(ecs_models.DescribeRegionsRequest())
    regions = []
    if response.body and response.body.regions and response.body.regions.region:
        for item in response.body.regions.region:
            region_id = getattr(item, "region_id", "")
            if region_id:
                regions.append(region_id)
    return sorted(list(set(regions)))


def fetch_tencent_region_ids(access_key: str, secret_key: str) -> list[str]:
    """实时拉取腾讯云 CVM 可用区域列表。"""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models

    cred = credential.Credential(access_key, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = "cvm.tencentcloudapi.com"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile

    # 腾讯云 DescribeRegions 为全局接口，region 参数可使用任一可用地域。
    client = cvm_client.CvmClient(cred, "ap-guangzhou", client_profile)
    response = client.DescribeRegions(cvm_models.DescribeRegionsRequest())

    regions: list[str] = []
    for item in getattr(response, "RegionSet", []) or []:
        region_id = str(getattr(item, "Region", "") or "").strip()
        if region_id:
            regions.append(region_id)
    return sorted(list(set(regions)))


def fetch_aws_region_ids(access_key: str, secret_key: str) -> list[str]:
    """实时拉取 AWS EC2 可用区域列表。"""
    import boto3

    # AWS 区域查询走 EC2 全局能力，通常通过 us-east-1 即可拿到区域清单。
    ec2 = boto3.client(
        "ec2",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )
    response = ec2.describe_regions(AllRegions=False)
    regions: list[str] = []
    for item in response.get("Regions", []) or []:
        region_id = str(item.get("RegionName") or "").strip()
        if region_id:
            regions.append(region_id)
    return sorted(list(set(regions)))


def patch_existing_cloud_asset(
    asset: Asset,
    region: str,
    os_name: str,
    cpu_cores: float,
    memory_gb: float,
    system_disk_gb: float,
    resource_type: str = "",
    cloud_credential_ref: str = "",
    private_ip: str = "",
) -> bool:
    """
    尽量补齐历史资产缺失配置字段，不覆盖用户已维护的有效值。
    返回 True 表示存在字段被补齐。
    """
    changed = False
    if (asset.cpu_cores or 0) <= 0 and cpu_cores > 0:
        asset.cpu_cores = cpu_cores
        changed = True
    if (asset.memory_gb or 0) <= 0 and memory_gb > 0:
        asset.memory_gb = memory_gb
        changed = True
    if (asset.system_disk_gb or 0) <= 0 and system_disk_gb > 0:
        asset.system_disk_gb = system_disk_gb
        changed = True
    if (not (asset.os or "").strip()) and os_name:
        asset.os = os_name
        changed = True
    if ((not (asset.region or "").strip()) or (asset.region or "").strip().lower() == "local") and region:
        asset.region = region
        changed = True
    if (not str(getattr(asset, "cloud_resource_type", "") or "").strip()) and resource_type:
        asset.cloud_resource_type = resource_type
        changed = True
    if cloud_credential_ref and getattr(asset, "cloud_credential_ref", "") != cloud_credential_ref:
        asset.cloud_credential_ref = cloud_credential_ref
        changed = True
    if (not (asset.private_ip or "").strip()) and private_ip:
        asset.private_ip = private_ip
        changed = True
    return changed


def _pkey_parse_error(pkey_content: str) -> str:
    """私钥解析失败的可读提示：区分「内容不是私钥」与「私钥格式不支持」。"""
    lowered = (pkey_content or "").strip().lower()
    if "-----begin" not in lowered or "private key-----" not in lowered:
        return "绑定的凭证内容不是 SSH 私钥（疑似把密码误存为 SSH Key）——请检查凭证类型与内容"
    return "私钥格式无法解析（支持 OpenSSH/RSA/ECDSA；PuTTY .ppk 或带口令的私钥不支持）——请重新导出 OpenSSH 格式"


def test_ssh(ip: str, port: int, user: str, pwd: str = None, pkey_content: str = None) -> tuple[bool, str]:
    """尝试通过 paramiko 建立连接并执行短命令，确认认证与会话可用。"""
    if not pwd and not pkey_content:
        return False, "未录入或关联有效鉴权凭证"

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, ip, port)
    try:
        if pkey_content:
            pkey = None
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                try:
                    pkey = key_cls.from_private_key(io.StringIO(pkey_content))
                    break
                except Exception:
                    continue
            if pkey:
                client.connect(hostname=ip, port=port, username=user, pkey=pkey, timeout=3.5)
            else:
                return False, _pkey_parse_error(pkey_content)
        elif pwd:
            client.connect(hostname=ip, port=port, username=user, password=pwd, timeout=3.5)
        # 不止测端口连通，额外执行短命令确认登录会话可用。
        stdin, stdout, stderr = client.exec_command("echo OPSPILOT_SSH_OK", timeout=3.5)
        out = (stdout.read() or b"").decode(errors="ignore").strip()
        client.close()
        if "OPSPILOT_SSH_OK" not in out:
            return False, "SSH 会话建立成功，但命令执行校验失败"
        return True, "SSH 登录认证成功"
    except paramiko.AuthenticationException:
        return False, "密码错误或私钥拒绝被访问"
    except (socket.timeout, socket.error):
        return False, "网络超时不可达或22端口阻断"
    except paramiko.SSHException as exc:
        if HOST_KEY_MISMATCH_MESSAGE in str(exc):
            return False, str(exc)  # 指纹钉扎拦截保持原文提示
        return False, "安全协商失败/非标准SSH端"
    except Exception as e:
        print("[SSH PING FAILURE]", e)
        return False, "系统内核级未知拨号异常"


def infer_cloud_resource_type(asset: Asset) -> str:
    """从历史状态文本中推断云产品类型，兼容旧同步数据。"""
    explicit = str(getattr(asset, "cloud_resource_type", "") or "").strip()
    if explicit:
        return explicit
    # 手动录入的主机编号统一以 ins- 开头，绝不参与云厂商兜底推断，
    # 否则手动机若把“云平台”填成 aliyun/tencent/aws 会被误判成云资源（误标为云同步）。
    if str(asset.instance_id or "").strip().lower().startswith("ins-"):
        return ""
    provider = str(asset.provider or "").strip().lower()
    instance_id = str(asset.instance_id or "").strip().lower()
    status_text = ",".join(str(item) for item in [asset.status or "", instance_id]).lower()
    if provider == "aliyun":
        if instance_id.startswith("swas") or "轻量" in status_text or "swas" in status_text:
            return "SWAS"
        return "ECS"
    if provider == "tencent":
        if instance_id.startswith("lhins") or "lighthouse" in status_text or "轻量" in status_text:
            return "Lighthouse"
        return "CVM"
    if provider == "aws":
        if "lightsail" in status_text:
            return "Lightsail"
        return "EC2"
    return ""


def infer_asset_source(asset: Asset) -> str:
    """区分云同步资产与手动录入资产，用于前端控制云元数据是否可编辑。"""
    provider = str(asset.provider or "").strip().lower()
    if provider in {"aliyun", "tencent", "aws"} and not str(asset.instance_id or "").startswith("ins-"):
        return "cloud_sync"
    if infer_cloud_resource_type(asset):
        return "cloud_sync"
    return "manual"


def safe_parse_float(raw: str) -> float:
    try:
        text = (raw or "").strip()
        if not text:
            return 0.0
        return round(float(text), 1)
    except Exception:
        return 0.0


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def decode_asset_password(asset: Asset) -> str:
    return decrypt_secret(asset.ssh_password or "")


def set_asset_password(asset: Asset, raw: str) -> None:
    text = (raw or "").strip()
    asset.ssh_password = encrypt_secret(text) if text else ""


def apply_password_update(asset: Asset, raw: str, *, allow_clear: bool = False) -> None:
    """更新主机 SSH 密码；空值默认保留原密码。

    编辑主机时前端留空密码表示「不修改已保存密码」，不能覆盖为空。
    如需显式清空密码，传 allow_clear=True。
    """
    text = (raw or "").strip()
    if not text:
        if allow_clear:
            asset.ssh_password = ""
        return
    asset.ssh_password = encrypt_secret(text)


def resolve_update_password(typed_password: str, stored_password: str) -> str:
    """编辑/更新时用于 SSH 探测的密码：本次输入优先，否则用已保存密码。"""
    return (typed_password or "").strip() or (stored_password or "").strip()


def delete_asset_record(db: Session, asset: Asset) -> None:
    """删除主机并清理分组成员表，避免孤儿行污染分组统计。"""
    instance_id = asset.instance_id
    previous_credential_ref = asset.credential_ref or ""
    sync_host_group_membership(db, instance_id, None)
    db.delete(asset)
    sync_credential_usage(db, {previous_credential_ref})


def set_collect_task_state(
    asset: Asset,
    status: str,
    error: str = "",
    updated_at: Optional[str] = None,
) -> None:
    asset.config_task_status = status
    asset.config_task_error = (error or "")[:255]
    asset.config_task_updated_at = updated_at or now_ts()


def queue_collect_task(asset: Asset, background_tasks: BackgroundTasks) -> bool:
    if not try_reserve_collect(asset.instance_id):
        return False
    set_collect_task_state(asset, "collecting", "")
    background_tasks.add_task(run_asset_config_collect_job, asset.instance_id, True)
    return True


def try_reserve_collect(asset_id: str) -> bool:
    with COLLECT_LOCK:
        if asset_id in COLLECTING_ASSET_IDS:
            return False
        COLLECTING_ASSET_IDS.add(asset_id)
        return True


def release_collect(asset_id: str) -> None:
    with COLLECT_LOCK:
        COLLECTING_ASSET_IDS.discard(asset_id)


def collect_host_config_over_ssh(
    ip: str,
    port: int,
    user: str,
    pwd: str = "",
    pkey_content: str = "",
) -> tuple[bool, str, float, float, float, str, list[dict]]:
    """通过 SSH 拉取 CPU/内存/磁盘(系统盘+数据盘)/hostname 信息。

    磁盘明细返回形如 [{"key","label","size_gb"}, ...]，第 0 块为系统盘
    （挂载 / 的物理盘），其余为数据盘，供前端「系统盘 +N」展示。
    """
    if not pwd and not pkey_content:
        return False, "未录入或关联有效鉴权凭证", 0.0, 0.0, 0.0, "", []

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, ip, port)

    try:
        if pkey_content:
            pkey = None
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                try:
                    pkey = key_cls.from_private_key(io.StringIO(pkey_content))
                    break
                except Exception:
                    continue
            if not pkey:
                return False, _pkey_parse_error(pkey_content), 0.0, 0.0, 0.0, "", []
            client.connect(hostname=ip, port=port, username=user, pkey=pkey, timeout=4.0, auth_timeout=6.0, banner_timeout=6.0)
        else:
            client.connect(hostname=ip, port=port, username=user, password=pwd, timeout=4.0)

        # 单次 SSH 执行采集全部指标，用分隔符切分，避免多次往返各自 4s 超时叠加。
        collect_cmd = (
            "echo OPSPILOT_SSH_OK; echo '@@CPU@@';"
            " (getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0);"
            " echo '@@MEM@@';"
            " (awk '/MemTotal/ {printf \"%.1f\", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0);"
            " echo '@@DISKS@@';"
            " (lsblk -b -dn -o NAME,TYPE,SIZE 2>/dev/null | awk '$2==\"disk\"{print $1\" \"$3}');"
            " echo '@@ROOT@@';"
            " (lsblk -no PKNAME \"$(findmnt -no SOURCE / 2>/dev/null)\" 2>/dev/null | head -1);"
            " echo '@@DFROOT@@';"
            " (df -B1 / 2>/dev/null | awk 'NR==2 {printf \"%.1f\", $2/1024/1024/1024}');"
            " echo '@@HOST@@';"
            " (hostname 2>/dev/null || echo '')"
        )
        _, stdout, _ = client.exec_command(collect_cmd, timeout=8.0)
        raw = (stdout.read() or b"").decode(errors="ignore")
        if "OPSPILOT_SSH_OK" not in raw:
            return False, "SSH 会话建立成功，但命令执行校验失败", 0.0, 0.0, 0.0, "", []

        def _section(name: str) -> str:
            marker = f"@@{name}@@"
            after = raw.split(marker, 1)
            if len(after) < 2:
                return ""
            rest = after[1]
            for nxt in ("@@CPU@@", "@@MEM@@", "@@DISKS@@", "@@ROOT@@", "@@DFROOT@@", "@@HOST@@"):
                if nxt in rest:
                    rest = rest.split(nxt, 1)[0]
            return rest.strip()

        cpu_cores = safe_parse_float(_section("CPU"))
        memory_gb = safe_parse_float(_section("MEM"))
        disks_raw = _section("DISKS")
        root_disk = _section("ROOT").splitlines()[0].strip() if _section("ROOT") else ""

        # 枚举所有物理盘（NAME + SIZE，字节），并定位承载 / 的根盘作为系统盘。
        disks: list[dict] = []
        parsed: list[tuple[str, float]] = []
        for line in disks_raw.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            name = parts[0].strip()
            size_gb = safe_parse_float(parts[1]) / 1024.0 / 1024.0 / 1024.0
            if size_gb <= 0:
                continue
            parsed.append((name, round(size_gb, 1)))

        # 系统盘排最前：优先匹配根盘名，否则取容量最大的盘。
        if parsed:
            def _is_system(item: tuple[str, float]) -> bool:
                return bool(root_disk) and item[0] == root_disk
            parsed.sort(key=lambda it: (0 if _is_system(it) else 1, -it[1], it[0]))
            for idx, (name, size_gb) in enumerate(parsed):
                disks.append({
                    "key": name[:32],
                    "label": "系统盘" if idx == 0 else f"数据盘 {idx}",
                    "size_gb": size_gb,
                })

        system_disk_gb = disks[0]["size_gb"] if disks else 0.0
        if system_disk_gb <= 0:
            system_disk_gb = safe_parse_float(_section("DFROOT"))

        real_hostname = _section("HOST").splitlines()[0].strip() if _section("HOST") else ""

        if cpu_cores <= 0 and memory_gb <= 0 and system_disk_gb <= 0:
            return True, "SSH 登录成功，但未采集到硬件配置", 0.0, 0.0, 0.0, real_hostname, disks
        return True, "SSH 登录认证成功，配置采集完成", cpu_cores, memory_gb, system_disk_gb, real_hostname, disks
    except paramiko.AuthenticationException:
        return False, "密码错误或私钥拒绝被访问", 0.0, 0.0, 0.0, "", []
    except (socket.timeout, socket.error):
        return False, "网络超时不可达或22端口阻断", 0.0, 0.0, 0.0, "", []
    except paramiko.SSHException:
        return False, "安全协商失败/非标准SSH端", 0.0, 0.0, 0.0, "", []
    except Exception as e:
        print("[SSH CONFIG COLLECT FAILURE]", e)
        return False, "系统内核级未知拨号异常", 0.0, 0.0, 0.0, "", []
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_asset_config_collect_job(asset_id: str, reserved: bool = False):
    """后台任务: 采集主机硬件配置并回写数据库。"""
    if not reserved and not try_reserve_collect(asset_id):
        return
    db = SessionLocal()
    try:
        asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
        if not asset:
            return

        set_collect_task_state(asset, "collecting", "")
        db.commit()

        auth = resolve_asset_ssh_auth(db, asset)
        ok, msg, cpu_cores, memory_gb, system_disk_gb, real_hostname, disks = collect_host_config_over_ssh(
            asset.ip,
            int(asset.ssh_port or 22),
            asset.ssh_user or "root",
            auth.password,
            auth.pkey_content,
        )

        checked_at = now_ts()
        if ok:
            if cpu_cores > 0:
                asset.cpu_cores = cpu_cores
            if memory_gb > 0:
                asset.memory_gb = memory_gb
            if system_disk_gb > 0:
                asset.system_disk_gb = system_disk_gb
            if disks:
                asset.disks_json = serialize_host_disks(disks, system_disk_gb)
            if real_hostname:
                asset.hostname = real_hostname
            asset.type = "healthy"
            asset.status = msg
            set_collect_task_state(asset, "success", "", checked_at)
        else:
            asset.type = "critical"
            asset.status = msg
            set_collect_task_state(asset, "failed", msg, checked_at)
        asset.health_last_checked_at = checked_at

        db.commit()
    except Exception as e:
        db.rollback()
        print("[ASSET CONFIG JOB FAILURE]", e)
    finally:
        db.close()
        release_collect(asset_id)


def run_assets_health_check_cycle():
    """对资产进行周期性 SSH 巡检，只更新在线状态。"""
    # NotificationCreate 与 create_notification 同在 app.api.notifications（勿拆成不存在的 schemas.notifications）
    from app.api.notifications import create_notification, NotificationCreate
    from app.db.models import User
    db = SessionLocal()
    try:
        assets = db.query(Asset).all()
        checked_at = now_ts()
        state_changes = []
        for asset in assets:
            old_type = asset.type
            auth = resolve_asset_ssh_auth(db, asset)
            if not auth.password and not auth.pkey_content:
                asset.type = "pending"
                asset.status = "未录入或关联有效鉴权凭证"
                asset.health_last_checked_at = checked_at
                continue

            ok, msg = test_ssh(
                asset.ip,
                int(asset.ssh_port or 22),
                asset.ssh_user or "root",
                auth.password,
                auth.pkey_content,
            )
            new_type = "healthy" if ok else "critical"
            asset.type = new_type
            asset.status = msg
            asset.health_last_checked_at = checked_at
            
            # 检测状态变更：healthy ↔ critical
            if old_type in {"healthy", "critical"} and new_type != old_type:
                state_changes.append((asset, old_type, new_type, msg))

        db.commit()
        
        # 通知管理员：主机健康状态变更
        if state_changes:
            admins = db.query(User).filter(User.role == "admin").all()
            for asset, old, new, msg in state_changes:
                for admin in admins:
                    try:
                        create_notification(
                            db,
                            data=NotificationCreate(
                                user_id=admin.id,
                                type="warning" if new == "critical" else "info",
                                category="asset",
                                title=f"主机状态变更：{'离线' if new == 'critical' else '恢复在线'}",
                                content=f"{asset.name}（{asset.ip}）{'连接失败' if new == 'critical' else '已恢复在线'}：{msg}",
                                link="/assets/hosts",
                                resource_id=asset.instance_id,
                            ),
                        )
                    except Exception:
                        pass
    except Exception as e:
        db.rollback()
        print("[ASSET HEALTH CHECK FAILURE]", e)
    finally:
        db.close()


async def start_assets_health_checker():
    """后台巡检协程：每 5 分钟执行一次资产健康检查。"""
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "assets_health_check",
        "主机健康巡检",
        "定时探测主机 SSH/Ping/磁盘等健康状态并刷新资产状态标签",
        300,
    )
    print("[OpsPilot] 启动主机健康巡检器（5分钟）...")
    while True:
        with track_run("assets_health_check"):
            await asyncio.to_thread(run_assets_health_check_cycle)
        await asyncio.sleep(300)

router = APIRouter()

class AssetDiskItem(BaseModel):
    """主机磁盘展示项，只含前端需要的安全字段。"""
    key: str
    label: str
    size_gb: float


class AssetResponse(BaseModel):
    id: str
    name: str
    ip: str
    hostname: str = ""
    private_ip: str = ""
    status: List[str]
    cpu: float
    uptime: str
    type: str
    provider: str
    region: str
    cloud_resource_type: str = ""
    source: str = "manual"
    remark: str = ""
    os: str
    ssh_port: int
    ssh_user: str
    ssh_password: str = ""
    credential_ref: str
    cloud_credential_ref: str = ""
    cpu_cores: float = 0.0
    memory_gb: float = 0.0
    system_disk_gb: float = 0.0
    disks: List[AssetDiskItem] = Field(default_factory=list)
    config_task_status: str = "idle"
    config_task_updated_at: str = ""
    config_task_error: str = ""
    health_last_checked_at: str = ""
    group_id: Optional[int] = None

class AssetCreate(BaseModel):
    name: str
    ip: str
    provider: str = "on-premise"
    region: str = "local"
    group_id: Optional[int] = None
    remark: str = ""
    os: str = ""
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str = ""
    credential_ref: str = ""
    cpu_cores: Optional[float] = None
    memory_gb: Optional[float] = None
    system_disk_gb: Optional[float] = None


class BatchDeleteRequest(BaseModel):
    ids: List[str]


class BatchRefreshConfigRequest(BaseModel):
    ids: List[str]


def sync_host_group_membership(db: Session, instance_id: str, group_id: Optional[int]) -> None:
    """同步主机旧 group_id 字段与统一资源分组成员表。"""
    db.query(ResourceGroupMember).filter(
        ResourceGroupMember.resource_type == RESOURCE_TYPE_HOST,
        ResourceGroupMember.resource_id == instance_id,
    ).delete(synchronize_session=False)
    if group_id is not None:
        db.add(ResourceGroupMember(
            group_id=group_id,
            resource_type=RESOURCE_TYPE_HOST,
            resource_id=instance_id,
        ))


def validate_asset_input(asset: AssetCreate) -> None:
    """基础输入校验，避免脏数据进入资产库。"""
    if not asset.name.strip():
        raise HTTPException(status_code=400, detail="主机名称不能为空")
    if not asset.ip.strip():
        raise HTTPException(status_code=400, detail="主机 IP 不能为空")
    try:
        ipaddress.ip_address(asset.ip.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="主机 IP 格式非法")

    if asset.ssh_port < 1 or asset.ssh_port > 65535:
        raise HTTPException(status_code=400, detail="SSH 端口需在 1-65535 之间")
    for value in (asset.cpu_cores, asset.memory_gb, asset.system_disk_gb):
        if value is not None and value < 0:
            raise HTTPException(status_code=400, detail="CPU/内存/系统盘大小不能为负数")


def resolve_ping_auth(
    password: str,
    credential_id: str,
    pkey_content: str,
    credential_auth,
) -> tuple[str, str]:
    """解析 SSH 测试所用认证材料。

    优先级：本次显式输入的密码 > 一次性私钥 > 凭证中心材料。
    避免用户填了密码却被凭证里的 SSH Key 抢走并报“私钥解析损坏”。
    """
    typed_password = str(password or "").strip()
    typed_pkey = str(pkey_content or "").strip()
    if typed_pkey and not str(credential_id or "").strip():
        return "", typed_pkey
    if typed_password:
        return typed_password, ""
    if credential_auth is None:
        return "", ""
    return str(getattr(credential_auth, "password", "") or ""), str(getattr(credential_auth, "pkey_content", "") or "")


def get_credential_content(
    db: Session,
    credential_ref: str,
    current_user: User | None = None,
) -> str:
    if not credential_ref:
        return ""
    cred = db.query(Credential).filter(Credential.cred_id == credential_ref).first()
    if not cred or not cred.content:
        return ""
    if current_user is not None:
        ensure_credential_access(
            cred,
            current_user,
            not_found_message="凭证不存在",
            denied_message="当前账号无权使用该凭证",
        )
    return decrypt_secret(cred.content)


def ensure_asset_credential_access(db: Session, asset: Asset, current_user: User | None) -> None:
    """校验当前用户是否可使用资产绑定的 SSH 凭证。"""
    credential_ref = str(asset.credential_ref or "").strip()
    if not credential_ref or current_user is None:
        return
    cred = db.query(Credential).filter(Credential.cred_id == credential_ref).first()
    ensure_credential_access(
        cred,
        current_user,
        not_found_message="资产绑定的 SSH 凭证不存在",
        denied_message="当前账号无权使用该主机绑定的 SSH 凭证",
    )


def serialize_asset(a: Asset) -> dict:
    # 历史数据可能存在 NULL，接口出口统一归一化，避免 response_model 校验失败。
    return {
        "id": a.instance_id or "",
        "name": a.name or "",
        "ip": a.ip or "",
        "hostname": a.hostname or "",
        "private_ip": a.private_ip or "",
        "status": a.status.split(",") if a.status else [],
        "cpu": round(float(a.cpu or 0.0), 1),
        "uptime": a.uptime or "",
        "type": a.type or "pending",
        "provider": a.provider or "on-premise",
        "region": a.region or "local",
        "cloud_resource_type": infer_cloud_resource_type(a),
        "source": infer_asset_source(a),
        "remark": a.remark or "",
        "os": a.os or "",
        "ssh_port": a.ssh_port or 22,
        "ssh_user": a.ssh_user or "root",
        "ssh_password": "",
        "credential_ref": a.credential_ref or "",
        "cloud_credential_ref": a.cloud_credential_ref or "",
        "cpu_cores": float(a.cpu_cores or 0.0),
        "memory_gb": float(a.memory_gb or 0.0),
        "system_disk_gb": float(a.system_disk_gb or 0.0),
        "disks": parse_host_disks_json(getattr(a, "disks_json", None), a.system_disk_gb or 0.0),
        "config_task_status": a.config_task_status or "idle",
        "config_task_updated_at": a.config_task_updated_at or "",
        "config_task_error": a.config_task_error or "",
        "health_last_checked_at": a.health_last_checked_at or "",
        "group_id": a.group_id,
    }

def reap_stale_collect_tasks(db: Session, assets: list[Asset], timeout_sec: int = 90) -> None:
    """自愈：DB 长期卡在 collecting 时超时判失败。

    超时后无论内存锁是否残留都强制清理，避免锁泄漏导致自愈被永久阻挡、
    新任务又被「已有任务在执行」挡回的死锁。
    """
    changed = False
    for a in assets:
        if a.config_task_status != "collecting":
            continue
        try:
            started = datetime.strptime(a.config_task_updated_at or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            started = None
        stale = started is None or (datetime.now() - started).total_seconds() > timeout_sec
        if not stale:
            continue
        set_collect_task_state(a, "failed", "采集超时或任务中断，请重试")
        release_collect(a.instance_id)
        changed = True
    if changed:
        db.commit()


@router.get("", response_model=List[AssetResponse])
def get_assets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回所有资产列表"""
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    reap_stale_collect_tasks(db, assets)
    return [serialize_asset(a) for a in assets]


@router.get("/detail/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    ensure_asset_credential_access(db, asset, current_user)
    return serialize_asset(asset)

@router.post("", response_model=AssetResponse)
def create_asset(
    asset: AssetCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """新增主机资产并绑定凭证血脉"""
    validate_asset_input(asset)

    # IP 去重：同一 IP 不允许重复纳管
    existing_by_ip = db.query(Asset).filter(Asset.ip == asset.ip.strip()).first()
    if existing_by_ip:
        raise HTTPException(
            status_code=409,
            detail=f"该 IP ({asset.ip.strip()}) 已被主机 \"{existing_by_ip.name}\" 使用，不能重复添加。",
        )

    has_auth = bool(asset.ssh_password.strip() or asset.credential_ref.strip())
    check_time = now_ts()

    if not has_auth:
        # 未绑定凭证：标成待配置，不要误报「SSH 连接失败」
        msg = "未录入或关联有效鉴权凭证"
        final_type = "pending"
        final_status = msg
    else:
        auth = load_credential_ssh_auth(
            db,
            asset.credential_ref,
            current_user,
            fallback_password=asset.ssh_password,
        )
        is_alive, msg = test_ssh(asset.ip, asset.ssh_port, asset.ssh_user, auth.password, auth.pkey_content)
        final_type = "healthy" if is_alive else "critical"
        final_status = msg

    new_asset = Asset(
        instance_id=f"ins-{str(uuid.uuid4())[:8]}",
        name=asset.name.strip(),
        ip=asset.ip.strip(),
        status=final_status,
        type=final_type,
        group_id=asset.group_id,
        cpu=0.0,
        uptime="刚刚接入",
        provider=asset.provider,
        region=(asset.region or "").strip() or "local",
        remark=asset.remark.strip(),
        os=asset.os.strip(),
        ssh_port=asset.ssh_port,
        ssh_user=asset.ssh_user.strip(),
        ssh_password=encrypt_secret(asset.ssh_password) if asset.ssh_password else "",
        credential_ref=asset.credential_ref,
        cpu_cores=float(asset.cpu_cores or 0.0),
        memory_gb=float(asset.memory_gb or 0.0),
        system_disk_gb=float(asset.system_disk_gb or 0.0),
        config_task_status="idle",
        config_task_updated_at="",
        config_task_error="",
        health_last_checked_at=check_time,
    )
    db.add(new_asset)
    db.flush()
    sync_host_group_membership(db, new_asset.instance_id, asset.group_id)
    sync_credential_usage(db, {asset.credential_ref})
    if has_auth:
        set_collect_task_state(new_asset, "collecting", "", check_time)
    db.commit()
    db.refresh(new_asset)

    if has_auth:
        if queue_collect_task(new_asset, background_tasks):
            db.commit()
            db.refresh(new_asset)
    return serialize_asset(new_asset)

class PingRequest(BaseModel):
    asset_id: str = ""
    ip: str
    port: int
    user: str
    password: str = ""
    credential_id: str = ""
    pkey_content: str = ""


class TerminalExecRequest(BaseModel):
    asset_id: str
    command: str
    timeout_sec: int = 12


class TerminalSessionCreateRequest(BaseModel):
    asset_id: str
    cols: int = 120
    rows: int = 36


class TerminalFileMkdirRequest(BaseModel):
    """WebSSH 文件面板的新建目录请求。"""

    path: str


def terminal_sftp_upload_limit_bytes() -> int:
    """
    读取 WebSSH SFTP 单文件上传上限。

    说明：使用 MB 作为配置单位，便于运维在 `.env` 中调整；
    代码层强制限制在 1MB ~ 10240MB，避免误配置为 0 或极端大值。
    """
    max_mb = int(getattr(settings, "TERMINAL_SFTP_UPLOAD_MAX_MB", 100) or 100)
    safe_mb = min(max(max_mb, 1), 10240)
    return safe_mb * 1024 * 1024


def normalize_sftp_path(raw_path: str, default: str = "/root") -> str:
    """
    规范化远端 SFTP 路径。

    安全边界：
    1. 禁止 NUL 字符，避免底层库路径截断风险；
    2. 所有相对路径统一转换为绝对路径；
    3. `..` 会被规范化，但最终访问权限仍由目标主机 SSH 用户决定。
    """
    text = (raw_path or "").strip() or default
    if "\x00" in text:
        raise HTTPException(status_code=400, detail="路径包含非法字符")
    if not text.startswith("/"):
        text = f"/{text}"
    normalized = posixpath.normpath(text)
    return "/" if normalized in {"", "."} else normalized


def parse_private_key_content(pkey_content: str) -> paramiko.PKey | None:
    """解析 SSH 私钥内容，兼容 RSA / ED25519 / ECDSA。"""
    raw = (pkey_content or "").strip()
    if not raw:
        return None
    for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(raw))
        except Exception:
            continue
    return None


def open_asset_sftp(
    asset: Asset,
    ssh_password: str,
    pkey_content: str,
) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    """
    为指定资产打开 SFTP 连接。

    说明：后端只使用资产绑定的 SSH 凭据，不把任何敏感内容回传给前端。
    """
    if not ssh_password and not pkey_content:
        raise HTTPException(status_code=400, detail="未配置 SSH 密码或凭证，无法打开文件面板")

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, asset.ip, int(asset.ssh_port or 22))
    try:
        pkey = parse_private_key_content(pkey_content)
        if pkey_content and not pkey:
            raise HTTPException(status_code=400, detail="私钥解析失败，请检查凭证格式")

        connect_kwargs = {
            "hostname": asset.ip,
            "port": int(asset.ssh_port or 22),
            "username": asset.ssh_user or "root",
            "timeout": 8.0,
            "banner_timeout": 8.0,
            "auth_timeout": 8.0,
        }
        if pkey:
            connect_kwargs["pkey"] = pkey
        else:
            connect_kwargs["password"] = ssh_password

        client.connect(**connect_kwargs)
        return client, client.open_sftp()
    except HTTPException:
        client.close()
        raise
    except paramiko.AuthenticationException as exc:
        client.close()
        raise HTTPException(status_code=400, detail="SSH 鉴权失败：用户名/密码或私钥不正确") from exc
    except (socket.timeout, socket.error) as exc:
        client.close()
        raise HTTPException(status_code=400, detail="SSH 网络不可达或端口不可用") from exc
    except paramiko.SSHException as exc:
        client.close()
        if HOST_KEY_MISMATCH_MESSAGE in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc  # 指纹钉扎拦截保持原文提示
        raise HTTPException(status_code=400, detail="SSH/SFTP 协议协商失败") from exc


def resolve_asset_sftp(
    db: Session,
    asset_id: str,
    current_user: User | None = None,
) -> tuple[Asset, paramiko.SSHClient, paramiko.SFTPClient]:
    """读取资产与凭据，并建立 SFTP 连接。"""
    normalized_asset_id = (asset_id or "").strip()
    if not normalized_asset_id:
        raise HTTPException(status_code=400, detail="asset_id 不能为空")

    asset = db.query(Asset).filter(Asset.instance_id == normalized_asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    ensure_asset_credential_access(db, asset, current_user)

    auth = resolve_asset_ssh_auth(db, asset, current_user)
    client, sftp = open_asset_sftp(asset, auth.password, auth.pkey_content)
    return asset, client, sftp


def serialize_sftp_attr(name: str, parent_path: str, attrs: paramiko.SFTPAttributes) -> dict:
    """将 Paramiko 文件属性转换为前端可消费的轻量结构。"""
    mode = int(getattr(attrs, "st_mode", 0) or 0)
    modified_ts = int(getattr(attrs, "st_mtime", 0) or 0)
    return {
        "name": name,
        "path": posixpath.join(parent_path, name) if parent_path != "/" else f"/{name}",
        "size": int(getattr(attrs, "st_size", 0) or 0),
        "is_dir": stat.S_ISDIR(mode),
        "is_symlink": stat.S_ISLNK(mode),
        "mode": oct(mode & 0o777),
        "modified_at": datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d %H:%M:%S") if modified_ts else "",
    }

@router.post("/ping")
def ping_asset(
    req: PingRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.connect")),
):
    """单独的测试连接断点；编辑已有主机时同步更新资产状态。"""
    # 新增主机弹窗允许先用本次输入的私钥做一次性连通性探测。
    # 私钥只在内存中交给 Paramiko，不写入资产表；真正保存主机时仍走凭证中心加密入库。
    credential_auth = None
    if str(req.credential_id or "").strip():
        credential_auth = load_credential_ssh_auth(
            db,
            req.credential_id,
            current_user,
            fallback_password=req.password,
        )
    auth_password, auth_pkey_content = resolve_ping_auth(
        password=req.password,
        credential_id=req.credential_id,
        pkey_content=req.pkey_content,
        credential_auth=credential_auth,
    )
    ok, msg = test_ssh(req.ip, req.port, req.user, auth_password, auth_pkey_content)
    asset_id = (req.asset_id or "").strip()
    if asset_id:
        ensure_asset_ping_write_permission(asset_id, current_user)
        asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
        if not asset:
            raise HTTPException(status_code=404, detail="主机实例未找到")
        asset.ip = req.ip
        asset.ssh_port = req.port
        asset.ssh_user = req.user
        if not str(req.pkey_content or "").strip():
            asset.credential_ref = req.credential_id or ""
            # 仅在用户本次输入了新密码时覆盖；空值不改动已保存密码
            apply_password_update(asset, req.password or "")
        asset.type = "healthy" if ok else "critical"
        asset.status = msg
        asset.health_last_checked_at = now_ts()
        db.commit()

    if ok:
        return {"success": True, "message": "SSH 登录认证成功，可执行远程命令。"}
    return {"success": False, "message": f"探测失败：{msg}"}


def run_ssh_command(
    ip: str,
    port: int,
    user: str,
    command: str,
    timeout_sec: int,
    pwd: str = "",
    pkey_content: str = "",
) -> tuple[bool, str, str, str, int]:
    """通过 SSH 执行单条命令，返回执行结果。"""
    if not pwd and not pkey_content:
        return False, "未录入或关联有效鉴权凭证", "", "", -1

    client = paramiko.SSHClient()
    # 主机指纹 TOFU+钉扎：首见落库放行，不一致拦截（替代旧的信任一切策略）。
    pin_and_connect(client, ip, port)
    try:
        if pkey_content:
            pkey = None
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                try:
                    pkey = key_cls.from_private_key(io.StringIO(pkey_content))
                    break
                except Exception:
                    continue
            if not pkey:
                return False, _pkey_parse_error(pkey_content), "", "", -1
            client.connect(hostname=ip, port=port, username=user, pkey=pkey, timeout=4.0, auth_timeout=6.0, banner_timeout=6.0)
        else:
            client.connect(hostname=ip, port=port, username=user, password=pwd, timeout=4.0, auth_timeout=6.0, banner_timeout=6.0)

        _, stdout, stderr = client.exec_command(command, timeout=timeout_sec)
        out = (stdout.read() or b"").decode(errors="ignore")
        err = (stderr.read() or b"").decode(errors="ignore")
        exit_code = int(stdout.channel.recv_exit_status())
        return True, "命令执行完成", out, err, exit_code
    except paramiko.AuthenticationException:
        return False, "密码错误或私钥拒绝被访问", "", "", -1
    except (socket.timeout, socket.error):
        return False, "网络超时不可达或22端口阻断", "", "", -1
    except paramiko.SSHException as exc:
        if HOST_KEY_MISMATCH_MESSAGE in str(exc):
            return False, str(exc), "", "", -1  # 指纹钉扎拦截保持原文提示
        return False, "安全协商失败/非标准SSH端", "", "", -1
    except Exception as e:
        print("[SSH TERMINAL COMMAND FAILURE]", e)
        return False, "系统内核级未知拨号异常", "", "", -1
    finally:
        try:
            client.close()
        except Exception:
            pass


def trim_terminal_output(text: str, max_len: int = 12000) -> str:
    raw = text or ""
    if len(raw) <= max_len:
        return raw
    return raw[:max_len] + "\n... output truncated ..."


@router.post("/terminal/exec")
def exec_terminal_command(
    req: TerminalExecRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.connect")),
):
    """按资产执行 SSH 命令（无状态执行）。"""
    asset_id = (req.asset_id or "").strip()
    command = (req.command or "").strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id 不能为空")
    if not command:
        raise HTTPException(status_code=400, detail="命令不能为空")
    if len(command) > 2000:
        raise HTTPException(status_code=400, detail="命令过长，请控制在 2000 字符以内")

    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    ensure_asset_credential_access(db, asset, current_user)

    timeout_sec = int(req.timeout_sec or 12)
    timeout_sec = min(max(timeout_sec, 3), 30)

    auth = resolve_asset_ssh_auth(db, asset, current_user)

    ok, msg, out, err, code = run_ssh_command(
        asset.ip,
        int(asset.ssh_port or 22),
        asset.ssh_user or "root",
        command,
        timeout_sec,
        auth.password,
        auth.pkey_content,
    )

    checked_at = now_ts()
    if ok:
        asset.type = "healthy"
        asset.status = "SSH 登录认证成功"
    else:
        asset.type = "pending" if "未录入或关联有效鉴权凭证" in msg else "critical"
        asset.status = msg
    asset.health_last_checked_at = checked_at
    db.commit()

    return {
        "success": ok,
        "message": msg,
        "stdout": trim_terminal_output(out),
        "stderr": trim_terminal_output(err),
        "exit_code": code,
        "asset_id": asset.instance_id,
        "executed_at": checked_at,
    }


@router.post("/terminal/session")
def create_terminal_ws_session(
    req: TerminalSessionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """
    创建 Web 终端一次性会话。

    安全设计：
    1. 仅允许具备权限的登录用户创建；
    2. 返回短期 token，供前端立即发起 WebSocket；
    3. SSH 凭据仅保留在后端，不回传前端。
    """
    asset_id = (req.asset_id or "").strip()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id 不能为空")

    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    ensure_asset_credential_access(db, asset, current_user)

    auth = resolve_asset_ssh_auth(db, asset, current_user)

    if not auth.password and not auth.pkey_content:
        raise HTTPException(status_code=400, detail="未配置 SSH 密码或凭证，无法打开终端")

    session_id, token, expire_epoch_ms = create_terminal_session(
        asset_id=asset.instance_id,
        asset_name=asset.name or asset.instance_id,
        ip=asset.ip,
        port=int(asset.ssh_port or 22),
        user=asset.ssh_user or "root",
        password=auth.password,
        pkey_content=auth.pkey_content,
        created_by=current_user.name or current_user.username,
        cols=req.cols,
        rows=req.rows,
    )

    # 返回相对路径，前端根据当前协议自动拼 ws/wss，兼容反向代理。
    ws_path = f"/api/terminal/ws/{session_id}"
    return {
        "success": True,
        "message": "终端会话已创建",
        "session_id": session_id,
        "session_token": token,
        "expires_at": expire_epoch_ms,
        "ws_path": ws_path,
        "asset": {
            "id": asset.instance_id,
            "name": asset.name,
            "ip": asset.ip,
            "ssh_user": asset.ssh_user or "root",
            "ssh_port": int(asset.ssh_port or 22),
        },
    }


@router.get("/terminal/files/{asset_id}/list")
def list_terminal_files(
    asset_id: str,
    path: str = Query("/root"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """列出远端目录内容，供 WebSSH 右侧文件面板使用。"""
    remote_path = normalize_sftp_path(path)
    _, client, sftp = resolve_asset_sftp(db, asset_id, current_user)
    try:
        entries = sftp.listdir_attr(remote_path)
        items = [
            serialize_sftp_attr(attr.filename, remote_path, attr)
            for attr in entries
            if attr.filename not in {".", ".."}
        ]
        items.sort(key=lambda item: (not item["is_dir"], str(item["name"]).lower()))
        return {
            "success": True,
            "path": remote_path,
            "parent": "/" if remote_path == "/" else posixpath.dirname(remote_path),
            "items": items,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="远端目录不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="远端目录无访问权限") from exc
    finally:
        try:
            sftp.close()
        finally:
            client.close()


@router.get("/terminal/files/config")
def get_terminal_file_config(
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """返回 WebSSH 文件面板运行参数，供前端做上传前预检。"""
    upload_limit_bytes = terminal_sftp_upload_limit_bytes()
    return {
        "success": True,
        "max_upload_bytes": upload_limit_bytes,
        "max_upload_mb": upload_limit_bytes // (1024 * 1024),
    }


@router.get("/terminal/files/{asset_id}/download")
def download_terminal_file(
    asset_id: str,
    request: Request,
    path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """流式下载远端文件，后端不落盘保存文件内容。"""
    remote_path = normalize_sftp_path(path)
    _, client, sftp = resolve_asset_sftp(db, asset_id, current_user)
    try:
        attrs = sftp.stat(remote_path)
        if stat.S_ISDIR(int(attrs.st_mode or 0)):
            sftp.close()
            client.close()
            write_terminal_file_audit(db, request, current_user, "下载", asset_id, remote_path, "failed")
            raise HTTPException(status_code=400, detail="目录不能直接下载")
        remote_file = sftp.open(remote_path, "rb")
        write_terminal_file_audit(db, request, current_user, "下载", asset_id, remote_path, "success")
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        sftp.close()
        client.close()
        write_terminal_file_audit(db, request, current_user, "下载", asset_id, remote_path, "failed")
        raise HTTPException(status_code=404, detail="远端文件不存在") from exc
    except PermissionError as exc:
        sftp.close()
        client.close()
        write_terminal_file_audit(db, request, current_user, "下载", asset_id, remote_path, "failed")
        raise HTTPException(status_code=403, detail="远端文件无读取权限") from exc

    def iter_file():
        """按块读取远端文件，响应结束时统一关闭 SFTP 资源。"""
        try:
            while True:
                chunk = remote_file.read(SFTP_STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            finally:
                try:
                    sftp.close()
                finally:
                    client.close()

    file_name = posixpath.basename(remote_path) or "download"
    encoded_name = quote(file_name)
    return StreamingResponse(
        iter_file(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.post("/terminal/files/{asset_id}/upload")
def upload_terminal_file(
    asset_id: str,
    request: Request,
    path: str = Form("/root"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """上传文件到远端目录，并限制单文件大小。"""
    remote_dir = normalize_sftp_path(path)
    filename = posixpath.basename((file.filename or "").strip())
    if not filename or filename in {".", ".."} or "\x00" in filename:
        raise HTTPException(status_code=400, detail="文件名非法")

    remote_path = posixpath.join(remote_dir, filename)
    _, client, sftp = resolve_asset_sftp(db, asset_id, current_user)
    written = 0
    upload_limit_bytes = terminal_sftp_upload_limit_bytes()
    remote_file = None
    try:
        remote_file = sftp.open(remote_path, "wb")
        while True:
            chunk = file.file.read(SFTP_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > upload_limit_bytes:
                upload_limit_mb = upload_limit_bytes // (1024 * 1024)
                raise HTTPException(status_code=413, detail=f"文件超过 {upload_limit_mb}MB 上传限制")
            remote_file.write(chunk)
        write_terminal_file_audit(db, request, current_user, "上传", asset_id, remote_path, "success")
        return {
            "success": True,
            "message": "上传完成",
            "path": remote_path,
            "size": written,
        }
    except HTTPException:
        try:
            if remote_file:
                remote_file.close()
            sftp.remove(remote_path)
        except Exception:
            pass
        write_terminal_file_audit(db, request, current_user, "上传", asset_id, remote_path, "failed")
        raise
    except PermissionError as exc:
        write_terminal_file_audit(db, request, current_user, "上传", asset_id, remote_path, "failed")
        raise HTTPException(status_code=403, detail="远端目录无写入权限") from exc
    finally:
        try:
            if remote_file:
                remote_file.close()
        finally:
            try:
                sftp.close()
            finally:
                client.close()


@router.post("/terminal/files/{asset_id}/mkdir")
def mkdir_terminal_file(
    asset_id: str,
    req: TerminalFileMkdirRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """在远端主机创建目录。"""
    remote_path = normalize_sftp_path(req.path)
    _, client, sftp = resolve_asset_sftp(db, asset_id, current_user)
    try:
        sftp.mkdir(remote_path)
        write_terminal_file_audit(db, request, current_user, "新建目录", asset_id, remote_path, "success")
        return {"success": True, "message": "目录创建完成", "path": remote_path}
    except FileExistsError as exc:
        write_terminal_file_audit(db, request, current_user, "新建目录", asset_id, remote_path, "failed")
        raise HTTPException(status_code=409, detail="目录已存在") from exc
    except PermissionError as exc:
        write_terminal_file_audit(db, request, current_user, "新建目录", asset_id, remote_path, "failed")
        raise HTTPException(status_code=403, detail="远端目录无写入权限") from exc
    finally:
        try:
            sftp.close()
        finally:
            client.close()


@router.delete("/terminal/files/{asset_id}")
def delete_terminal_file(
    asset_id: str,
    request: Request,
    path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("module.tools.terminal", "action.assets.hosts.connect")),
):
    """删除远端文件或空目录。非空目录不做递归删除，避免误删。"""
    remote_path = normalize_sftp_path(path)
    if remote_path == "/":
        write_terminal_file_audit(db, request, current_user, "删除", asset_id, remote_path, "failed")
        raise HTTPException(status_code=400, detail="禁止删除根目录")

    _, client, sftp = resolve_asset_sftp(db, asset_id, current_user)
    try:
        attrs = sftp.stat(remote_path)
        if stat.S_ISDIR(int(attrs.st_mode or 0)):
            sftp.rmdir(remote_path)
        else:
            sftp.remove(remote_path)
        write_terminal_file_audit(db, request, current_user, "删除", asset_id, remote_path, "success")
        return {"success": True, "message": "删除完成", "path": remote_path}
    except FileNotFoundError as exc:
        write_terminal_file_audit(db, request, current_user, "删除", asset_id, remote_path, "failed")
        raise HTTPException(status_code=404, detail="远端路径不存在") from exc
    except PermissionError as exc:
        write_terminal_file_audit(db, request, current_user, "删除", asset_id, remote_path, "failed")
        raise HTTPException(status_code=403, detail="远端路径无删除权限") from exc
    except OSError as exc:
        write_terminal_file_audit(db, request, current_user, "删除", asset_id, remote_path, "failed")
        raise HTTPException(status_code=400, detail="目录非空或无法删除") from exc
    finally:
        try:
            sftp.close()
        finally:
            client.close()


@router.post("/{asset_id}/refresh-config")
def refresh_asset_config(
    asset_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """触发单台主机配置异步采集。"""
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    ensure_asset_credential_access(db, asset, current_user)

    auth = resolve_asset_ssh_auth(db, asset, current_user)
    if not auth.password and not auth.pkey_content:
        raise HTTPException(status_code=400, detail="未配置 SSH 密码或凭证，无法刷新配置")

    queued = queue_collect_task(asset, background_tasks)
    if not queued:
        return {"success": False, "message": "该主机已有采集任务在执行，请稍候刷新结果。"}
    db.commit()
    return {"success": True, "message": "已提交刷新配置任务，约 2-8 秒后生效。"}


@router.post("/batch-refresh-config")
def batch_refresh_asset_config(
    payload: BatchRefreshConfigRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "queued_ids": [], "failed": []}

    queued_ids: List[str] = []
    failed: List[dict] = []

    for asset_id in ids:
        asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
        if not asset:
            failed.append({"id": asset_id, "reason": "主机不存在"})
            continue
        try:
            ensure_asset_credential_access(db, asset, current_user)
        except HTTPException as exc:
            failed.append({"id": asset_id, "reason": str(exc.detail)})
            continue
        auth = resolve_asset_ssh_auth(db, asset, current_user)
        if not auth.password and not auth.pkey_content:
            failed.append({"id": asset_id, "reason": "未配置 SSH 密码或凭证"})
            continue
        if not queue_collect_task(asset, background_tasks):
            failed.append({"id": asset_id, "reason": "采集任务执行中，请稍后重试"})
            continue
        queued_ids.append(asset_id)

    db.commit()
    return {"success_count": len(queued_ids), "queued_ids": queued_ids, "failed": failed}

class SyncRequest(BaseModel):
    provider: str
    access_key: str = ""
    secret_key: str = ""
    region: str
    credential_id: str = ""
    instance_ids: Optional[List[str]] = None


class RegionListRequest(BaseModel):
    provider: str = "aliyun"
    credential_id: str = ""
    access_key: str = ""
    secret_key: str = ""


def resolve_cloud_regions(provider: str, access_key: str, secret_key: str) -> list[str]:
    normalized = (provider or "").strip().lower()
    if normalized == "aliyun":
        return fetch_aliyun_region_ids(access_key, secret_key)
    if normalized == "tencent":
        return fetch_tencent_region_ids(access_key, secret_key)
    if normalized == "aws":
        return fetch_aws_region_ids(access_key, secret_key)
    raise ValueError("不支持的云厂商")


@router.post("/aliyun/regions")
def get_aliyun_regions(
    req: RegionListRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """拉取阿里云区域列表，供前端同步可选区。"""
    access_key, secret_key, err = resolve_aliyun_keys(
        db, req.credential_id, req.access_key, req.secret_key, current_user
    )
    if err:
        return {"success": False, "message": err, "regions": []}

    try:
        regions = resolve_cloud_regions("aliyun", access_key, secret_key)
        if not regions:
            return {"success": False, "message": "未获取到可用区域列表", "regions": []}
        return {"success": True, "regions": regions}
    except Exception as e:
        return {"success": False, "message": f"拉取区域列表失败: {str(e)}", "regions": []}


@router.post("/cloud/regions")
def get_cloud_regions(
    req: RegionListRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """按云厂商动态拉取可用区域列表（阿里云/腾讯云/AWS）。"""
    provider = (req.provider or "").strip().lower()
    if provider not in {"aliyun", "tencent", "aws"}:
        return {"success": False, "message": "不支持的云厂商", "regions": []}

    access_key, secret_key, err = resolve_aliyun_keys(
        db, req.credential_id, req.access_key, req.secret_key, current_user
    )
    if err:
        return {"success": False, "message": err, "regions": []}

    try:
        regions = resolve_cloud_regions(provider, access_key, secret_key)
        if not regions:
            return {"success": False, "message": "未获取到可用区域列表", "regions": []}
        return {"success": True, "regions": regions}
    except Exception as e:
        return {"success": False, "message": f"拉取区域列表失败: {str(e)}", "regions": []}


@router.post("/sync")
def sync_assets(
    req: SyncRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """云账号同步主机的接入网关。"""
    scanned_hosts: List[dict] = []
    provider = (req.provider or "").strip().lower()
    if provider not in {"aliyun", "tencent", "aws"}:
        return {"success": False, "message": "不支持的云厂商或未实现"}

    access_key, secret_key, err = resolve_aliyun_keys(
        db, req.credential_id, req.access_key, req.secret_key, current_user
    )
    if err:
        return {"success": False, "message": err}

    cloud_cred_ref = req.credential_id
    if not cloud_cred_ref and req.access_key:
        cloud_cred_ref = f"手动输入({req.access_key[:8]}...)"

    if provider == "aliyun":
        try:
            from alibabacloud_ecs20140526.client import Client as EcsClient
            from alibabacloud_tea_openapi import models as open_api_models
            from alibabacloud_ecs20140526 import models as ecs_models

            config = open_api_models.Config(
                access_key_id=access_key,
                access_key_secret=secret_key
            )

            regions_to_scan = [req.region]
            if req.region == "auto":
                try:
                    regions_to_scan = fetch_aliyun_region_ids(access_key, secret_key)
                except Exception:
                    regions_to_scan = ["cn-hangzhou", "cn-beijing", "cn-shanghai", "cn-shenzhen", "cn-guangzhou", "cn-wulanchabu", "cn-hongkong", "ap-southeast-1", "us-east-1"]

            sync_count = 0
            errors = []
            for rgn in regions_to_scan:
                try:
                    config.endpoint = f"ecs.{rgn}.aliyuncs.com"
                    client = EcsClient(config)
                    describe_req = ecs_models.DescribeInstancesRequest(region_id=rgn)
                    res = client.describe_instances(describe_req)

                    if res.body.instances and res.body.instances.instance:
                        for inst in res.body.instances.instance:
                            pub_ip = ""
                            if inst.public_ip_address and getattr(inst.public_ip_address, 'ip_address', []):
                                pub_ip = inst.public_ip_address.ip_address[0]
                            elif inst.eip_address and getattr(inst.eip_address, 'ip_address', ''):
                                pub_ip = inst.eip_address.ip_address

                            priv_ip = ""
                            if inst.vpc_attributes and getattr(inst.vpc_attributes.private_ip_address, 'ip_address', []):
                                priv_ip = inst.vpc_attributes.private_ip_address.ip_address[0]

                            ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                            private_ip = priv_ip if pub_ip else ""

                            os_name = getattr(inst, 'os_name', getattr(inst, 'osname', 'Linux'))
                            cpu_cores = float(getattr(inst, "cpu", 0) or 0)
                            memory_raw = float(getattr(inst, "memory", 0) or 0)
                            memory_gb = round(memory_raw / 1024.0, 1) if memory_raw > 64 else round(memory_raw, 1)
                            sys_disk_obj = getattr(inst, "system_disk", None)
                            system_disk_gb = float(
                                getattr(sys_disk_obj, "size", 0)
                                or getattr(inst, "system_disk_size", 0)
                                or 0
                            )

                            inst_name = getattr(inst, 'instance_name', f"ecs-{inst.instance_id}")
                            scanned_hosts.append({
                                "instance_id": inst.instance_id,
                                "name": inst_name,
                                "ip": ip,
                                "private_ip": private_ip,
                                "status": f"云主机,真·阿里云,{getattr(inst, 'status', '未知状态')}",
                                "provider": "aliyun",
                                "region": inst.region_id,
                                "cloud_resource_type": "ECS",
                                "os": os_name,
                                "cpu_cores": cpu_cores,
                                "memory_gb": memory_gb,
                                "system_disk_gb": system_disk_gb,
                                "is_imported": db.query(Asset).filter(Asset.instance_id == inst.instance_id).first() is not None,
                            })

                            if req.instance_ids is None:
                                continue
                            if inst.instance_id not in req.instance_ids:
                                continue

                            exist = db.query(Asset).filter(Asset.instance_id == inst.instance_id).first()
                            if exist:
                                changed = patch_existing_cloud_asset(
                                    exist,
                                    region=rgn,
                                    os_name=os_name,
                                    cpu_cores=cpu_cores,
                                    memory_gb=memory_gb,
                                    system_disk_gb=system_disk_gb,
                                    resource_type="ECS",
                                    cloud_credential_ref=cloud_cred_ref,
                                    private_ip=private_ip,
                                )
                                if changed:
                                    sync_count += 1
                                continue

                            db.add(Asset(
                                instance_id=inst.instance_id,
                                name=inst_name,
                                ip=ip,
                                private_ip=private_ip,
                                status=f"云主机,真·阿里云,{getattr(inst, 'status', '未知状态')}",
                                type="pending",
                                cpu=getattr(inst, 'cpu', 0),
                                uptime="真实调取",
                                provider="aliyun",
                                region=inst.region_id,
                                cloud_resource_type="ECS",
                                os=os_name,
                                ssh_port=22,
                                ssh_user="root",
                                ssh_password="",
                                credential_ref="",
                                cloud_credential_ref=cloud_cred_ref,
                                cpu_cores=cpu_cores,
                                memory_gb=memory_gb,
                                system_disk_gb=system_disk_gb,
                            ))
                            sync_count += 1
                except Exception as loop_e:
                    errors.append(f"[{rgn}] {str(loop_e)}")
                    pass

            # 增加轻量应用服务器 (SWAS) 的同步
            try:
                from alibabacloud_swas_open20200601.client import Client as SwasClient
                from alibabacloud_swas_open20200601 import models as swas_models

                swas_config = open_api_models.Config(
                    access_key_id=access_key,
                    access_key_secret=secret_key,
                )
                for rgn in regions_to_scan:
                    try:
                        swas_config.endpoint = f"swas.{rgn}.aliyuncs.com"
                        swas_client = SwasClient(swas_config)
                        swas_req = swas_models.ListInstancesRequest(region_id=rgn)
                        swas_resp = swas_client.list_instances(swas_req)
                        for inst in swas_resp.body.instances or []:
                            pub_ip = inst.public_ip_address or ""
                            priv_ip = inst.inner_ip_address or ""
                            ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                            private_ip = priv_ip if pub_ip else ""

                            inst_name = inst.instance_name or f"swas-{inst.instance_id}"
                            os_name_str = "Linux"
                            if inst.image:
                                if inst.image.os_type:
                                    os_name_str = inst.image.os_type
                                elif inst.image.image_name:
                                    os_name_str = inst.image.image_name

                            cpu_cores = 0.0
                            memory_gb = 0.0
                            system_disk_gb = 0.0
                            if inst.resource_spec:
                                if inst.resource_spec.cpu:
                                    cpu_cores = float(inst.resource_spec.cpu)
                                if inst.resource_spec.memory:
                                    memory_gb = float(inst.resource_spec.memory)
                                if inst.resource_spec.disk_size:
                                    system_disk_gb = float(inst.resource_spec.disk_size)

                            scanned_hosts.append({
                                "instance_id": inst.instance_id,
                                "name": inst_name,
                                "ip": ip,
                                "private_ip": private_ip,
                                "status": f"阿里云轻量,{inst.status}",
                                "provider": "aliyun",
                                "region": rgn,
                                "cloud_resource_type": "SWAS",
                                "os": os_name_str,
                                "cpu_cores": cpu_cores,
                                "memory_gb": memory_gb,
                                "system_disk_gb": system_disk_gb,
                                "is_imported": db.query(Asset).filter(Asset.instance_id == inst.instance_id).first() is not None,
                            })

                            if req.instance_ids is None:
                                continue
                            if inst.instance_id not in req.instance_ids:
                                continue

                            exist = db.query(Asset).filter(Asset.instance_id == inst.instance_id).first()
                            if exist:
                                if patch_existing_cloud_asset(
                                    exist,
                                    region=rgn,
                                    os_name=os_name_str,
                                    cpu_cores=cpu_cores,
                                    memory_gb=memory_gb,
                                    system_disk_gb=system_disk_gb,
                                    resource_type="SWAS",
                                    cloud_credential_ref=cloud_cred_ref,
                                    private_ip=private_ip,
                                ):
                                    sync_count += 1
                                continue

                            db.add(Asset(
                                instance_id=inst.instance_id,
                                name=inst_name,
                                ip=ip,
                                private_ip=private_ip,
                                status=f"阿里云轻量,{inst.status}",
                                type="pending",
                                provider="aliyun",
                                region=rgn,
                                cloud_resource_type="SWAS",
                                os=os_name_str,
                                cpu_cores=cpu_cores,
                                memory_gb=memory_gb,
                                system_disk_gb=system_disk_gb,
                                ssh_port=22, ssh_user="root", ssh_password="", credential_ref="",
                                cloud_credential_ref=cloud_cred_ref,
                            ))
                            sync_count += 1
                    except Exception as e:
                        print("Aliyun SWAS Error:", e)
            except Exception as e:
                print("Aliyun SWAS Module Error:", e)

            db.commit()
            if req.instance_ids is None:
                return {"success": True, "preview": True, "instances": scanned_hosts}

            if sync_count == 0:
                if len(errors) > 0:
                    err_summary = " | ".join(errors[:2])
                    err_msg = f"未扫描到机器，拦截到报错(请确认AK权限): {err_summary}"
                    create_notification(
                        db,
                        data=NotificationCreate(
                            user_id=current_user.id,
                            type="error",
                            category="host",
                            title="阿里云主机同步失败",
                            content=err_msg,
                            link="/assets/hosts",
                        ),
                    )
                    return {"success": False, "message": err_msg}
                else:
                    err_msg = "您的AK/SK完全合法，但在全区扫描中得到了 0 个 ECS() 返回值！请问您的主机是【轻量应用服务器】(SWAS)吗？ECS 接口扫不到它们，需要专门对接！"
                    create_notification(
                        db,
                        data=NotificationCreate(
                            user_id=current_user.id,
                            type="warning",
                            category="host",
                            title="阿里云主机同步无结果",
                            content=err_msg,
                            link="/assets/hosts",
                        ),
                    )
                    return {"success": False, "message": err_msg}

            scan_hint = "各大可用区全局" if req.region == "auto" else f"[{req.region}]可用区"
            success_msg = f"成功从阿里云 {scan_hint} 同步/补齐 {sync_count} 台实例！"
            create_notification(
                db,
                data=NotificationCreate(
                    user_id=current_user.id,
                    type="success",
                    category="host",
                    title="阿里云主机同步完成",
                    content=success_msg,
                    link="/assets/hosts",
                ),
            )
            return {"success": True, "message": success_msg}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"success": False, "message": f"阿里云API对接报错：{e}"}

    elif provider == "tencent":
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models
            from tencentcloud.lighthouse.v20200324 import lighthouse_client, models as lh_models

            cred = credential.Credential(access_key, secret_key)
            regions_to_scan = [req.region]
            if req.region == "auto":
                try:
                    regions_to_scan = fetch_tencent_region_ids(access_key, secret_key)
                except Exception:
                    regions_to_scan = [
                        "ap-guangzhou",
                        "ap-shanghai",
                        "ap-beijing",
                        "ap-hongkong",
                        "ap-singapore",
                    ]

            sync_count = 0
            errors: list[str] = []

            for rgn in regions_to_scan:
                # CVM
                try:
                    httpProfile = HttpProfile()
                    httpProfile.endpoint = "cvm.tencentcloudapi.com"
                    clientProfile = ClientProfile()
                    clientProfile.httpProfile = httpProfile
                    c_client = cvm_client.CvmClient(cred, rgn, clientProfile)
                    c_req = cvm_models.DescribeInstancesRequest()
                    c_resp = c_client.DescribeInstances(c_req)
                    for inst in c_resp.InstanceSet or []:
                        pub_ip = inst.PublicIpAddresses[0] if inst.PublicIpAddresses else ""
                        priv_ip = inst.PrivateIpAddresses[0] if inst.PrivateIpAddresses else ""
                        ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                        private_ip = priv_ip if pub_ip else ""

                        cpu = float(inst.CPU or 0)
                        mem_gb = float(inst.Memory or 0)
                        disk_obj = getattr(inst, "SystemDisk", None)
                        disk_gb = float(getattr(disk_obj, "DiskSize", 0) or 0)
                        os_name = str(inst.OsName or "Linux")
                        inst_name = inst.InstanceName or f"cvm-{inst.InstanceId}"

                        scanned_hosts.append({
                            "instance_id": inst.InstanceId,
                            "name": inst_name,
                            "ip": ip,
                            "private_ip": private_ip,
                            "status": f"腾讯云主机,{inst.InstanceState}",
                            "provider": "tencent",
                            "region": rgn,
                            "cloud_resource_type": "CVM",
                            "os": os_name,
                            "cpu_cores": cpu,
                            "memory_gb": mem_gb,
                            "system_disk_gb": disk_gb,
                            "is_imported": db.query(Asset).filter(Asset.instance_id == inst.InstanceId).first() is not None,
                        })

                        if req.instance_ids is None:
                            continue
                        if inst.InstanceId not in req.instance_ids:
                            continue

                        exist = db.query(Asset).filter(Asset.instance_id == inst.InstanceId).first()
                        if exist:
                            if patch_existing_cloud_asset(
                                exist,
                                region=rgn,
                                os_name=os_name,
                                cpu_cores=cpu,
                                memory_gb=mem_gb,
                                system_disk_gb=disk_gb,
                                resource_type="CVM",
                                cloud_credential_ref=cloud_cred_ref,
                                private_ip=private_ip,
                            ):
                                sync_count += 1
                            continue

                        db.add(Asset(
                            instance_id=inst.InstanceId,
                            name=inst_name,
                            ip=ip,
                            private_ip=private_ip,
                            status=f"腾讯云主机,{inst.InstanceState}",
                            type="pending",
                            provider="tencent",
                            region=rgn,
                            cloud_resource_type="CVM",
                            os=os_name,
                            ssh_port=22, ssh_user="root", ssh_password="", credential_ref="",
                            cloud_credential_ref=cloud_cred_ref,
                            cpu_cores=cpu, memory_gb=mem_gb, system_disk_gb=disk_gb,
                        ))
                        sync_count += 1
                except Exception as e:
                    errors.append(f"[{rgn}/CVM] {str(e)}")

                # Lighthouse
                try:
                    lh_httpProfile = HttpProfile()
                    lh_httpProfile.endpoint = "lighthouse.tencentcloudapi.com"
                    lh_clientProfile = ClientProfile()
                    lh_clientProfile.httpProfile = lh_httpProfile
                    lh_client = lighthouse_client.LighthouseClient(cred, rgn, lh_clientProfile)
                    lh_req = lh_models.DescribeInstancesRequest()
                    lh_resp = lh_client.DescribeInstances(lh_req)
                    for inst in lh_resp.InstanceSet or []:
                        pub_ip = inst.PublicAddresses[0] if inst.PublicAddresses else ""
                        priv_ip = inst.PrivateAddresses[0] if inst.PrivateAddresses else ""
                        ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                        private_ip = priv_ip if pub_ip else ""

                        hardware = getattr(inst, "Hardware", None)
                        disk_obj = getattr(inst, "SystemDisk", None)
                        cpu = float(getattr(inst, "CPU", 0) or getattr(hardware, "CPU", 0) or 0)
                        mem_gb = float(getattr(inst, "Memory", 0) or getattr(hardware, "Memory", 0) or 0)
                        disk_gb = float(getattr(disk_obj, "DiskSize", 0) or 0)
                        os_name = str(getattr(inst, "OsName", "Linux") or "Linux")
                        inst_name = inst.InstanceName or f"lh-{inst.InstanceId}"

                        scanned_hosts.append({
                            "instance_id": inst.InstanceId,
                            "name": inst_name,
                            "ip": ip,
                            "private_ip": private_ip,
                            "status": f"腾讯云轻量,{inst.InstanceState}",
                            "provider": "tencent",
                            "region": rgn,
                            "cloud_resource_type": "Lighthouse",
                            "os": os_name,
                            "cpu_cores": cpu,
                            "memory_gb": mem_gb,
                            "system_disk_gb": disk_gb,
                            "is_imported": db.query(Asset).filter(Asset.instance_id == inst.InstanceId).first() is not None,
                        })

                        if req.instance_ids is None:
                            continue
                        if inst.InstanceId not in req.instance_ids:
                            continue

                        exist = db.query(Asset).filter(Asset.instance_id == inst.InstanceId).first()
                        if exist:
                            if patch_existing_cloud_asset(
                                exist,
                                region=rgn,
                                os_name=os_name,
                                cpu_cores=cpu,
                                memory_gb=mem_gb,
                                system_disk_gb=disk_gb,
                                resource_type="Lighthouse",
                                cloud_credential_ref=cloud_cred_ref,
                                private_ip=private_ip,
                            ):
                                sync_count += 1
                            continue

                        db.add(Asset(
                            instance_id=inst.InstanceId,
                            name=inst_name,
                            ip=ip,
                            private_ip=private_ip,
                            status=f"腾讯云轻量,{inst.InstanceState}",
                            type="pending",
                            provider="tencent",
                            region=rgn,
                            cloud_resource_type="Lighthouse",
                            os=os_name,
                            ssh_port=22, ssh_user="root", ssh_password="", credential_ref="",
                            cloud_credential_ref=cloud_cred_ref,
                            cpu_cores=cpu, memory_gb=mem_gb, system_disk_gb=disk_gb,
                        ))
                        sync_count += 1
                except Exception as e:
                    errors.append(f"[{rgn}/Lighthouse] {str(e)}")

            db.commit()
            if req.instance_ids is None:
                return {"success": True, "preview": True, "instances": scanned_hosts}

            if sync_count == 0 and errors:
                err_msg = f"腾讯云扫描完成但无可同步实例：{' | '.join(errors[:2])}"
                create_notification(
                    db,
                    data=NotificationCreate(
                        user_id=current_user.id,
                        type="warning",
                        category="host",
                        title="腾讯云主机同步无结果",
                        content=err_msg,
                        link="/assets/hosts",
                    ),
                )
                return {"success": False, "message": err_msg}
            scan_hint = "全区域" if req.region == "auto" else req.region
            success_msg = f"成功从腾讯云 {scan_hint} 同步/补齐 {sync_count} 台实例！"
            create_notification(
                db,
                data=NotificationCreate(
                    user_id=current_user.id,
                    type="success",
                    category="host",
                    title="腾讯云主机同步完成",
                    content=success_msg,
                    link="/assets/hosts",
                ),
            )
            return {"success": True, "message": success_msg}
        except Exception as e:
            return {"success": False, "message": f"腾讯云API对接报错：{e}"}

    elif provider == "aws":
        try:
            import boto3

            regions_to_scan = [req.region]
            if req.region == "auto":
                try:
                    regions_to_scan = fetch_aws_region_ids(access_key, secret_key)
                except Exception:
                    regions_to_scan = ["us-east-1", "us-west-2", "ap-southeast-1", "ap-northeast-1"]
            sync_count = 0
            errors: list[str] = []

            for rgn in regions_to_scan:
                # EC2
                try:
                    ec2 = boto3.client(
                        "ec2",
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        region_name=rgn,
                    )
                    ec2_resp = ec2.describe_instances()
                    for reservation in ec2_resp.get("Reservations", []) or []:
                        for inst in reservation.get("Instances", []) or []:
                            inst_id = str(inst.get("InstanceId") or "").strip()
                            if not inst_id:
                                continue
                            pub_ip = inst.get("PublicIpAddress") or ""
                            priv_ip = inst.get("PrivateIpAddress") or ""
                            ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                            private_ip = priv_ip if pub_ip else ""

                            name = inst_id
                            for tag in inst.get("Tags", []) or []:
                                if str(tag.get("Key") or "") == "Name":
                                    name = str(tag.get("Value") or name)
                                    break

                            cpu_opts = inst.get("CpuOptions", {}) or {}
                            cpu_cores = float(cpu_opts.get("CoreCount") or 0)
                            memory_gb = 0.0
                            system_disk_gb = 0.0
                            os_name = str(inst.get("PlatformDetails") or "Linux")

                            scanned_hosts.append({
                                "instance_id": inst_id,
                                "name": name,
                                "ip": ip,
                                "private_ip": private_ip,
                                "status": f"AWS EC2,{inst.get('State', {}).get('Name')}",
                                "provider": "aws",
                                "region": rgn,
                                "cloud_resource_type": "EC2",
                                "os": os_name,
                                "cpu_cores": cpu_cores,
                                "memory_gb": memory_gb,
                                "system_disk_gb": system_disk_gb,
                                "is_imported": db.query(Asset).filter(Asset.instance_id == inst_id).first() is not None,
                            })

                            if req.instance_ids is None:
                                continue
                            if inst_id not in req.instance_ids:
                                continue

                            exist = db.query(Asset).filter(Asset.instance_id == inst_id).first()
                            if exist:
                                if patch_existing_cloud_asset(
                                    exist,
                                    region=rgn,
                                    os_name=os_name,
                                    cpu_cores=cpu_cores,
                                    memory_gb=memory_gb,
                                    system_disk_gb=system_disk_gb,
                                    resource_type="EC2",
                                    cloud_credential_ref=cloud_cred_ref,
                                    private_ip=private_ip,
                                ):
                                    sync_count += 1
                                continue

                            db.add(Asset(
                                instance_id=inst_id,
                                name=name,
                                ip=ip,
                                private_ip=private_ip,
                                status=f"AWS EC2,{inst.get('State', {}).get('Name')}",
                                type="pending",
                                provider="aws",
                                region=rgn,
                                cloud_resource_type="EC2",
                                os=os_name,
                                ssh_port=22, ssh_user="ec2-user", ssh_password="", credential_ref="",
                                cloud_credential_ref=cloud_cred_ref,
                                cpu_cores=cpu_cores,
                                memory_gb=memory_gb,
                                system_disk_gb=system_disk_gb,
                            ))
                            sync_count += 1
                except Exception as e:
                    errors.append(f"[{rgn}/EC2] {str(e)}")

                # Lightsail
                try:
                    ls = boto3.client(
                        "lightsail",
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        region_name=rgn,
                    )
                    ls_resp = ls.get_instances()
                    for inst in ls_resp.get("instances", []) or []:
                        inst_id = str(inst.get("name") or "").strip()
                        if not inst_id:
                            continue
                        pub_ip = inst.get("publicIpAddress") or ""
                        priv_ip = inst.get("privateIpAddress") or ""
                        ip = pub_ip if pub_ip else (priv_ip if priv_ip else "0.0.0.0")
                        private_ip = priv_ip if pub_ip else ""

                        hardware = inst.get("hardware", {}) or {}
                        cpu_cores = float(hardware.get("cpuCount") or 0)
                        memory_gb = float(hardware.get("ramSizeInGb") or 0)
                        system_disk_gb = float(hardware.get("disks", [{}])[0].get("sizeInGb") or 0)
                        os_name = str(inst.get("blueprintName") or "Linux")

                        scanned_hosts.append({
                            "instance_id": inst_id,
                            "name": inst_id,
                            "ip": ip,
                            "private_ip": private_ip,
                            "status": f"AWS Lightsail,{inst.get('state', {}).get('name')}",
                            "provider": "aws",
                            "region": rgn,
                            "cloud_resource_type": "Lightsail",
                            "os": os_name,
                            "cpu_cores": cpu_cores,
                            "memory_gb": memory_gb,
                            "system_disk_gb": system_disk_gb,
                            "ssh_user": "ubuntu",
                            "is_imported": db.query(Asset).filter(Asset.instance_id == inst_id).first() is not None,
                        })

                        if req.instance_ids is None:
                            continue
                        if inst_id not in req.instance_ids:
                            continue

                        exist = db.query(Asset).filter(Asset.instance_id == inst_id).first()
                        if exist:
                            if patch_existing_cloud_asset(
                                exist,
                                region=rgn,
                                os_name=os_name,
                                cpu_cores=cpu_cores,
                                memory_gb=memory_gb,
                                system_disk_gb=system_disk_gb,
                                resource_type="Lightsail",
                                cloud_credential_ref=cloud_cred_ref,
                                private_ip=private_ip,
                            ):
                                sync_count += 1
                            continue

                        db.add(Asset(
                            instance_id=inst_id,
                            name=inst_id,
                            ip=ip,
                            private_ip=private_ip,
                            status=f"AWS Lightsail,{inst.get('state', {}).get('name')}",
                            type="pending",
                            provider="aws",
                            region=rgn,
                            cloud_resource_type="Lightsail",
                            os=os_name,
                            ssh_port=22, ssh_user="ubuntu", ssh_password="", credential_ref="",
                            cloud_credential_ref=cloud_cred_ref,
                            cpu_cores=cpu_cores,
                            memory_gb=memory_gb,
                            system_disk_gb=system_disk_gb,
                        ))
                        sync_count += 1
                except Exception as e:
                    errors.append(f"[{rgn}/Lightsail] {str(e)}")

            db.commit()
            if req.instance_ids is None:
                return {"success": True, "preview": True, "instances": scanned_hosts}

            if sync_count == 0 and errors:
                err_msg = f"AWS 扫描完成但无可同步实例：{' | '.join(errors[:2])}"
                create_notification(
                    db,
                    data=NotificationCreate(
                        user_id=current_user.id,
                        type="warning",
                        category="host",
                        title="AWS 主机同步无结果",
                        content=err_msg,
                        link="/assets/hosts",
                    ),
                )
                return {"success": False, "message": err_msg}
            scan_hint = "全区域" if req.region == "auto" else req.region
            success_msg = f"成功从 AWS {scan_hint} 同步/补齐 {sync_count} 台实例！"
            create_notification(
                db,
                data=NotificationCreate(
                    user_id=current_user.id,
                    type="success",
                    category="host",
                    title="AWS 主机同步完成",
                    content=success_msg,
                    link="/assets/hosts",
                ),
            )
            return {"success": True, "message": success_msg}
        except Exception as e:
            return {"success": False, "message": f"AWS API对接报错：{e}"}

    return {"success": False, "message": "不支持的云厂商或未实现"}

@router.put("/{asset_id}")
def update_asset(
    asset_id: str,
    asset_data: AssetCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.hosts.manage")),
):
    """全量更新/编辑主机信息并重新检测连通性"""
    validate_asset_input(asset_data)
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")

    # IP 去重：编辑时如果 IP 变了，检查新 IP 是否已被其他主机占用
    new_ip = asset_data.ip.strip()
    if new_ip != (asset.ip or "").strip():
        conflict = db.query(Asset).filter(Asset.ip == new_ip, Asset.instance_id != asset_id).first()
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"该 IP ({new_ip}) 已被主机 \"{conflict.name}\" 使用，不能重复。",
            )

    previous_credential_ref = asset.credential_ref or ""
    cloud_metadata_locked = infer_asset_source(asset) == "cloud_sync"

    # 空密码 = 不修改；SSH 探测时回落到已保存密码，避免误判离线
    stored_password = decode_asset_password(asset)
    probe_password = resolve_update_password(asset_data.ssh_password, stored_password)
    auth = load_credential_ssh_auth(
        db,
        asset_data.credential_ref,
        current_user,
        fallback_password=probe_password,
    )
    is_alive, msg = test_ssh(
        asset_data.ip,
        asset_data.ssh_port,
        asset_data.ssh_user,
        auth.password,
        auth.pkey_content,
    )
    check_time = now_ts()

    asset.name = asset_data.name.strip()
    asset.ip = asset_data.ip.strip()
    if not cloud_metadata_locked:
        asset.provider = asset_data.provider
        asset.region = (asset_data.region or "").strip() or "local"
        asset.os = asset_data.os.strip()
    asset.remark = asset_data.remark.strip()[:255]
    asset.ssh_port = asset_data.ssh_port
    asset.ssh_user = asset_data.ssh_user.strip()
    apply_password_update(asset, asset_data.ssh_password)
    asset.credential_ref = asset_data.credential_ref
    if asset_data.cpu_cores is not None:
        asset.cpu_cores = float(asset_data.cpu_cores)
    if asset_data.memory_gb is not None:
        asset.memory_gb = float(asset_data.memory_gb)
    if asset_data.system_disk_gb is not None:
        asset.system_disk_gb = float(asset_data.system_disk_gb)

    asset.type = "healthy" if is_alive else "critical"
    asset.status = msg
    asset.group_id = asset_data.group_id
    asset.health_last_checked_at = check_time
    sync_host_group_membership(db, asset.instance_id, asset_data.group_id)

    sync_credential_usage(db, {previous_credential_ref, asset_data.credential_ref})
    config_collecting = bool((asset_data.ssh_password or "").strip() or asset_data.credential_ref.strip())
    if config_collecting:
        queued = queue_collect_task(asset, background_tasks)
        if not queued:
            set_collect_task_state(asset, "collecting", "任务执行中", check_time)
    else:
        set_collect_task_state(asset, "idle", "", check_time)
    db.commit()
    return {
        "message": "success",
        "config_collecting": config_collecting,
        "asset": serialize_asset(asset),
    }

@router.delete("/{asset_id}")
def delete_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.hosts.manage")),
    _risk=Depends(require_risk_ack("action.assets.hosts.manage", "delete")),
):
    """销毁/下线主机资产"""
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    previous_credential_ref = asset.credential_ref or ""
    sync_host_group_membership(db, asset.instance_id, None)
    db.delete(asset)
    sync_credential_usage(db, {previous_credential_ref})
    db.commit()
    return {"message": "success"}


@router.get("/host-keys")
def list_host_keys(
    host: str = Query(default=""),
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin")),
):
    """列出 SSH 主机指纹钉扎记录（管理员专用）。

    可选按 host 过滤；按 host/port/key_type 唯一，返回首连/最近校验时间。
    """
    query = db.query(AssetHostKey).order_by(AssetHostKey.host.asc(), AssetHostKey.port.asc())
    if host.strip():
        query = query.filter(AssetHostKey.host == host.strip())
    rows = query.all()
    return [
        {
            "id": row.id,
            "host": row.host,
            "port": row.port,
            "key_type": row.key_type,
            "fingerprint": row.fingerprint,
            "first_seen_at": row.first_seen_at or "",
            "last_seen_at": row.last_seen_at or "",
        }
        for row in rows
    ]


@router.post("/{asset_id}/reset-host-key")
def reset_host_key(
    asset_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin")),
):
    """清除该主机的 SSH 指纹钉扎记录（管理员专用）。

    服务器重装/换钥匙后指纹不一致会被 TOFU 策略拦截（疑似中间人）。
    此端点删除该主机:端口的全部钉扎记录，下次连接按首连重新钉扎；
    返回删除条数。属管理端高危操作，仅 admin 角色可调。
    """
    asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    hostname = (asset.ip or "").strip()
    port = int(asset.ssh_port or 22)
    if not hostname:
        raise HTTPException(status_code=400, detail="该主机没有可识别的 IP 地址")
    deleted = (
        db.query(AssetHostKey)
        .filter(AssetHostKey.host == hostname, AssetHostKey.port == port)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {
        "message": "已清除该主机的指纹钉扎记录，下次连接将按首次连接重新记录",
        "deleted": int(deleted or 0),
        "host": hostname,
        "port": port,
    }


@router.post("/batch-delete")
def batch_delete_assets(
    payload: BatchDeleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.hosts.manage")),
    _risk=Depends(require_risk_ack("action.assets.hosts.manage", "batch_delete")),
):
    """批量删除资产，并返回成功/失败明细。"""
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "success_ids": [], "failed": []}

    success_ids: List[str] = []
    failed: List[dict] = []

    for asset_id in ids:
        asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
        if not asset:
            failed.append({"id": asset_id, "reason": "主机不存在"})
            continue
        try:
            delete_asset_record(db, asset)
            db.commit()
            success_ids.append(asset_id)
        except Exception as e:
            db.rollback()
            failed.append({"id": asset_id, "reason": f"删除失败: {str(e)}"})

    return {"success_count": len(success_ids), "success_ids": success_ids, "failed": failed}

@router.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回大盘概览数字"""
    total = db.query(Asset).count()
    critical = db.query(Asset).filter(Asset.type == "critical").count()
    return {
        "total_servers": total,
        "abnormal_alerts": critical
    }
