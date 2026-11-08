#!/usr/bin/env bash
# OpsPilot 一键增量部署：开发机构建镜像 → 目标机 load + 重启 + 健康检查
#
# 用法（仓库根或任意目录执行）：
#   bash deploy/update/deploy_update.sh --host root@10.10.0.9
#   bash deploy/update/deploy_update.sh --host root@10.10.0.9 --frontend-only
#   bash deploy/update/deploy_update.sh --host root@10.10.0.9 --backend-only
#   bash deploy/update/deploy_update.sh --host root@10.10.0.9 --backend-tag 1.0.7 --frontend-tag 1.0.13
#   bash deploy/update/deploy_update.sh --host root@10.10.0.9 --check-only
#
# 默认：前后端都打新 tag（在目标机当前 tag 上递增 patch），构建→传输→同步 compose→改 .env→重启→验证。
# 需要：本机 docker + ssh/scp 免密；目标机 docker compose、应用目录可写。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST=""
BACKEND_TAG=""
FRONTEND_TAG=""
DO_BACKEND=1
DO_FRONTEND=1
CHECK_ONLY=0
STAGE_DIR="/opt/opspilot-src/opspilot-offline-stage"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
# 后端镜像 ~1GB，scp 容易超时，用 docker save | ssh docker load 流式
BACKEND_TRANSFER="stream"   # stream | scp

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  # 只打印文件头注释（到第一个非 # 行为止）
  sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --backend-tag) BACKEND_TAG="${2:-}"; shift 2 ;;
    --frontend-tag) FRONTEND_TAG="${2:-}"; shift 2 ;;
    --backend-only) DO_FRONTEND=0; shift ;;
    --frontend-only) DO_BACKEND=0; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --stage-dir) STAGE_DIR="${2:-}"; shift 2 ;;
    --backend-scp) BACKEND_TRANSFER="scp"; shift ;;
    -h|--help) usage 0 ;;
    *) die "未知参数: $1（--help 看用法）" ;;
  esac
done

[[ -n "$HOST" ]] || die "必须指定 --host，例如 --host root@10.10.0.9"
[[ $DO_BACKEND -eq 1 || $DO_FRONTEND -eq 1 ]] || die "不能同时 --backend-only 与 --frontend-only"

# ── 前置检查 ──────────────────────────────────────────────
command -v docker >/dev/null || die "本机未安装 docker"
command -v ssh >/dev/null || die "本机未安装 ssh"
command -v scp >/dev/null || die "本机未安装 scp"
docker info >/dev/null 2>&1 || die "docker 守护进程不可用"

log "探测目标机 $HOST ..."
ssh "${SSH_OPTS[@]}" "$HOST" "test -d '$STAGE_DIR' && test -f '$STAGE_DIR/.env' && test -f '$STAGE_DIR/app/docker-compose.yaml'" \
  || die "目标机缺少 $STAGE_DIR（请先离线安装 deploy/offline，或用 --stage-dir 指定）"

# 读目标机当前镜像 tag
CUR_BACKEND="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -E '^OPSPILOT_BACKEND_IMAGE=' '$STAGE_DIR/.env' | tail -1 | cut -d= -f2" | tr -d '\r')"
CUR_FRONTEND="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -E '^OPSPILOT_FRONTEND_IMAGE=' '$STAGE_DIR/.env' | tail -1 | cut -d= -f2" | tr -d '\r')"
CUR_BACKEND="${CUR_BACKEND:-opspilot/backend:1.0.0}"
CUR_FRONTEND="${CUR_FRONTEND:-opspilot/frontend:1.0.0}"
log "目标机当前: $CUR_BACKEND / $CUR_FRONTEND"

bump_patch() {
  # opspilot/backend:1.0.6 → opspilot/backend:1.0.7
  local img="$1"
  local repo="${img%:*}"
  local tag="${img##*:}"
  local base="${tag%%-*}"          # 去掉 -rc 等后缀
  IFS=. read -r maj min pat <<<"$base"
  printf '%s:%d.%d.%d' "$repo" "${maj:-1}" "${min:-0}" "$(( ${pat:-0} + 1 ))"
}

