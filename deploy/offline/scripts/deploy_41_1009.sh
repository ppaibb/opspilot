#!/usr/bin/env bash
# 部署 1B+2B 到 10.10.0.9 离线安装目录
# 前置：镜像 tar / monitoring compose / alloy.alloy 已 scp 到 /tmp/opspilot-deploy-41/
set -euo pipefail
export PATH="${PATH}:/usr/libexec/docker/cli-plugins:/usr/lib/docker/cli-plugins:/usr/local/lib/docker/cli-plugins"

STAGE=/opt/opspilot-src/opspilot-offline-stage
SRC=/tmp/opspilot-deploy-41
# 与资产表 localhost 一致（后端容器 IP）；日志 host / host-metrics 都靠它对齐
HOST_INSTANCE="${HOST_INSTANCE:-172.19.0.3}"
HOST_NAME="${HOST_NAME:-aitest-System-Product-Name}"

log() { printf '[deploy41] %s\n' "$*"; }

cd "$STAGE"

log "load images..."
docker load -i "$SRC/opspilot_frontend_1.0.0.tar"
docker load -i "$SRC/opspilot_backend_1.0.0.tar"
if [[ -f "$SRC/grafana_alloy_v1.16.3.tar" ]]; then
  docker load -i "$SRC/grafana_alloy_v1.16.3.tar"
elif docker images --format '{{.Repository}}:{{.Tag}}' | grep -qx 'grafana/alloy:v1.16.3'; then
  log "alloy v1.16.3 already present"
elif docker images --format '{{.Repository}}:{{.Tag}}' | grep -q 'alloy'; then
  EXIST="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep alloy | head -1)"
  log "retag $EXIST -> grafana/alloy:v1.16.3"
  docker tag "$EXIST" grafana/alloy:v1.16.3
else
  log "ERROR: no alloy image"; exit 1
fi

# 同步监控栈 compose + alloy 配置
mkdir -p "$STAGE/monitoring/config"
cp -f "$SRC/docker-compose.yaml" "$STAGE/monitoring/docker-compose.yaml"
cp -f "$SRC/alloy.alloy" "$STAGE/monitoring/config/alloy.alloy"
tr -d '\r' < "$STAGE/monitoring/docker-compose.yaml" > "$STAGE/monitoring/docker-compose.yaml.lf"
mv "$STAGE/monitoring/docker-compose.yaml.lf" "$STAGE/monitoring/docker-compose.yaml"
tr -d '\r' < "$STAGE/monitoring/config/alloy.alloy" > "$STAGE/monitoring/config/alloy.alloy.lf"
mv "$STAGE/monitoring/config/alloy.alloy.lf" "$STAGE/monitoring/config/alloy.alloy"

# .env：补 Alloy 标签（MONITORING_HOST 保持 172.19.0.1 给后端查存储，不覆盖）
if ! grep -q '^HOST_INSTANCE=' "$STAGE/.env"; then
  echo "HOST_INSTANCE=${HOST_INSTANCE}" >> "$STAGE/.env"
else
  sed -i "s|^HOST_INSTANCE=.*|HOST_INSTANCE=${HOST_INSTANCE}|" "$STAGE/.env"
fi
if ! grep -q '^HOST_NAME=' "$STAGE/.env"; then
  echo "HOST_NAME=${HOST_NAME}" >> "$STAGE/.env"
else
  sed -i "s|^HOST_NAME=.*|HOST_NAME=${HOST_NAME}|" "$STAGE/.env"
fi
if ! grep -q '^ALLOY_IMAGE=' "$STAGE/.env"; then
  echo 'ALLOY_IMAGE=grafana/alloy:v1.16.3' >> "$STAGE/.env"
fi
# compose 的 alloy 用 MONITORING_HOST 当 HOST_INSTANCE 会错（那是网关 172.19.0.1），
# 改为专用变量：alloy service 读 HOST_INSTANCE
# 已在 compose environment: HOST_INSTANCE: ${HOST_INSTANCE:-}
tr -d '\r' < "$STAGE/.env" > "$STAGE/.env.lf" && mv "$STAGE/.env.lf" "$STAGE/.env"

