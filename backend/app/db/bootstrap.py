"""
运行时初始化辅助模块。

职责：
1. 仅根据环境变量初始化首个管理员账号；
2. 自动注册本机为默认主机资产（Docker Alloy 已内置采集，无需 SSH 安装）；
3. 不注入任何演示 Mock 数据；
4. 启动时幂等执行，避免重复创建或覆盖现有账号。
"""

import os
import socket

from app.core.config import settings
from app.core.passwords import hash_password
from app.core.rbac import normalize_role_name
from app.db.database import SessionLocal
from app.models.asset import Asset
from app.models.user import User

# 本机主机固定 instance_id，确保幂等
LOCAL_HOST_INSTANCE_ID = "ins-localhost"


def ensure_bootstrap_admin() -> None:
    """
    根据环境变量初始化首个管理员。

    设计原则：
    - 用户名与密码必须同时存在才执行；
    - 若账号已存在则直接跳过，不重置密码；
    - 不输出明文密码到日志。
    """
    username = (settings.INIT_ADMIN_USERNAME or "").strip()
    password = settings.INIT_ADMIN_PASSWORD or ""

    db = SessionLocal()
    try:
        existing_user_count = db.query(User.id).count()

        if not username and not password:
            if existing_user_count == 0:
                print(
                    "[OpsPilot] 未检测到任何用户，且未配置 INIT_ADMIN_USERNAME / "
                    "INIT_ADMIN_PASSWORD，跳过管理员初始化。"
                )
            return

        if not username or not password:
            print(
                "[OpsPilot] 管理员初始化配置不完整：INIT_ADMIN_USERNAME 与 "
                "INIT_ADMIN_PASSWORD 必须同时提供，已跳过。"
            )
            return

        role = normalize_role_name(settings.INIT_ADMIN_ROLE or "管理员") or "管理员"
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            changed = False
            if normalize_role_name(existing_user.role) != role:
                # 只补齐引导账号角色，不重置密码，避免部署重启破坏用户密码。
                existing_user.role = role
                existing_user.permission_overrides = ""
                changed = True
            if getattr(existing_user, "status", "active") != "active":
                existing_user.status = "active"
                changed = True
            if changed:
                db.commit()
                print(f"[OpsPilot] 已补齐管理员账号 {username} 的权限配置。")
            else:
                print(f"[OpsPilot] 管理员账号 {username} 已存在，跳过初始化。")
            return

        email = (settings.INIT_ADMIN_EMAIL or f"{username}@opspilot.local").strip()
        display_name = (settings.INIT_ADMIN_NAME or "系统管理员").strip() or "系统管理员"
        department = (settings.INIT_ADMIN_DEPARTMENT or "基础平台部").strip() or "基础平台部"

        db.add(
            User(
                username=username,
                name=display_name,
                email=email,
                role=role,
                department=department,
                status="active",
                hashed_password=hash_password(password),
            )
        )
        db.commit()
        print(f"[OpsPilot] 已初始化管理员账号 {username}。")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_localhost_asset() -> None:
    """
    启动时幂等注册本机为默认主机资产。

    设计说明：
    1. 本机通过 Docker Alloy 内置采集（node + 日志），不需要 SSH 安装 Agent；
    2. 标记 provider="localhost" 作为白名单标识，前端对此类主机隐藏"安装监控"按钮；
    3. IP 取本机 hostname 解析结果（容器内回环场景会回退到 127.0.0.1）；
    4. 已存在则跳过，不覆盖任何用户手动修改。
    """
    db = SessionLocal()
    try:
        existing = db.query(Asset).filter(Asset.instance_id == LOCAL_HOST_INSTANCE_ID).first()
        if existing:
            return

        # 获取本机信息
        hostname = socket.gethostname()
        # 后端跑在容器里时 UDP 探测只能拿到容器网桥 IP（172.x），
        # 而 Alloy 的 HOST_INSTANCE 取的是安装脚本注入的 MONITORING_HOST——
        # 两者必须一致 host-metrics 才能对上资产，所以 MONITORING_HOST 优先。
        local_ip = ""
        mon_host = (os.environ.get("MONITORING_HOST") or "").strip().rstrip("/")
        if mon_host and mon_host not in ("127.0.0.1", "localhost") and not mon_host.startswith("http"):
            local_ip = mon_host
        if not local_ip:
            try:
                # 用 UDP 连接获取默认出口 IP（不实际发包），比 gethostbyname 更准确
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except Exception:
                try:
                    local_ip = socket.gethostbyname(hostname)
                except socket.gaierror:
                    local_ip = "127.0.0.1"

        db.add(
            Asset(
                instance_id=LOCAL_HOST_INSTANCE_ID,
                name=f"{hostname} (本机)",
                ip=local_ip,
                provider="localhost",
                region="local",
                os="Linux",
                type="healthy",
                ssh_port=22,
                ssh_user="root",
                remark="OpsPilot 所在主机，监控由内置 Docker Alloy 自动采集，无需手动安装 Agent。",
            )
        )
        db.commit()
        print(f"[OpsPilot] 已注册本机主机资产 {hostname} ({local_ip})。")
    except Exception:
        db.rollback()
    finally:
        db.close()