if [[ $DO_BACKEND -eq 1 && -z "$BACKEND_TAG" ]]; then
  BACKEND_TAG="$(bump_patch "$CUR_BACKEND")"
fi
if [[ $DO_FRONTEND -eq 1 && -z "$FRONTEND_TAG" ]]; then
  FRONTEND_TAG="$(bump_patch "$CUR_FRONTEND")"
fi

BACKEND_IMG="opspilot/backend:${BACKEND_TAG}"
FRONTEND_IMG="opspilot/frontend:${FRONTEND_TAG}"

if [[ $CHECK_ONLY -eq 1 ]]; then
  log "仅健康检查 ..."
  ssh "${SSH_OPTS[@]}" "$HOST" "curl -fsS -m 8 -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1:8080/ || true
curl -fsS -m 8 -o /dev/null -w 'login-page %{http_code}\n' http://127.0.0.1:8001/api/settings/login-page || true
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep opspilot || true"
  exit 0
fi

log "计划部署:"
[[ $DO_BACKEND -eq 1 ]] && log "  后端 $BACKEND_IMG"
[[ $DO_FRONTEND -eq 1 ]] && log "  前端 $FRONTEND_IMG"

# ── 构建 ──────────────────────────────────────────────────
build_image() {
  local tag="$1" dir="$2"
  log "构建 $tag ..."
  docker build -t "$tag" "$dir"
}

[[ $DO_BACKEND -eq 1 ]] && build_image "$BACKEND_IMG" "$ROOT/backend"
[[ $DO_FRONTEND -eq 1 ]] && build_image "$FRONTEND_IMG" "$ROOT/frontend"

# ── 传输 + 加载 ───────────────────────────────────────────
push_image() {
  local tag="$1" kind="$2"   # kind: backend|frontend
  log "传输 $tag → $HOST ..."
  if [[ "$kind" == "backend" && "$BACKEND_TRANSFER" == "stream" ]]; then
    # 1GB 级：流式 save|load，不经磁盘、不怕 scp 超时
    docker save "$tag" | ssh "${SSH_OPTS[@]}" "$HOST" "docker load"
    return
  fi
  local name
  name="$(echo "$tag" | tr '/:' '__').tar"
  local tmp="${TMPDIR:-/tmp}/$name"
  docker save -o "$tmp" "$tag"
  scp "${SSH_OPTS[@]}" "$tmp" "$HOST:/tmp/$name"
  ssh "${SSH_OPTS[@]}" "$HOST" "docker load -i /tmp/$name && rm -f /tmp/$name"
  rm -f "$tmp"
}

[[ $DO_BACKEND -eq 1 ]] && push_image "$BACKEND_IMG" backend
[[ $DO_FRONTEND -eq 1 ]] && push_image "$FRONTEND_IMG" frontend

# ── 同步 compose 文件 ─────────────────────────────────────
# 目标机只备份旧 compose（在远端脚本里做），这里先传成 .new，远端备份后再 mv 到位，
# 避免先覆盖导致回滚时无旧文件可恢复。
log "同步 compose 文件 → $HOST ..."
scp "${SSH_OPTS[@]}" "$ROOT/deploy/offline/app/docker-compose.yaml" "$HOST:$STAGE_DIR/app/docker-compose.yaml.new"
if ssh "${SSH_OPTS[@]}" "$HOST" "test -f '$STAGE_DIR/monitoring/docker-compose.yaml'"; then
  scp "${SSH_OPTS[@]}" "$ROOT/deploy/offline/monitoring/docker-compose.yaml" "$HOST:$STAGE_DIR/monitoring/docker-compose.yaml.new"
else
  log "目标机未安装监控栈，跳过 monitoring compose 同步"
fi

# ── 改 .env + 重启 ────────────────────────────────────────
SERVICES=""
[[ $DO_BACKEND -eq 1 ]] && SERVICES+=" backend"
[[ $DO_FRONTEND -eq 1 ]] && SERVICES+=" frontend"
SERVICES="${SERVICES# }"

