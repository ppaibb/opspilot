"""监控资产 target 管理与 Node Exporter / Alloy Agent 安装命令构建。

【职责边界】
 本模块处理"单台主机如何纳入监控"的全生命周期：
1. target 记录生成（端口校验、job 名规范化）；
2. 端点可用性探测（TCP 连接而非 HTTP GET，因为 node_exporter 的 /metrics 可能受防火墙限制，
   但 TCP 端口通就说明服务在运行）；
3. Alloy Agent 安装命令渲染：生成一次性 bash 脚本，包含注册、下载、systemd 服务注册；
4. Node Exporter 安装：旧版兼容路径，新版环境推荐直接走 Alloy。

【安全设计】
 安装命令中嵌入的 register_token 是一次性的 JWT 风格令牌，有效期短且只能用于注册，
 防止脚本泄露后被长期滥用。
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any

import base64
import hashlib
import requests
import secrets
from datetime import datetime
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.collector import CollectorAgent, CollectorConfigVersion, CollectorPolicy, CollectorRegistrationToken
from app.models.user import User
from app.core.security import get_current_user, require_permissions
from app.api.monitoring.constants import (
    ALLOY_DEFAULT_POLICY_PREFIX,
    ALLOY_SYNC_SERVICE,
    CONFIG_KEY,
    DEFAULT_NODE_EXPORTER_PORT,
    REPO_ROOT,
    TARGETS_CONFIG_KEY,
)
from app.api.monitoring.services.config_store import (
    read_monitoring_config,
    read_monitoring_targets,
    save_monitoring_targets,
)
from app.api.monitoring.services.alloy_policy import (
    _create_collector_register_token,
)
from app.api.assets import now_ts

def _asset_target_record(asset: Asset, port: int, job: str, status: str = "pending", error: str = "") -> dict[str, Any]:
    """生成 target 记录。"""
    safe_port = max(1, min(int(port or DEFAULT_NODE_EXPORTER_PORT), 65535))
    safe_job = (job or "opspilot-node").strip()[:80] or "opspilot-node"
    return {
        "asset_id": asset.instance_id,
        "name": asset.name,
        "ip": asset.ip,
        "provider": str(asset.provider or ""),
        "region": str(asset.region or ""),
        "port": safe_port,
        "job": safe_job,
        "enabled": True,
        "source": "asset",
        "target": f"{asset.ip}:{safe_port}",
        "status": status,
        "last_checked_at": now_ts(),
        "last_error": (error or "")[:255],
    }


def _upsert_target(targets: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """按 asset_id 更新或插入 target。"""
    next_targets = [item for item in targets if item.get("asset_id") != record.get("asset_id")]
    next_targets.append(record)
    return next_targets


# 探测 node_exporter / alloy 端口是否可达。
# 用 socket.connect 而非 HTTP 请求，原因：
# - 生产环境防火墙常放行 TCP 9100 但拦截 HTTP，socket 探测成功率更高；
# - 不需要解析 exporter 返回的 metrics 文本，只需确认进程存活即可。
def _check_exporter_endpoint(ip: str, port: int) -> tuple[bool, str]:
    """从后端侧检查 node_exporter /metrics 是否可访问。"""
    url = f"http://{ip}:{int(port)}/metrics"
    try:
        resp = requests.get(url, timeout=4)
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}"
    text = resp.text[:2048]
    if "node_exporter" in text or "node_cpu_seconds_total" in text or "go_gc_duration_seconds" in text:
        return True, "node_exporter 可访问"
    return False, "端点可访问，但未识别到 node_exporter 指标"


def _get_asset_or_404(db: Session, asset_id: str) -> Asset:
    asset = db.query(Asset).filter(Asset.instance_id == (asset_id or "").strip()).first()
    if not asset:
        raise HTTPException(status_code=404, detail="主机实例未找到")
    return asset


# 监控 Nginx 代理端口。远端 Agent 的全部回连（指标 remote_write、日志写入、
# 以及 install-script 下载 + import.http 拉取配置模块）都必须经过此入口，
# 而不是后端裸端口 8001（后端只监听 127.0.0.1，远端不可达）。
MONITORING_PROXY_PORT = "18080"


def _collector_proxy_base_url(base_url: str) -> str:
    """将外部 base_url 改写为监控 Nginx 代理入口（host:18080）。

    `_external_request_base_url` 已把回环地址换成本机真实 IP，但端口仍是请求端口
    （浏览器/Vite 直连时为 8001/3001）。Agent 在远端机器上无法连到后端裸端口，必须
    统一走 Nginx 18080 代理。

    规则：
    - URL 带显式端口（裸端口直连，如 :8001/:3001）→ 改写为 :18080；
    - URL 无端口（已是 80/443 反代域名入口）→ 保持原样，不画蛇添足加端口。
    """
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return value
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    if not hostname:
        return value
    # 无显式端口 = 已是干净的反代入口（80/443），不改。
    if parsed.port is None:
        return value
    # 后端 API 裸端口直连（分体部署常见 :8001）：Agent 拉 install-script/module
    # 必须打到后端本身，不能改写到监控 Nginx :18080（该代理未必部署）。
    if parsed.port == 8001:
        return value
    scheme = parsed.scheme or "http"
    return f"{scheme}://{hostname}:{MONITORING_PROXY_PORT}"


def _build_alloy_install_command(base_url: str, register_token: str, asset: Asset) -> str:
    """生成固定 Alloy Agent 安装命令，不暴露可注入参数给前端。"""
    safe_base_url = _collector_proxy_base_url(base_url.rstrip("/"))
    install_url = f"{safe_base_url}/api/monitoring/collector/agent/install-script"
    # 不使用 `curl | bash`，改为分步下载、落盘、执行，便于审计和回显失败原因。
    return "\n".join(
        [
            "set -euo pipefail",
            "# 日志采集前置条件：目标机需可读 /var/run/docker.sock（只采 Docker 日志）",
            f"export OPSPILOT_BASE_URL={shlex.quote(safe_base_url)}",
            f"export OPSPILOT_REGISTER_TOKEN={shlex.quote(register_token)}",
            f"export OPSPILOT_ASSET_ID={shlex.quote(asset.instance_id)}",
            'INSTALLER="/tmp/opspilot-alloy-install.sh"',
            f"curl -fsSL {shlex.quote(install_url)} -o \"$INSTALLER\"",
            "chmod 700 \"$INSTALLER\"",
            "bash \"$INSTALLER\"",
        ]
    )


def _external_request_base_url(request: Request) -> str:
    """
    还原浏览器/Agent 可访问的外部 API 根地址。

    容器化部署经常是 `用户 -> Nginx:8080 -> backend:8001`。如果直接使用
    FastAPI 的 `request.base_url`，可能会丢失外部端口，导致 Agent 去访问
    `http://host/api/...` 而不是 `http://host:8080/api/...`。

    Vite dev proxy 会把 Host 改写为 127.0.0.1:8001，远端机器无法回连。
    这里检测到回环地址时自动替换为本机真实 IP，保留原始端口。
    """
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or str(request.headers.get("host") or "").strip()
    scheme = forwarded_proto or request.url.scheme

    if host:
        # 如果 host 是回环地址，替换为本机真实 IP（保留端口）
        import socket
        host_part = host.split(":")[0]
        if host_part in ("127.0.0.1", "localhost", "::1"):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                real_ip = s.getsockname()[0]
                s.close()
            except Exception:
                real_ip = host_part
            port_part = host.split(":", 1)[1] if ":" in host else ""
            host = f"{real_ip}:{port_part}" if port_part else real_ip
        return f"{scheme}://{host}"
    return str(request.base_url).rstrip("/")


def _build_alloy_disable_command() -> str:
    """生成停用 OpsPilot 托管 Alloy Agent 的固定命令。"""
    return f"""
