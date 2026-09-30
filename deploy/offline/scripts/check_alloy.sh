#!/usr/bin/env bash
set -euo pipefail

echo '=== host binary/service ==='
command -v alloy || echo 'alloy: not in PATH'
ls -l /usr/bin/alloy 2>/dev/null || true
systemctl is-active alloy 2>/dev/null || echo 'systemctl alloy: not active/absent'
systemctl is-enabled alloy 2>/dev/null || true

echo '=== docker alloy ==='
docker ps -a --format '{{.Names}} {{.Status}} {{.Image}}' | grep -i alloy || echo 'no alloy container'

echo '=== config head ==='
if [ -f /etc/alloy/config.alloy ]; then
  head -40 /etc/alloy/config.alloy
else
  echo 'no /etc/alloy/config.alloy'
fi

echo '=== override/token ==='
if [ -f /etc/systemd/system/alloy.service.d/opspilot.conf ]; then
  sed 's/\(TOKEN=\).\{8\}.*/\1****/' /etc/systemd/system/alloy.service.d/opspilot.conf
else
  echo 'no opspilot override'
fi

echo '=== journal ==='
journalctl -u alloy -n 15 --no-pager 2>/dev/null || true

echo '=== backend agents ==='
python3 - <<'PY'
import json, urllib.request, pathlib

def gv(path, key):
    for line in open(path, encoding='utf-8', errors='ignore'):
        line = line.strip().lstrip('﻿')
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return ''

env = '/opt/opspilot-src/opspilot-offline-stage/.env'
if not pathlib.Path(env).exists():
    print('no .env')
    raise SystemExit(0)
pw = gv(env, 'INIT_ADMIN_PASSWORD')
body = json.dumps({'username': 'admin', 'password': pw}).encode()
req = urllib.request.Request('http://127.0.0.1:8001/api/auth/login', data=body, headers={'Content-Type': 'application/json'})
try:
    tok = json.load(urllib.request.urlopen(req, timeout=15))['access_token']
except Exception as e:
    print('login fail', e)
    raise SystemExit(0)
H = {'Authorization': 'Bearer ' + tok}
try:
    data = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/collector/agents', headers=H), timeout=15))
    rows = data.get('agents') or []
    print('agents', len(rows))
    for a in rows:
        print(' ', a.get('agent_id'), a.get('hostname'), a.get('ip'), a.get('status'), a.get('last_seen_at'), a.get('current_version_id'), a.get('last_error'))
except Exception as e:
    print('agents fail', e)
try:
    data = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/targets', headers=H), timeout=15))
    tg = data.get('targets') or []
    print('targets', len(tg))
    for t in tg[:20]:
        print(' ', t.get('asset_id'), t.get('name'), t.get('ip'), t.get('status'), t.get('source'))
except Exception as e:
    print('targets fail', e)
PY
echo DONE
