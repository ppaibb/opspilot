#!/usr/bin/env bash
set -euo pipefail
cd /opt/opspilot-src/opspilot-offline-stage

echo '=== containers ==='
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}' | grep opspilot || true

echo '=== wait backend ==='
ok=0
for i in $(seq 1 15); do
  if curl -fsS -m 3 http://127.0.0.1:8001/api/settings/login-page >/dev/null; then
    echo BACKEND_OK
    ok=1
    break
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo BACKEND_FAIL; exit 1; }

echo '=== frontend ==='
curl -fsS -m 5 -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8080/

echo '=== monitoring ==='
curl -fsS -m 3 http://127.0.0.1:8428/-/healthy; echo
curl -fsS -m 3 http://127.0.0.1:9428/-/healthy; echo
curl -fsS -m 3 http://127.0.0.1:3000/api/health; echo

echo '=== login ==='
python3 - <<'PY'
import json, urllib.request

def gv(path, key):
    for line in open(path, encoding='utf-8', errors='ignore'):
        line = line.strip().lstrip('﻿')
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return ''

user = gv('.env', 'INIT_ADMIN_USERNAME') or 'admin'
pw = gv('.env', 'INIT_ADMIN_PASSWORD')
body = json.dumps({'username': user, 'password': pw}).encode()
req = urllib.request.Request(
    'http://127.0.0.1:8001/api/auth/login',
    data=body,
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=15) as resp:
    data = json.load(resp)
tok = data.get('access_token', '')
print('LOGIN_OK', 'user=' + data.get('user', {}).get('username', ''), 'token_len=' + str(len(tok)))

# self-check monitoring connection if possible
req2 = urllib.request.Request(
    'http://127.0.0.1:8001/api/monitoring/config',
    headers={'Authorization': 'Bearer ' + tok},
)
with urllib.request.urlopen(req2, timeout=15) as resp2:
    cfg = json.load(resp2)
print('MONITOR_CONFIG', json.dumps({k: cfg.get(k) for k in ('vm_url','vlogs_url','grafana_url','vlogs_ingest_url')}, ensure_ascii=False))
PY

echo '=== summary URLs ==='
echo "UI:     http://192.168.3.100:8080"
echo "API:    http://192.168.3.100:8001"
echo "VM:     http://192.168.3.100:8428"
echo "VLogs:  http://192.168.3.100:9428"
echo "Grafana:http://192.168.3.100:3000"
grep -E '^INIT_ADMIN_PASSWORD=' .env | head -1 || true
echo DONE