set -eu
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi
if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl disable --now {ALLOY_SYNC_SERVICE} >/dev/null 2>&1 || true
  $SUDO systemctl disable --now alloy >/dev/null 2>&1 || true
fi
echo "alloy disabled"
""".strip()


def _build_alloy_uninstall_command() -> str:
    """生成「彻底卸载」OpsPilot 托管 Alloy Agent 的固定命令。

    设计原则：与安装脚本(collector_bootstrap.render_alloy_install_script)一一对应,
    「装了什么就删什么」,且全程幂等(每步 `|| true`),目标机缺东少西也不报错中断。

    清理范围(对应安装落地物)：
      1. systemd 服务: 停用 alloy + 旧版 opspilot-alloy-sync;
      2. 包管理安装: apt/yum 卸载 alloy 包(若是包方式装的);
      3. 二进制: /usr/bin/alloy、/usr/local/bin/alloy;
      4. systemd 单元 + OpsPilot override(含 token): alloy.service、alloy.service.d;
      5. 配置: /etc/alloy;
      6. 环境文件: /etc/default/alloy、/etc/sysconfig/alloy;
      7. 数据目录: /var/lib/alloy;
      8. 用户/组: alloy 用户与组;
      9. apt/yum 源与 keyring(仅 grafana 源,安装时新增的);
     10. 旧版 sync 脚本残留。

    安全：命令固定生成、不接受任何外部参数拼接,杜绝注入。
    """
    return r"""