log "更新 $STAGE_DIR/.env 并重启 ($SERVICES) ..."

remote_script() {
  cat <<REMOTE
set -eu
export PATH="\$PATH:/usr/libexec/docker/cli-plugins:/usr/lib/docker/cli-plugins:/usr/local/lib/docker/cli-plugins"
cd '$STAGE_DIR'
if docker compose version >/dev/null 2>&1; then C='docker compose'; else C='docker-compose'; fi
COMPOSE="\$C -f app/docker-compose.yaml --env-file .env"
COMPOSE_MONITORING="\$C -f monitoring/docker-compose.yaml --env-file .env"
BACKUP_ENV='.env.pre-update'
cp -p .env "\$BACKUP_ENV"
chmod 600 "\$BACKUP_ENV"
# 旧 compose 一并备份，回滚时连同 .env 一起恢复（新 compose 配旧镜像跑不起来，
# 如前端端口 8080:8080 对只听 80 的旧前端镜像）。
cp -p app/docker-compose.yaml app/docker-compose.yaml.pre-update
if [[ -f monitoring/docker-compose.yaml ]]; then
  cp -p monitoring/docker-compose.yaml monitoring/docker-compose.yaml.pre-update
fi

rollback_deploy() {
  status=\$?
  trap - ERR
  echo '[rollback] 迁移或健康检查失败，恢复旧镜像配置' >&2
  cp -p "\$BACKUP_ENV" .env
  [[ -f app/docker-compose.yaml.pre-update ]] && cp -p app/docker-compose.yaml.pre-update app/docker-compose.yaml
  [[ -f monitoring/docker-compose.yaml.pre-update ]] && cp -p monitoring/docker-compose.yaml.pre-update monitoring/docker-compose.yaml
  set -a; . ./.env; set +a
  # 回滚时绕开 init-db：DB 已迁到新 revision，旧镜像的 alembic 不认识它，
  # 走正常 up -d 会让 init-db exit 255，backend/frontend 停在 Created。
  \$COMPOSE up -d --no-deps backend frontend || true
  if [[ -f monitoring/docker-compose.yaml ]]; then
    \$COMPOSE_MONITORING up -d || true
  fi
  exit "\$status"
}
trap rollback_deploy ERR

# 同步 compose（本地已传成 .new；旧文件已在上面备份）
if [[ -f app/docker-compose.yaml.new ]]; then
  mv app/docker-compose.yaml.new app/docker-compose.yaml
fi
if [[ -f monitoring/docker-compose.yaml.new ]]; then
  mv monitoring/docker-compose.yaml.new monitoring/docker-compose.yaml
fi
\$COMPOSE config --quiet
if [[ -f monitoring/docker-compose.yaml ]]; then
  \$COMPOSE_MONITORING config --quiet
fi

$([[ $DO_BACKEND -eq 1 ]] && echo "sed -i 's|^OPSPILOT_BACKEND_IMAGE=.*|OPSPILOT_BACKEND_IMAGE=${BACKEND_IMG}|' .env")
$([[ $DO_FRONTEND -eq 1 ]] && echo "sed -i 's|^OPSPILOT_FRONTEND_IMAGE=.*|OPSPILOT_FRONTEND_IMAGE=${FRONTEND_IMG}|' .env")
set -a; . ./.env; set +a

# 补新必填变量（旧 .env 没有这两项时监控 compose 的 :? 校验会直接报错）
ensure_env() {
  local key="\$1" value="\$2"
  if ! grep -q "^\${key}=" .env; then
    printf '%s=%s\n' "\$key" "\$value" >> .env
    echo "[env] 补 \${key}=\${value}"
  fi
}
ensure_env GRAFANA_AUTH_PROXY_WHITELIST 172.30.250.0/24
ensure_env OPSPILOT_MONITORING_SUBNET 172.30.250.0/24

# 监控镜像变量跟随新 compose 的默认值走：旧 .env 里钉死的旧版（如 Grafana 11.0.0）
# 会覆盖 compose 默认值，导致监控安全加固到不了服务器。
sync_monitoring_image() {
  local key="\$1"
  local def
  def="\$(grep -m1 -o '\${'"\$key"':-[^}]*}' monitoring/docker-compose.yaml | sed 's/^.*:-//; s/}$//')"
  [[ -n "\$def" ]] || return 0
  if grep -q "^\${key}=" .env; then
    sed -i "s|^\${key}=.*|\${key}=\${def}|" .env
  else
    printf '%s=%s\n' "\$key" "\$def" >> .env
  fi
  echo "[env] \${key}=\${def}"
}
if [[ -f monitoring/docker-compose.yaml ]]; then
  for _key in VM_IMAGE VLOGS_IMAGE GRAFANA_IMAGE ALLOY_IMAGE; do
    sync_monitoring_image "\$_key"
  done
fi
set -a; . ./.env; set +a

# 监控专用网络（不存在才建；网段来自 .env）
if [[ -f monitoring/docker-compose.yaml ]] && ! docker network inspect opspilot-monitoring-bridge >/dev/null 2>&1; then
  docker network create --driver bridge --subnet "\${OPSPILOT_MONITORING_SUBNET:-172.30.250.0/24}" opspilot-monitoring-bridge >/dev/null
  echo '[network] 已创建监控专用网络 opspilot-monitoring-bridge'
fi

# 旧版后端以 root 运行，数据卷属 root；新版以后端 opspilot（999:999）运行，
# 卷属主不对会 PermissionError crash-loop。卷已存在且属主不对时才 chown。
for _vol in "\${OPSPILOT_PROJECT:-opspilot}-backend-storage" "\${OPSPILOT_PROJECT:-opspilot}-backend-logs"; do
  if docker volume inspect "\$_vol" >/dev/null 2>&1; then
    _owner="\$(docker run --rm --entrypoint stat -v "\$_vol:/data" "\${OPSPILOT_BACKEND_IMAGE}" -c '%u:%g' /data 2>/dev/null || echo unknown)"
    if [[ "\$_owner" != '999:999' ]]; then
      echo "[chown] \$_vol 属主为 \$_owner，改为 999:999"
      docker run --rm --user root --entrypoint chown -v "\$_vol:/data" "\${OPSPILOT_BACKEND_IMAGE}" -R 999:999 /data
    fi
  fi
done

# 后端镜像切换前先用新镜像执行迁移；失败会触发回滚且不会重启业务容器。
$([[ $DO_BACKEND -eq 1 ]] && echo "\$COMPOSE run --rm --no-deps init-db")
\$COMPOSE up -d ${SERVICES}
if [[ -f monitoring/docker-compose.yaml ]]; then
  \$COMPOSE_MONITORING up -d
fi

wait_http() {
  name="\$1"; url="\$2"; attempt=0
  while [ "\$attempt" -lt 18 ]; do
    if curl -fsS -m 8 -o /dev/null "\$url"; then
      echo "[health] \$name OK"
      return 0
    fi
    attempt=\$((attempt + 1))
    sleep 5
  done
  echo "[health] \$name FAILED" >&2
  return 1
}
$([[ $DO_FRONTEND -eq 1 ]] && echo "wait_http frontend http://127.0.0.1:8080/")
$([[ $DO_BACKEND -eq 1 ]] && echo "wait_http backend http://127.0.0.1:8001/api/settings/login-page")

trap - ERR
rm -f "\$BACKUP_ENV" app/docker-compose.yaml.pre-update monitoring/docker-compose.yaml.pre-update
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E 'opspilot-(backend|frontend|victoriametrics|victorialogs|grafana|alloy)'
echo '--- backend log (errors) ---'
docker logs opspilot-backend --since 2m 2>&1 | grep -iE 'error|traceback' | head -10 || true
echo '--- env ---'
grep -E '^OPSPILOT_(BACKEND|FRONTEND)_IMAGE=' .env
REMOTE
}

ssh "${SSH_OPTS[@]}" "$HOST" "bash -s" <<<"$(remote_script)"

log "DONE"
