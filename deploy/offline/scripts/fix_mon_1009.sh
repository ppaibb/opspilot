#!/usr/bin/env bash
set -euo pipefail
cd /opt/opspilot-src/opspilot-offline-stage

# 1) 修正 .env 访问地址：wg0 的 10.10.0.9 不能本机访问
sed -i 's|PUBLIC_URL=http://10.10.0.9:8080|PUBLIC_URL=http://192.168.1.16:8080|' .env
sed -i 's|MONITORING_HOST=10.10.0.9|MONITORING_HOST=172.19.0.1|' .env
# 补充浏览器用地址（若已有则覆盖）
grep -q '^GRAFANA_URL=' .env || echo 'GRAFANA_URL=http://192.168.1.16:3000' >> .env
sed -i 's|^GRAFANA_URL=.*|GRAFANA_URL=http://192.168.1.16:3000|' .env
tr -d '\r' < .env > .env.lf && mv .env.lf .env

# 2) 写入监控配置（系统配置表），让 self-check / 查询走网关
python3 - <<'PY'
import json, urllib.request, urllib.error

def gv(key):
    for line in open('.env', encoding='utf-8', errors='ignore'):
        line = line.strip().lstrip('﻿')
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return ''

pw = gv('INIT_ADMIN_PASSWORD')
body = json.dumps({'username': 'admin', 'password': pw}).encode()
req = urllib.request.Request('http://127.0.0.1:8001/api/auth/login', data=body, headers={'Content-Type': 'application/json'})
tok = json.load(urllib.request.urlopen(req, timeout=20))['access_token']
H = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}

# 后端容器 → 存储：走 compose 网关；浏览器 Grafana：走 LAN IP
payload = {
    'vm_url': 'http://172.19.0.1:8428',
    'vlogs_url': 'http://172.19.0.1:9428',
    'grafana_url': 'http://192.168.1.16:3000',
    'remote_write_url': 'http://192.168.1.16:8428/api/v1/write',
    'vlogs_ingest_url': 'http://192.168.1.16:9428/insert/loki/api/v1/push',
    'vmui_url': 'http://192.168.1.16:8428/vmui',
    'vlogsui_url': 'http://192.168.1.16:9428/select/vmui',
}
req = urllib.request.Request(
    'http://127.0.0.1:8001/api/monitoring/config',
    data=json.dumps(payload).encode(),
    headers=H,
    method='PUT',
)
try:
    print('PUT config', urllib.request.urlopen(req, timeout=20).read()[:200])
except Exception as e:
    # 若 PUT 不支持，用 GET 确认当前
    print('PUT failed', e)
    cfg = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/config', headers=H), timeout=15))
    print('current', cfg.get('vm_url'), cfg.get('vlogs_url'), cfg.get('grafana_url'))
    raise SystemExit(1)

sc = json.load(urllib.request.urlopen(
    urllib.request.Request('http://127.0.0.1:8001/api/monitoring/self-check', data=b'{}', headers=H, method='POST'),
    timeout=45,
))
print('SELF_CHECK', sc.get('overall_ok'))
for c in sc.get('checks') or []:
    print(' ', c.get('ok'), c.get('name'), (c.get('message') or '')[:120])
PY

echo '=== URLs ==='
echo "UI:  http://192.168.1.16:8080"
echo "API: http://192.168.1.16:8001"
echo "VM:  http://192.168.1.16:8428"
echo "VLogs: http://192.168.1.16:9428"
echo "Grafana: http://192.168.1.16:3000"
grep '^INIT_ADMIN_PASSWORD=' .env || true
echo FIXED