def ensure_monitoring_config() -> None:
    """首次启动写入监控连接配置（仅当尚未配置时）。

    读取环境变量（部署脚本注入），之后在「系统设置 → 监控连接」里修改：

    - ``MONITORING_HOST``：监控套件所在主机（分体部署），生成 VM/VLogs/Grafana 标准端口
    - ``VM_URL`` / ``VLOGS_URL`` / ``GRAFANA_URL``：更精确的覆盖（优先级高于 MONITORING_HOST）
    - 均未设置时：默认 ``127.0.0.1`` 标准端口（与后端同机）
    """
    import json
    import os

    from app.api.monitoring.constants import DEFAULT_STEP_SECONDS, DEFAULT_TIMEOUT_SECONDS, CONFIG_KEY
    from app.models.system_config import SystemConfig

    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.config_key == CONFIG_KEY).first()
        if row and str(row.config_value or "").strip():
            return  # 已配置，不覆盖人工修改

        host = os.environ.get("MONITORING_HOST", "").strip().rstrip("/")
        if host and not host.startswith("http"):
            host = f"http://{host}"
        if host:
            vm_url = os.environ.get("VM_URL", "").strip().rstrip("/") or f"{host}:8428"
            vlogs_url = os.environ.get("VLOGS_URL", "").strip().rstrip("/") or f"{host}:9428"
            grafana_url = os.environ.get("GRAFANA_URL", "").strip().rstrip("/") or f"{host}:3000"
        else:
            vm_url = os.environ.get("VM_URL", "").strip().rstrip("/") or "http://127.0.0.1:8428"
            vlogs_url = os.environ.get("VLOGS_URL", "").strip().rstrip("/") or "http://127.0.0.1:9428"
            grafana_url = os.environ.get("GRAFANA_URL", "").strip().rstrip("/") or "http://127.0.0.1:3000"

        content = {
            "vm_url": vm_url,
            "vlogs_url": vlogs_url,
            "grafana_url": grafana_url,
            "vmui_url": f"{vm_url}/vmui",
            "vlogsui_url": f"{vlogs_url}/select/vmui",
            "remote_write_url": "",
            "vlogs_ingest_url": "",
            "auth_type": "none",
            "username": "",
            "secret": "",
            "verify_tls": True,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "default_step_seconds": DEFAULT_STEP_SECONDS,
        }
        from datetime import datetime

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if not row:
            row = SystemConfig(
                config_key=CONFIG_KEY,
                config_value=json.dumps(content, ensure_ascii=False),
                updated_at=now_str,
            )
            db.add(row)
        else:
            row.config_value = json.dumps(content, ensure_ascii=False)
            row.updated_at = now_str
        db.commit()
        print(f"[OpsPilot] 已初始化监控连接：VM={vm_url} VLogs={vlogs_url} Grafana={grafana_url}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[OpsPilot] 监控连接初始化失败（可在系统设置里手动配置）: {exc}")
    finally:
        db.close()
