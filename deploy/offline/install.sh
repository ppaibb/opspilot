#!/usr/bin/env bash
# OpsPilot 离线一键安装脚本（Linux + Docker Compose v2）
# 用法：
#   ./install.sh                 # 交互填写参数
#   ./install.sh --config env.conf   # 读配置文件非交互
#   ./install.sh --load-images   # 仅导入 images/*.tar
set -euo pipefail
# 本脚本会生成数据库、应用和 Grafana 密钥；默认禁止同组及其他用户读取新文件。
umask 077

ROOT="$(cd "$(dirname "$0")" && pwd)"
IMAGES_DIR="$ROOT/images"
APP_COMPOSE="$ROOT/app/docker-compose.yaml"
MON_COMPOSE="$ROOT/monitoring/docker-compose.yaml"
ENV_FILE="$ROOT/.env"
MANIFEST_FILE="$ROOT/manifest.json"

log()  { printf '\033[1;32m[OpsPilot]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

backup_private_file() {
  local source="$1"
  [[ -f "$source" ]] || return 0
  local backup="${source}.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$source" "$backup"
  chmod 600 "$backup"
  log "已备份配置: $backup"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未安装 docker"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（需 root 或 docker 组）"
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "未找到 docker compose / docker-compose"
  fi
}

load_images() {
  verify_manifest
  log "导入离线镜像 ..."
  local n=0
  shopt -s nullglob
  for tar in "$IMAGES_DIR"/*.tar; do
    log "  load $(basename "$tar")"
    docker load -i "$tar"
    n=$((n+1))
  done
  shopt -u nullglob
  [[ $n -gt 0 ]] || warn "images/ 下没有 .tar，跳过导入"
  verify_loaded_image_ids
  log "镜像导入完成（$n 个）"
}

verify_loaded_image_ids() {
  local ref expected actual
  while IFS='|' read -r ref expected; do
    [[ -n "$ref" ]] || continue
    actual="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null)" || die "manifest 镜像未成功导入: $ref"
    [[ "$actual" == "$expected" ]] || die "manifest 镜像 ID 不匹配: $ref"
  done < <(python3 - "$MANIFEST_FILE" <<'PY'
import json
import pathlib
import sys

for item in json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["images"].values():
    print(f"{item['ref']}|{item['image_id']}")
PY
  )
}

verify_manifest() {
  command -v python3 >/dev/null 2>&1 || die "校验 manifest 需要 python3"
  command -v sha256sum >/dev/null 2>&1 || die "校验离线制品需要 sha256sum"
  [[ -f "$MANIFEST_FILE" ]] || die "缺少制品清单: $MANIFEST_FILE"
  python3 - "$MANIFEST_FILE" "$IMAGES_DIR" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
images_dir = pathlib.Path(sys.argv[2]).resolve()
required = {"backend", "frontend", "postgres"}
missing = required - set(manifest.get("images", {}))
if missing:
    raise SystemExit(f"manifest 缺少镜像: {sorted(missing)}")
for name, item in manifest["images"].items():
    archive = (images_dir / item["archive"]).resolve()
    if images_dir not in archive.parents or not archive.is_file():
        raise SystemExit(f"manifest 制品不存在或路径越界: {name}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != item.get("sha256"):
        raise SystemExit(f"manifest 校验失败: {name}")
PY
}

apply_manifest_images() {
  local assignments key value
  assignments="$(python3 - "$MANIFEST_FILE" <<'PY'
import json
import pathlib
import shlex
import sys

images = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["images"]
mapping = {
    "backend": "OPSPILOT_BACKEND_IMAGE", "frontend": "OPSPILOT_FRONTEND_IMAGE",
    "victoriametrics": "VM_IMAGE", "victorialogs": "VLOGS_IMAGE",
    "grafana": "GRAFANA_IMAGE", "alloy": "ALLOY_IMAGE",
}
for name, variable in mapping.items():
    if name in images:
        print(f"{variable}={images[name]['ref']}")
PY
)"
  while IFS='=' read -r key value; do
    [[ -n "$key" ]] || continue
    printf -v "$key" '%s' "$value"
    export "$key"
    if grep -q "^${key}=" "$ENV_FILE"; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
      printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
  done <<< "$assignments"
  chmod 600 "$ENV_FILE"
}

rand() {
  # 32 字节 hex
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

ask() {
  # ask VAR "提示" "默认值"
  local __var="$1" __prompt="$2" __def="${3:-}"
  local __val=""
  if [[ -n "$__def" ]]; then
    read -r -p "$__prompt [$__def]: " __val || true
    __val="${__val:-$__def}"
  else
    read -r -p "$__prompt: " __val || true
  fi
  printf -v "$__var" '%s' "$__val"
}

ask_secret() {
  local __var="$1" __prompt="$2" __def="${3:-}"
  local __val=""
  if [[ -n "$__def" ]]; then
    read -r -s -p "$__prompt [已设默认]: " __val || true
    echo
    __val="${__val:-$__def}"
  else
    read -r -s -p "$__prompt: " __val || true
    echo
  fi
  printf -v "$__var" '%s' "$__val"
}

gen_env_interactive() {
  local host_ip install_dir frontend_port backend_port
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  host_ip="${host_ip:-127.0.0.1}"

  log "交互填写安装参数（直接回车用方括号默认值）"
  ask install_dir "安装目录" "/opt/opspilot"
  ask host_ip "本机对外访问 IP（前端 URL 用）" "$host_ip"
  ask frontend_port "前端端口" "8080"
  ask backend_port "后端端口" "8001"

  local db_pass secret enc admin_pass grafana_pass
  db_pass="$(rand)"
  secret="$(rand)"
  enc="$(rand)"
  admin_pass="Aa1!$(rand | cut -c1-20)"
  grafana_pass="Aa1!$(rand | cut -c1-20)"

  ask_secret db_pass "PostgreSQL 密码" "$db_pass"
  ask_secret secret "SECRET_KEY" "$secret"
  ask_secret enc "CREDENTIAL_ENCRYPTION_KEY" "$enc"
  ask_secret admin_pass "初始管理员密码" "$admin_pass"
  ask_secret grafana_pass "Grafana 管理员密码" "$grafana_pass"

  local install_mon mon_host mon_bind
  ask install_mon "安装监控栈 (true/false)" "true"
  ask mon_bind "监控端口绑定 (显式私网 IP 跨机 / 127.0.0.1 本机)" "127.0.0.1"
  mon_host="$host_ip"
  if [[ "$install_mon" == "true" ]]; then
    ask mon_host "监控机地址（MONITORING_HOST，同机填本机 IP）" "$host_ip"
  fi

  cat > "$ENV_FILE" <<EOF
# generated by install.sh $(date -Iseconds)
INSTALL_DIR=$install_dir
OPSPILOT_PROJECT=opspilot
PUBLIC_URL=http://${host_ip}:${frontend_port}
FRONTEND_PORT=$frontend_port
BACKEND_PORT=$backend_port

POSTGRES_USER=opspilot
POSTGRES_DB=opspilot
POSTGRES_PASSWORD=$db_pass

SECRET_KEY=$secret
CREDENTIAL_ENCRYPTION_KEY=$enc

INIT_ADMIN_USERNAME=admin
INIT_ADMIN_PASSWORD=$admin_pass
INIT_ADMIN_NAME=系统管理员
INIT_ADMIN_EMAIL=admin@opspilot.local
INIT_ADMIN_ROLE=管理员
INIT_ADMIN_DEPARTMENT=基础平台部

INSTALL_MONITORING=$install_mon
MONITORING_HOST=$mon_host
MONITOR_BIND=$mon_bind
VM_MAX_DISK_BYTES=53687091200
VLOGS_MAX_DISK_BYTES=21474836480
VM_PORT=8428
VLOGS_PORT=9428
GRAFANA_PORT=3000
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=$grafana_pass
GRAFANA_BIND=127.0.0.1
OPSPILOT_MONITORING_SUBNET=172.30.250.0/24
GRAFANA_AUTH_PROXY_WHITELIST=172.30.250.0/24
VM_RETENTION=30d
VLOGS_RETENTION=7d

OPSPILOT_BACKEND_IMAGE=opspilot/backend:1.0.0
OPSPILOT_FRONTEND_IMAGE=opspilot/frontend:1.0.0
VM_IMAGE=victoriametrics/victoria-metrics:v1.140.0
VLOGS_IMAGE=victoriametrics/victoria-logs:v1.50.0
GRAFANA_IMAGE=grafana/grafana:11.6.14-security-04
EOF
  chmod 600 "$ENV_FILE"
  log "已写入 $ENV_FILE"
  # Windows 编辑过的配置常带 CRLF，会污染镜像名导致 docker invalid reference
  tr -d '\r' < "$ENV_FILE" > "$ENV_FILE.lf" && mv "$ENV_FILE.lf" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  # 展示关键项（不打印完整密钥）
  log "访问地址: http://${host_ip}:${frontend_port}"
  log "初始管理员账号: admin（密码仅保存在受限配置文件中）"
}

validate_configuration() {
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  local required=(INSTALL_DIR PUBLIC_URL FRONTEND_PORT BACKEND_PORT POSTGRES_PASSWORD SECRET_KEY CREDENTIAL_ENCRYPTION_KEY INIT_ADMIN_USERNAME INIT_ADMIN_PASSWORD)
  local key
  for key in "${required[@]}"; do
    [[ -n "${!key:-}" ]] || die "必填配置为空: $key"
  done
  [[ ${#POSTGRES_PASSWORD} -ge 24 ]] || die "POSTGRES_PASSWORD 至少 24 个字符"
  [[ ${#INIT_ADMIN_PASSWORD} -ge 24 ]] || die "INIT_ADMIN_PASSWORD 至少 24 个字符"
  [[ "$INIT_ADMIN_PASSWORD" =~ [A-Z] && "$INIT_ADMIN_PASSWORD" =~ [a-z] && "$INIT_ADMIN_PASSWORD" =~ [0-9] && "$INIT_ADMIN_PASSWORD" =~ [^a-zA-Z0-9] ]] \
    || die "INIT_ADMIN_PASSWORD 必须包含大小写字母、数字和符号"
  [[ ${#SECRET_KEY} -ge 32 ]] || die "SECRET_KEY 至少 32 个字符"
  [[ ${#CREDENTIAL_ENCRYPTION_KEY} -ge 32 ]] || die "CREDENTIAL_ENCRYPTION_KEY 至少 32 个字符"
  [[ "$SECRET_KEY" != "$CREDENTIAL_ENCRYPTION_KEY" ]] || die "SECRET_KEY 与 CREDENTIAL_ENCRYPTION_KEY 不能相同"
  if [[ "${INSTALL_MONITORING:-true}" == "true" ]]; then
    [[ -n "${MONITORING_HOST:-}" ]] || die "启用监控时 MONITORING_HOST 必填"
    [[ ${#GRAFANA_ADMIN_PASSWORD} -ge 24 ]] || die "GRAFANA_ADMIN_PASSWORD 至少 24 个字符"
  fi
}

prepare_install_directory() {
  local target current
  target="$(readlink -m "$INSTALL_DIR")"
  current="$(readlink -m "$ROOT")"
  [[ "$target" != "/" ]] || die "INSTALL_DIR 禁止使用根目录"
  case "$target" in
    "$current"/*) die "INSTALL_DIR 不能位于离线包目录内部" ;;
  esac
  if [[ "$target" == "$current" ]]; then
    return 0
  fi
  log "复制离线包到安装目录: $target"
  mkdir -p "$target"
  cp -a "$ROOT/." "$target/"
  chmod 600 "$target/.env"
  exec "$target/install.sh" --config "$target/.env"
}

ensure_private_monitoring_network() {
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  : "${OPSPILOT_MONITORING_SUBNET:?set OPSPILOT_MONITORING_SUBNET}"
  : "${GRAFANA_AUTH_PROXY_WHITELIST:?set GRAFANA_AUTH_PROXY_WHITELIST}"

  if ! docker network inspect opspilot-monitoring-bridge >/dev/null 2>&1; then
    # 固定专用网段使 Grafana 能精确校验 auth.proxy 来源；冲突时立即失败，
    # 由管理员在配置文件中换一个未占用网段后重试。
    docker network create \
      --driver bridge \
      --subnet "$OPSPILOT_MONITORING_SUBNET" \
      opspilot-monitoring-bridge >/dev/null
    log "已创建监控专用网络 opspilot-monitoring-bridge"
  fi
}

write_backend_env() {
  # 应用容器内 .env（compose env_file）
  local dest="$ROOT/app/.env"
  backup_private_file "$dest"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  local vm_url vlogs_url grafana_url
  if [[ "${INSTALL_MONITORING:-true}" == "true" ]]; then
    # 同机安装通过专用 Docker 网络直连，不发布 Grafana 到局域网。
    vm_url="http://victoriametrics:8428"
    vlogs_url="http://victorialogs:9428"
    grafana_url="http://grafana:3000"
  else
    vm_url="${VM_URL:-http://${MONITORING_HOST}:${VM_PORT}}"
    vlogs_url="${VLOGS_URL:-http://${MONITORING_HOST}:${VLOGS_PORT}}"
    grafana_url="${GRAFANA_URL:-http://${MONITORING_HOST}:${GRAFANA_PORT}}"
  fi
  cat > "$dest" <<EOF
SECRET_KEY=$SECRET_KEY
CREDENTIAL_ENCRYPTION_KEY=$CREDENTIAL_ENCRYPTION_KEY
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=${ACCESS_TOKEN_EXPIRE_MINUTES:-60}
REFRESH_TOKEN_EXPIRE_DAYS=${REFRESH_TOKEN_EXPIRE_DAYS:-7}
TRUSTED_PROXY_CIDRS=${TRUSTED_PROXY_CIDRS:-127.0.0.1/32,::1/128}
OUTBOUND_ALLOWED_HOSTS=${OUTBOUND_ALLOWED_HOSTS:-}
OUTBOUND_ALLOWED_CIDRS=${OUTBOUND_ALLOWED_CIDRS:-}
OUTBOUND_TIMEOUT_SECONDS=${OUTBOUND_TIMEOUT_SECONDS:-30}
OUTBOUND_MAX_RESPONSE_BYTES=${OUTBOUND_MAX_RESPONSE_BYTES:-4194304}
OUTBOUND_MAX_REDIRECTS=${OUTBOUND_MAX_REDIRECTS:-3}
DATABASE_URL=postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB
INIT_ADMIN_USERNAME=$INIT_ADMIN_USERNAME
INIT_ADMIN_PASSWORD=$INIT_ADMIN_PASSWORD
INIT_ADMIN_NAME=$INIT_ADMIN_NAME
INIT_ADMIN_EMAIL=$INIT_ADMIN_EMAIL
INIT_ADMIN_ROLE=$INIT_ADMIN_ROLE
INIT_ADMIN_DEPARTMENT=$INIT_ADMIN_DEPARTMENT
MONITORING_HOST=$MONITORING_HOST
# 后端自注册本机资产用它当主机名（容器内 gethostname 是容器 ID）
HOST_NAME=${HOST_NAME:-$(hostname)}
DB_AUTO_CREATE_TABLES=0
TERMINAL_SFTP_UPLOAD_MAX_MB=100
# 监控连接三件套：后端启动时 bootstrap 写入 SystemConfig（已配置则不覆盖），
# 装了监控栈就一并给上，省得再去设置页手填
VM_URL=$vm_url
VLOGS_URL=$vlogs_url
GRAFANA_URL=$grafana_url
EOF
  chmod 600 "$dest"
  log "已生成 $dest"
}

wait_http() {
  local url="$1" name="$2" tries="${3:-60}"
  for i in $(seq 1 "$tries"); do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
      log "$name 就绪: $url"
      return 0
    fi
    sleep 2
  done
  warn "$name 等待超时: $url"
  return 1
}

start_app() {
  log "启动应用栈 (db + init + backend + frontend) ..."
  set -a; source "$ENV_FILE"; set +a
  "${COMPOSE[@]}" -f "$APP_COMPOSE" --env-file "$ENV_FILE" up -d
  wait_http "http://127.0.0.1:${FRONTEND_PORT}/" "前端" 40
  wait_http "http://127.0.0.1:${BACKEND_PORT}/api/settings/login-page" "后端" 60
  # init 容器日志
  "${COMPOSE[@]}" -f "$APP_COMPOSE" --env-file "$ENV_FILE" logs --no-color init-db | tail -n 30 || true
}

start_monitoring() {
  log "启动监控栈 ..."
  set -a; source "$ENV_FILE"; set +a
  # VM_URL 等给后端用
  if [[ -z "${VM_URL:-}" ]]; then
    export VM_URL="http://${MONITORING_HOST}:${VM_PORT}"
  fi
  if [[ -z "${VLOGS_URL:-}" ]]; then
    export VLOGS_URL="http://${MONITORING_HOST}:${VLOGS_PORT}"
  fi
  # Alloy 内置采集：HOST_INSTANCE 对齐 MONITORING_HOST（须与资产表本机 IP 一致）
  if [[ -z "${MONITORING_HOST:-}" ]]; then
    MONITORING_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
    export MONITORING_HOST="${MONITORING_HOST:-127.0.0.1}"
  fi
  if [[ -z "${HOST_NAME:-}" ]]; then
    export HOST_NAME="$(hostname 2>/dev/null || echo opspilot)"
  fi
  # HOST_NAME 持久化进 .env：alloy 容器用它打 host 标签（日志页「主机」筛选的来源）。
  # 只靠 shell 导出的话，后续任何一次手工 `docker compose up -d`（不带导出）都会把它
  # 丢成空串——235 演练实际踩过，host 标签整体消失。
  if ! grep -q '^HOST_NAME=' "$ENV_FILE" 2>/dev/null; then
    printf 'HOST_NAME=%s\n' "$HOST_NAME" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
  "${COMPOSE[@]}" -f "$MON_COMPOSE" --env-file "$ENV_FILE" up -d
  wait_http "http://127.0.0.1:${VM_PORT}/-/healthy" "VictoriaMetrics" 40
  wait_http "http://127.0.0.1:${VLOGS_PORT}/-/healthy" "VictoriaLogs" 40
  wait_http "http://127.0.0.1:${GRAFANA_PORT}/api/health" "Grafana" 60
}

summary() {
  set -a; source "$ENV_FILE"; set +a
  echo
  log "========== 安装完成 =========="
  log "前端:        ${PUBLIC_URL}"
  log "后端 API:    http://127.0.0.1:${BACKEND_PORT}"
  log "初始管理员账号: ${INIT_ADMIN_USERNAME}（密码仅保存在受限配置文件中）"
  if [[ "${INSTALL_MONITORING}" == "true" ]]; then
    log "VM:          http://${MONITORING_HOST}:${VM_PORT}"
    log "VLogs:       http://${MONITORING_HOST}:${VLOGS_PORT}"
    log "Grafana SSO: 请从 OpsPilot 监控页进入（裸端口默认不对局域网开放）"
    log "下一步: 登录后 系统设置→监控连接，确认 VM/VLogs/Grafana 并点测试连接"
  fi
  log "配置文件:    $ENV_FILE"
  log "数据卷:      ${OPSPILOT_PROJECT}-pgdata 等"
  echo
}

verify_service_matrix() {
  local required=(opspilot-db opspilot-backend opspilot-frontend)
  if [[ "${INSTALL_MONITORING:-true}" == "true" ]]; then
    required+=(opspilot-victoriametrics opspilot-victorialogs opspilot-grafana opspilot-alloy)
  fi
  log "最终服务矩阵:"
  local container status
  for container in "${required[@]}"; do
    status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null)" \
      || die "必需容器不存在: $container"
    printf '  %-30s %s\n' "$container" "$status"
    [[ "$status" == "running" ]] || die "必需容器未运行: $container ($status)"
  done
}

main() {
  local mode="install" config=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --load-images) mode="load"; shift ;;
      --config) config="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: $0 [--config env.conf] [--load-images]"; exit 0 ;;
      *) die "未知参数 $1" ;;
    esac
  done

  if [[ "$mode" == "load" ]]; then
    require_docker
    load_images
    exit 0
  fi

  if [[ -n "$config" ]]; then
    [[ -f "$config" ]] || die "配置不存在: $config"
    local normalized
    normalized="$(mktemp)"
    tr -d '\r' < "$config" > "$normalized"
    backup_private_file "$ENV_FILE"
    mv "$normalized" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    log "已使用配置 $config"
  elif [[ -f "$ENV_FILE" ]]; then
    tr -d '\r' < "$ENV_FILE" > "$ENV_FILE.lf" && mv "$ENV_FILE.lf" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    log "发现已有 $ENV_FILE，直接使用（删掉可重新交互）"
  else
    gen_env_interactive
  fi

  validate_configuration
  prepare_install_directory
  require_docker
  load_images
  apply_manifest_images
  ensure_private_monitoring_network
  write_backend_env
  start_monitoring_if_needed
  start_app
  verify_service_matrix
  summary
}

start_monitoring_if_needed() {
  set -a; source "$ENV_FILE"; set +a
  if [[ "${INSTALL_MONITORING:-true}" == "true" ]]; then
    start_monitoring
  else
    log "INSTALL_MONITORING=false，跳过监控栈"
  fi
}

main "$@"
