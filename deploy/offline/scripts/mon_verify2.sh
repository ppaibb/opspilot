#!/usr/bin/env bash
set -euo pipefail
cd /opt/opspilot-src/opspilot-offline-stage

echo '=== containers ==='
docker ps --format '{{.Names}} {{.Status}}' | grep opspilot || true

echo '=== wait backend ==='
ok=0
for i in $(seq 1 20); do
  if curl -fsS -m 3 http://127.0.0.1:8001/api/settings/login-page >/dev/null; then
    echo BACKEND_OK; ok=1; break
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo BACKEND_FAIL; docker logs opspilot-backend --tail 40; exit 1; }

curl -fsS -m 5 -o /dev/null -w 'frontend HTTP %{http_code}\n' http://127.0.0.1:8080/
curl -fsS -m 3 http://127.0.0.1:8428/-/healthy; echo
curl -fsS -m 3 http://127.0.0.1:9428/-/healthy; echo
curl -fsS -m 3 http://127.0.0.1:3000/api/health | head -c 80; echo

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
print('LOGIN_OK', 'token_len', len(tok))
H = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}
cfg = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/config', headers=H), timeout=15))
print('MONITOR', cfg.get('vm_url'), cfg.get('vlogs_url'), cfg.get('grafana_url'), cfg.get('vlogs_ingest_url'))
try:
    sc = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/self-check', data=b'{}', headers=H, method='POST'), timeout=45))
    print('SELF_CHECK', sc.get('overall_ok'))
    for c in sc.get('checks') or []:
        print(' ', c.get('ok'), c.get('name'), c.get('message'))
except Exception as e:
    print('SELF_CHECK_ERR', e)
PY
echo DONE
