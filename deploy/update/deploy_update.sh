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
# 默认：前后端都打新 tag（在目标机当前 tag 上递增 patch），构建→传输→改 .env→重启→验证。
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

# ── 改 .env + 重启 ────────────────────────────────────────
SERVICES=""
[[ $DO_BACKEND -eq 1 ]] && SERVICES+=" backend"
[[ $DO_FRONTEND -eq 1 ]] && SERVICES+=" frontend"
SERVICES="${SERVICES# }"

log "更新 $STAGE_DIR/.env 并重启 ($SERVICES) ..."

remote_script() {
  cat <<REMOTE
set -e
$([[ $DO_BACKEND -eq 1 ]] && echo "sed -i 's|^OPSPILOT_BACKEND_IMAGE=.*|OPSPILOT_BACKEND_IMAGE=${BACKEND_IMG}|' '$STAGE_DIR/.env'")
$([[ $DO_FRONTEND -eq 1 ]] && echo "sed -i 's|^OPSPILOT_FRONTEND_IMAGE=.*|OPSPILOT_FRONTEND_IMAGE=${FRONTEND_IMG}|' '$STAGE_DIR/.env'")
export PATH="\$PATH:/usr/libexec/docker/cli-plugins:/usr/lib/docker/cli-plugins:/usr/local/lib/docker/cli-plugins"
cd '$STAGE_DIR'
set -a; . ./.env; set +a
if docker compose version >/dev/null 2>&1; then C='docker compose'; else C='docker-compose'; fi
\$C -f app/docker-compose.yaml --env-file .env up -d ${SERVICES}
sleep 8
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E 'opspilot-(backend|frontend)' || true
echo '--- health ---'
curl -fsS -m 8 -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1:8080/ || true
curl -fsS -m 8 -o /dev/null -w 'login-page %{http_code}\n' http://127.0.0.1:8001/api/settings/login-page || true
echo '--- backend log (errors) ---'
docker logs opspilot-backend --since 2m 2>&1 | grep -iE 'error|traceback' | head -10 || true
echo '--- env ---'
grep -E '^OPSPILOT_(BACKEND|FRONTEND)_IMAGE=' '$STAGE_DIR/.env' || true
REMOTE
}

ssh "${SSH_OPTS[@]}" "$HOST" "bash -s" <<<"$(remote_script)"

log "DONE"
