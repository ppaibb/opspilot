"""Alloy Agent bootstrap 与安装脚本渲染工具。

主机侧只保留 Alloy 原生 `import.http` bootstrap。真实采集配置由 OpsPilot
远程 module 接口生成，避免再维护 `opspilot-alloy-sync` 常驻脚本。
"""
from __future__ import annotations

import gzip
import json
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path


SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]{1,160}$")

# 局域网二进制快速通道：后端镜像/主机若自带 alloy 二进制，安装脚本优先经
# OpsPilot 拉取 gzip 二进制（秒级），仅在架构不符 / 镜像无二进制时回退到
# apt/yum（从 Grafana 源拉包，国内出口极慢）。
# 仓库内分发位：deploy/monitoring/agents/alloy-linux-amd64（Windows 后端也能
# 从这里给局域网机器分发二进制，避免回退到极慢的公网 apt 源）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALLOY_BINARY_CANDIDATES = (
    "/usr/bin/alloy",
    "/usr/local/bin/alloy",
    str(_REPO_ROOT / "deploy" / "monitoring" / "agents" / "alloy-linux-amd64"),
)
_ALLOY_BINARY_GZIP_CACHE = os.path.join(tempfile.gettempdir(), "opspilot-alloy-binary.gz")


def _normalize_arch(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("amd64", "x86_64", "x64"):
        return "amd64"
    if v in ("arm64", "aarch64"):
        return "arm64"
    return v


def local_alloy_arch() -> str:
    """本机（后端运行环境）的 Alloy 二进制架构。"""
    return _normalize_arch(platform.machine())


def local_alloy_binary_path() -> str | None:
    """定位本机可执行的 alloy 二进制；容器化后端可能没有，返回 None。"""
    for p in _ALLOY_BINARY_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("alloy")


def resolve_alloy_binary_for_arch(arch: str) -> str | None:
    """请求架构与本机一致且本机有二进制时返回路径，否则 None（触发脚本回退）。"""
    want = _normalize_arch(arch)
    if want and want != local_alloy_arch():
        return None
    return local_alloy_binary_path()


def build_alloy_binary_gzip(path: str) -> str:
    """把二进制 gzip 到缓存文件；源文件更新时重建，避免每次请求压缩 ~400MB。"""
    try:
        src_mtime = os.path.getmtime(path)
        if (
            os.path.isfile(_ALLOY_BINARY_GZIP_CACHE)
            and os.path.getmtime(_ALLOY_BINARY_GZIP_CACHE) >= src_mtime
            and os.path.getsize(_ALLOY_BINARY_GZIP_CACHE) > 0
        ):
            return _ALLOY_BINARY_GZIP_CACHE
    except OSError:
        pass
    tmp = _ALLOY_BINARY_GZIP_CACHE + ".tmp"
    with open(path, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    os.replace(tmp, _ALLOY_BINARY_GZIP_CACHE)
    return _ALLOY_BINARY_GZIP_CACHE


def render_alloy_base_systemd_unit() -> str:
    """生成 Alloy systemd 主单元。

    二进制安装路径用：deb/rpm 包会自带该单元，纯二进制安装没有，需要补齐。
    OpsPilot override（opspilot.conf）会再把 User 改成 root 并注入 Token。
    EnvironmentFile 用 `-` 前缀表示可选，兼容 Debian(/etc/default) 与 RHEL(/etc/sysconfig)。
    """
    return "\n".join(
        [
            "[Unit]",
            "Description=Vendor-agnostic OpenTelemetry Collector distribution with programmable pipelines",
            "Documentation=https://grafana.com/docs/alloy",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Restart=always",
            "User=alloy",
            "Environment=HOSTNAME=%H",
            "EnvironmentFile=-/etc/default/alloy",
            "EnvironmentFile=-/etc/sysconfig/alloy",
            "WorkingDirectory=/var/lib/alloy",
            "ExecStart=/usr/bin/alloy run $CUSTOM_ARGS --storage.path=/var/lib/alloy/data $CONFIG_FILE",
            "ExecReload=/usr/bin/env kill -HUP $MAINPID",
            "TimeoutStopSec=20s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def render_alloy_bootstrap_config(
    *,
    base_url: str = "__OPSPILOT_BASE_URL__",
    poll_frequency: str = "__OPSPILOT_POLL_FREQUENCY__",
) -> str:
    """生成 Alloy 远程配置 bootstrap。

    Token 从 systemd 环境变量读取，不直接写入 `/etc/alloy/config.alloy`。
    """
    safe_base_url = _validate_http_url(base_url).rstrip("/") if not base_url.startswith("__") else base_url
    safe_poll = str(poll_frequency or "60s").strip()
    return "\n".join(
        [
            "// OpsPilot Alloy bootstrap 配置。真实采集逻辑由远程 module 下发。",
            "logging {",
            '  level  = "info"',
            '  format = "logfmt"',
            "}",
            "",
            'import.http "opspilot" {',
            f"  url = {_shell_quote(f'{safe_base_url}/api/monitoring/collector/agent/module')}",
            f"  poll_frequency = {_shell_quote(safe_poll)}",
            "  headers = {",
            '    "X-OpsPilot-Agent-Id" = sys.env("OPSPILOT_COLLECTOR_ID"),',
            '    "X-OpsPilot-Agent-Token" = sys.env("OPSPILOT_COLLECTOR_TOKEN"),',
            "  }",
            "}",
            "",
            'opspilot.main "default" {}',
            "",
        ]
    )


def render_alloy_default_env() -> str:
    """生成 Debian/Ubuntu 包使用的 Alloy 环境配置。"""
    return "\n".join(
        [
            "# OpsPilot 托管 Alloy 启动参数。",
            "# HTTP 调试端口只监听本机，并避开监控套件容器默认的 12345 端口。",
            'CONFIG_FILE="/etc/alloy/config.alloy"',
            'CUSTOM_ARGS="--server.http.listen-addr=127.0.0.1:12346 --disable-reporting=true"',
            "",
        ]
    )


def render_alloy_systemd_override() -> str:
    """生成 Alloy systemd override。"""
    return "\n".join(
        [
            "[Service]",
            "User=root",
            "Group=root",
            'Environment="OPSPILOT_COLLECTOR_ID=__AGENT_ID__"',
            'Environment="OPSPILOT_COLLECTOR_TOKEN=__AGENT_TOKEN__"',
            "",
        ]
    )


def render_alloy_install_script() -> str:
    """生成 Linux Agent 安装脚本，注册后写入 import.http bootstrap。"""
    return f"""#!/usr/bin/env bash
set -euo pipefail

# OpsPilot Alloy Agent 安装脚本。
# 使用方式：
#   OPSPILOT_BASE_URL="http://opspilot.example.com" OPSPILOT_REGISTER_TOKEN="..." bash install.sh
#
# 日志采集前置条件（产品决策：只采 Docker 容器日志，不采 journald/syslog）：
#   1. 目标机需运行 Docker，且 /var/run/docker.sock 可读（本安装以 root 运行 Alloy，默认可读）；
#   2. 若 Alloy 跑在容器里，必须挂载 -v /var/run/docker.sock:/var/run/docker.sock:ro；
#   3. 真实采集配置由 OpsPilot 远程 module 下发（loki.source.docker），
#      与 deploy/monitoring/config/alloy.alloy 模板保持一致。
if [ -z "${{OPSPILOT_BASE_URL:-}}" ]; then
  echo "OPSPILOT_BASE_URL 不能为空" >&2
  exit 1
fi
if [ -z "${{OPSPILOT_REGISTER_TOKEN:-}}" ]; then
  echo "OPSPILOT_REGISTER_TOKEN 不能为空" >&2
  exit 1
fi
OPSPILOT_ASSET_ID="${{OPSPILOT_ASSET_ID:-}}"
OPSPILOT_POLL_FREQUENCY="${{OPSPILOT_POLL_FREQUENCY:-60s}}"

if [ "$(id -u)" != "0" ]; then
  echo "请使用 root 或 sudo 执行安装脚本" >&2
  exit 1
fi

HOSTNAME_VALUE="$(hostname 2>/dev/null || echo unknown)"
IP_VALUE="$(hostname -I 2>/dev/null | awk '{{print $1}}')"

REGISTER_JSON="$(python3 - "$OPSPILOT_REGISTER_TOKEN" "$HOSTNAME_VALUE" "$IP_VALUE" "$OPSPILOT_ASSET_ID" "${{OPSPILOT_BASE_URL%/}}/api/monitoring/collector/agent/register" <<'PY'
import json
import sys
import urllib.request

payload = {{
    "register_token": sys.argv[1],
    "hostname": sys.argv[2],
    "ip": sys.argv[3],
    "os_type": "linux",
    "asset_id": sys.argv[4],
    "labels": {{"env": "prod", "host": sys.argv[2], "asset_id": sys.argv[4]}}
}}
req = urllib.request.Request(
    sys.argv[5],
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={{"Content-Type": "application/json"}},
    method="POST"
)
try:
    with urllib.request.urlopen(req) as response:
        print(response.read().decode("utf-8"))
except Exception as e:
    print(f"Error during registration: {{e}}", file=sys.stderr)
    sys.exit(1)
PY
)"

read -r AGENT_ID AGENT_TOKEN <<EOF
$(python3 - <<'PY' "$REGISTER_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload.get("agent_id", ""), payload.get("agent_token", ""))
PY
)
EOF

if [ -z "$AGENT_ID" ] || [ -z "$AGENT_TOKEN" ]; then
  echo "Agent 注册失败：响应中缺少 agent_id 或 agent_token" >&2
  exit 1
fi

# ---------- 安装 Alloy ----------
# 策略：优先经 OpsPilot 局域网拉取预编译二进制（秒级，glibc 全发行版通用），
# 失败再回退 apt/yum（从 Grafana 源拉包，国内出口极慢）。
ALLOY_READY=0
if command -v alloy >/dev/null 2>&1; then
  ALLOY_READY=1
fi

if [ "$ALLOY_READY" != "1" ]; then
  ARCH_RAW="$(uname -m 2>/dev/null || echo unknown)"
  case "$ARCH_RAW" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) ARCH="$ARCH_RAW" ;;
  esac
  BIN_URL="${{OPSPILOT_BASE_URL%/}}/api/monitoring/collector/agent/binary?arch=${{ARCH}}"
  echo "尝试从 OpsPilot 拉取 Alloy 二进制（${{ARCH}}）..."
  if curl -fsSL "$BIN_URL" -o /tmp/opspilot-alloy.gz 2>/dev/null && [ -s /tmp/opspilot-alloy.gz ]; then
    if gzip -dc /tmp/opspilot-alloy.gz > /tmp/opspilot-alloy.bin 2>/dev/null && head -c 4 /tmp/opspilot-alloy.bin | grep -q $'\\x7fELF'; then
      install -m 0755 /tmp/opspilot-alloy.bin /usr/bin/alloy
      rm -f /tmp/opspilot-alloy.gz /tmp/opspilot-alloy.bin
      # 复刻 deb/rpm 包的附带物：alloy 用户/组、数据目录、systemd 主单元。
      if ! getent group alloy >/dev/null 2>&1; then groupadd -r alloy || true; fi
      if ! getent passwd alloy >/dev/null 2>&1; then
        useradd -m -r -g alloy -d /var/lib/alloy -s /sbin/nologin -c "alloy user" alloy || true
      fi
      getent group adm >/dev/null 2>&1 && usermod -a -G adm alloy || true
      getent group systemd-journal >/dev/null 2>&1 && usermod -a -G systemd-journal alloy || true
      mkdir -p /var/lib/alloy/data
      chown -R alloy:alloy /var/lib/alloy 2>/dev/null || true
      chmod 0770 /var/lib/alloy /var/lib/alloy/data 2>/dev/null || true
      if [ ! -f /lib/systemd/system/alloy.service ] && [ ! -f /etc/systemd/system/alloy.service ]; then
        mkdir -p /lib/systemd/system
        cat >/lib/systemd/system/alloy.service <<'OPSPILOT_ALLOY_UNIT_EOF'
{render_alloy_base_systemd_unit().rstrip()}
OPSPILOT_ALLOY_UNIT_EOF
      fi
      if command -v alloy >/dev/null 2>&1; then
        ALLOY_READY=1
        echo "已通过 OpsPilot 二进制安装 Alloy。"
      fi
    else
      echo "二进制校验失败，回退到软件包安装。" >&2
      rm -f /tmp/opspilot-alloy.gz /tmp/opspilot-alloy.bin
    fi
  else
    echo "未获取到 OpsPilot 二进制（架构不符或后端无二进制），回退到软件包安装。" >&2
  fi
fi

if [ "$ALLOY_READY" != "1" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y ca-certificates curl gpg
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    chmod 0644 /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" >/etc/apt/sources.list.d/grafana.list
    apt-get update -y
    apt-get install -y alloy
  elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    PM="$(command -v dnf || command -v yum)"
    cat >/etc/yum.repos.d/grafana.repo <<'EOF'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOF
    "$PM" install -y alloy
  else
    echo "未找到受支持的软件包管理器，无法自动安装 Alloy" >&2
    exit 1
  fi
fi

if ! command -v alloy >/dev/null 2>&1; then
  echo "Alloy 安装后仍不可用" >&2
  exit 1
fi

mkdir -p /etc/alloy
cat >/etc/alloy/config.alloy <<'OPSPILOT_CONFIG_EOF'
{render_alloy_bootstrap_config().rstrip()}
OPSPILOT_CONFIG_EOF
python3 - "$OPSPILOT_BASE_URL" "$OPSPILOT_POLL_FREQUENCY" /etc/alloy/config.alloy <<'PY'
import json
import sys
from pathlib import Path

base_url = sys.argv[1].rstrip("/")
poll_frequency = sys.argv[2]
path = Path(sys.argv[3])
content = path.read_text(encoding="utf-8")
content = content.replace('"__OPSPILOT_BASE_URL__/api/monitoring/collector/agent/module"', json.dumps(f"{{base_url}}/api/monitoring/collector/agent/module"))
content = content.replace('"__OPSPILOT_POLL_FREQUENCY__"', json.dumps(poll_frequency))
path.write_text(content, encoding="utf-8")
PY
chown -R root:root /etc/alloy
find /etc/alloy -type d -exec chmod 755 {{}} \\;
find /etc/alloy -type f -exec chmod 644 {{}} \\;

if [ -d /etc/default ]; then
  cat >/etc/default/alloy <<'OPSPILOT_ALLOY_ENV_EOF'
{render_alloy_default_env().rstrip()}
OPSPILOT_ALLOY_ENV_EOF
fi

if [ -d /etc/sysconfig ]; then
  cat >/etc/sysconfig/alloy <<'OPSPILOT_ALLOY_ENV_EOF'
{render_alloy_default_env().rstrip()}
OPSPILOT_ALLOY_ENV_EOF
fi

mkdir -p /etc/systemd/system/alloy.service.d
cat >/etc/systemd/system/alloy.service.d/opspilot.conf <<'OPSPILOT_ALLOY_OVERRIDE_EOF'
{render_alloy_systemd_override().rstrip()}
OPSPILOT_ALLOY_OVERRIDE_EOF
python3 - "$AGENT_ID" "$AGENT_TOKEN" /etc/systemd/system/alloy.service.d/opspilot.conf <<'PY'
import sys
from pathlib import Path

agent_id = sys.argv[1]
agent_token = sys.argv[2]
path = Path(sys.argv[3])
content = path.read_text(encoding="utf-8")
content = content.replace("__AGENT_ID__", agent_id)
content = content.replace("__AGENT_TOKEN__", agent_token.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"'))
path.write_text(content, encoding="utf-8")
PY
chmod 600 /etc/systemd/system/alloy.service.d/opspilot.conf
rm -f /usr/local/bin/opspilot-alloy-sync /etc/systemd/system/opspilot-alloy-sync.service

systemctl daemon-reload
systemctl disable --now opspilot-alloy-sync.service >/dev/null 2>&1 || true
systemctl enable alloy >/dev/null
systemctl reset-failed alloy >/dev/null 2>&1 || true
systemctl restart alloy

echo "OpsPilot Alloy Agent 已注册并启动：${{AGENT_ID}}"
echo "日志采集：Docker 容器日志（loki.source.docker）。请确认 /var/run/docker.sock 可读。"
"""


def _validate_http_url(value: str) -> str:
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("OpsPilot base_url 必须是 HTTP(S) 地址")
    return url


def _validate_header_value(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID_PATTERN.match(text):
        raise ValueError(f"{field_name} 包含不安全字符")
    return text


def _shell_quote(value: str) -> str:
    return json.dumps(value)