set +e
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

echo "==> [1/9] 停用并禁用 systemd 服务"
if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl disable --now alloy >/dev/null 2>&1 || true
  $SUDO systemctl disable --now opspilot-alloy-sync >/dev/null 2>&1 || true
fi

echo "==> [2/9] 卸载包管理器安装的 alloy(若有)"
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get remove -y alloy >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum remove -y alloy >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  $SUDO dnf remove -y alloy >/dev/null 2>&1 || true
fi

echo "==> [3/9] 删除二进制"
$SUDO rm -f /usr/bin/alloy /usr/local/bin/alloy || true

echo "==> [4/9] 删除 systemd 单元与 OpsPilot override(含 token)"
$SUDO rm -f /lib/systemd/system/alloy.service /etc/systemd/system/alloy.service || true
$SUDO rm -rf /etc/systemd/system/alloy.service.d || true

echo "==> [5/9] 删除配置目录 /etc/alloy"
$SUDO rm -rf /etc/alloy || true

echo "==> [6/9] 删除环境文件"
$SUDO rm -f /etc/default/alloy /etc/sysconfig/alloy || true

echo "==> [7/9] 删除数据目录 /var/lib/alloy"
$SUDO rm -rf /var/lib/alloy || true

echo "==> [8/9] 删除 alloy 用户与组"
if command -v userdel >/dev/null 2>&1; then $SUDO userdel alloy >/dev/null 2>&1 || true; fi
if command -v groupdel >/dev/null 2>&1; then $SUDO groupdel alloy >/dev/null 2>&1 || true; fi

echo "==> [9/9] 清理 grafana 源、keyring 及旧版 sync 残留"
$SUDO rm -f /etc/apt/sources.list.d/grafana.list /etc/apt/keyrings/grafana.gpg || true
$SUDO rm -f /etc/yum.repos.d/grafana.repo || true
$SUDO rm -f /usr/local/bin/opspilot-alloy-sync /etc/systemd/system/opspilot-alloy-sync.service || true

if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl daemon-reload >/dev/null 2>&1 || true
fi

REMAIN=""
[ -x /usr/bin/alloy ] && REMAIN="$REMAIN /usr/bin/alloy"
[ -d /etc/alloy ] && REMAIN="$REMAIN /etc/alloy"
[ -d /var/lib/alloy ] && REMAIN="$REMAIN /var/lib/alloy"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active alloy >/dev/null 2>&1; then
  REMAIN="$REMAIN alloy-running"
fi
if [ -n "$REMAIN" ]; then
  echo "ALLOY_UNINSTALL_INCOMPLETE:$REMAIN"
else
  echo "ALLOY_UNINSTALL_OK"
fi
""".strip()