# 修正 compose：alloy 的 HOST_INSTANCE 不要用 MONITORING_HOST
sed -i 's|HOST_INSTANCE: ${MONITORING_HOST:-}|HOST_INSTANCE: ${HOST_INSTANCE:-${MONITORING_HOST:-}}|' "$STAGE/monitoring/docker-compose.yaml"

log "restart monitoring (add alloy)..."
set -a; source "$STAGE/.env"; set +a
# 导出给 compose
export HOST_INSTANCE HOST_NAME ALLOY_IMAGE="${ALLOY_IMAGE:-grafana/alloy:v1.16.3}"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "no compose"; exit 1
fi

"${COMPOSE[@]}" -f "$STAGE/monitoring/docker-compose.yaml" --env-file "$STAGE/.env" up -d
sleep 5
docker ps --format '{{.Names}} {{.Status}} {{.Image}}' | grep -E 'opspilot-(alloy|victoria|grafana)' || true

log "restart app backend+frontend..."
"${COMPOSE[@]}" -f "$STAGE/app/docker-compose.yaml" --env-file "$STAGE/.env" up -d backend frontend
sleep 8

log "health..."
curl -fsS -m 8 -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1:8080/ || true
curl -fsS -m 8 -o /dev/null -w 'login-page %{http_code}\n' http://127.0.0.1:8001/api/settings/login-page || true
curl -fsS -m 8 -o /dev/null -w 'alloy-ready %{http_code}\n' http://127.0.0.1:12346/-/ready || true

# API 冒烟：本机 target 应为 builtin/up
python3 - <<'PY'
import json, urllib.request
pw=None
for line in open('/opt/opspilot-src/opspilot-offline-stage/.env',encoding='utf-8',errors='ignore'):
    line=line.strip().lstrip('﻿')
    if line.startswith('INIT_ADMIN_PASSWORD='):
        pw=line.split('=',1)[1].strip()
body=json.dumps({'username':'admin','password':pw}).encode()
req=urllib.request.Request('http://127.0.0.1:8001/api/auth/login',data=body,headers={'Content-Type':'application/json'})
tok=json.load(urllib.request.urlopen(req,timeout=20))['access_token']
H={'Authorization':'Bearer '+tok}
tg=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/targets',headers=H),timeout=20))
for t in tg.get('targets') or []:
    if str(t.get('provider','')).lower()=='localhost' or t.get('asset_id')=='ins-localhost':
        print('BUILTIN_TARGET', t.get('asset_id'), t.get('source'), t.get('status'), t.get('enabled'), t.get('metrics_enabled'), t.get('logs_enabled'))
lt=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/log-targets',headers=H),timeout=20))
for t in lt.get('targets') or []:
    if str(t.get('provider','')).lower()=='localhost' or t.get('asset_id')=='ins-localhost':
        print('BUILTIN_LOG', t.get('asset_id'), t.get('source'), t.get('status'), t.get('enabled'))
# host-metrics 是否有数据
hm=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/host-metrics?host_ip=172.19.0.3&range_minutes=15',headers=H),timeout=30))
pts=(hm.get('metrics') or [])
print('HOST_METRICS points', len(pts), 'configured', hm.get('configured'))
if pts:
    print(' last', pts[-1])
# 日志是否有 host=instance 样本
q=json.dumps({'query':'*','limit':5,'start':'2024-01-01T00:00:00Z','end':'2099-01-01T00:00:00Z'}).encode()
# use relative: last hour
from datetime import datetime, timedelta, timezone
end=datetime.now(timezone.utc)
start=end-timedelta(hours=1)
q=json.dumps({'query':'*','limit':5,'start':start.isoformat(),'end':end.isoformat(),'host_ips':['172.19.0.3']}).encode()
req=urllib.request.Request('http://127.0.0.1:8001/api/monitoring/logs/query',data=q,headers={**H,'Content-Type':'application/json'},method='POST')
logs=json.load(urllib.request.urlopen(req,timeout=30))
print('HOST_LOGS count', len(logs.get('logs') or []), 'configured', logs.get('configured'))
for line in (logs.get('logs') or [])[:2]:
    print(' log', line.get('host') or (line.get('fields') or {}).get('host'), line.get('message','')[:80])
PY

log "containers:"
docker ps --format '{{.Names}} {{.Status}}' | grep opspilot || true
log "DONE"
