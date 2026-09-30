#!/usr/bin/env bash
# 在可联网开发机构打包离线交付物（需 Docker）
# 用法: ./package.sh [tag]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PKG="$ROOT/deploy/offline"
TAG="${1:-1.0.0}"
IMAGES_DIR="$PKG/images"

log() { printf '\033[1;32m[package]\033[0m %s\n' "$*"; }

log "构建前后端镜像 ..."
docker build -t "opspilot/backend:${TAG}" "$ROOT/backend"
docker build -t "opspilot/frontend:${TAG}" "$ROOT/frontend"

# 若 tag 非 1.0.0，同步改 compose 默认（仍保留 1.0.0 便于 env 覆盖）
log "拉取基础/监控镜像 ..."
docker pull postgres:16-alpine
docker pull nginx:1.27-alpine
docker pull node:22-alpine
docker pull python:3.12-slim
docker pull victoriametrics/victoria-metrics:v1.140.0
docker pull victoriametrics/victoria-logs:v1.50.0
docker pull grafana/grafana:11.0.0
docker pull grafana/alloy:v1.16.3

mkdir -p "$IMAGES_DIR"
IMAGES=(
  "opspilot/backend:${TAG}"
  "opspilot/frontend:${TAG}"
  "postgres:16-alpine"
  "nginx:1.27-alpine"
  "node:22-alpine"
  "python:3.12-slim"
  "victoriametrics/victoria-metrics:v1.140.0"
  "victoriametrics/victoria-logs:v1.50.0"
  "grafana/grafana:11.0.0"
  "grafana/alloy:v1.16.3"
)

for img in "${IMAGES[@]}"; do
  safe="$(echo "$img" | tr '/:' '__')"
  out="$IMAGES_DIR/${safe}.tar"
  log "save $img -> $(basename "$out")"
  docker save -o "$out" "$img"
done

# 可选：Alloy 二进制（业务机 Agent 用），网络允许时下载
if [[ ! -f "$IMAGES_DIR/../alloy/alloy" ]]; then
  mkdir -p "$PKG/alloy"
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) A=amd64 ;;
    aarch64|arm64) A=arm64 ;;
    *) A="" ;;
  esac
  if [[ -n "$A" ]]; then
    URL="https://github.com/grafana/alloy/releases/download/v1.16.3/alloy-linux-${A}.gz"
    if curl -fsSL -o "$PKG/alloy/alloy.gz" "$URL" 2>/dev/null; then
      gunzip -f "$PKG/alloy/alloy.gz"
      chmod +x "$PKG/alloy/alloy"
      log "已下载 alloy binary ($A)"
    else
      warn_skip=1
      log "Alloy 二进制下载失败（可稍后手工放入 alloy/）"
    fi
  fi
fi

log "打包完成。目录: $PKG"
ls -lh "$IMAGES_DIR"
