#!/usr/bin/env bash
set -euo pipefail

echo "=== stop/remove opspilot containers ==="
# 先停所有名字含 opspilot 的容器
mapfile -t CIDS < <(docker ps -a --format '{{.Names}} {{.ID}}' | awk 'tolower($1) ~ /opspilot/ {print $2}')
if ((${#CIDS[@]})); then
  docker stop "${CIDS[@]}" || true
  docker rm -f "${CIDS[@]}" || true
else
  echo "no opspilot containers"
fi

echo "=== remove opspilot volumes ==="
mapfile -t VOLS < <(docker volume ls --format '{{.Name}}' | grep -iE 'opspilot|opspilot-app|opspilot-monitoring' || true)
if ((${#VOLS[@]})); then
  docker volume rm -f "${VOLS[@]}" || true
else
  echo "no opspilot volumes"
fi

echo "=== remove /data/opspilot ==="
if [ -d /data/opspilot ]; then
  # 只删 opspilot 目录本身，不动 /data 其它内容
  rm -rf /data/opspilot
  echo "removed /data/opspilot"
else
  echo "/data/opspilot not found"
fi

echo "=== leftover check ==="
docker ps -a --format '{{.Names}}' | grep -i opspilot || echo "containers: none"
docker volume ls --format '{{.Name}}' | grep -i opspilot || echo "volumes: none"
[ -d /data/opspilot ] && echo "/data/opspilot still exists" || echo "/data/opspilot: gone"
ss -ltn 2>/dev/null | grep -E ':(8080|8001|8428|9428|3000|3002)\s' || echo "ports 8080/8001/8428/9428 free (or not listening)"
echo CLEAN_DONE
