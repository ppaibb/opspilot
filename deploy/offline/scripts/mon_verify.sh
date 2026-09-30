#!/usr/bin/env bash
set -euo pipefail
cd /opt/opspilot-src/opspilot-offline-stage

python3 - <<'PY'
import json, urllib.request, time

def gv(key):
    for line in open('.env', encoding='utf-8', errors='ignore'):
        line = line.strip().lstrip('﻿')
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return ''

user = gv('INIT_ADMIN_USERNAME') or 'admin'
pw = gv('INIT_ADMIN_PASSWORD')
body = json.dumps({'username': user, 'password': pw}).encode()
req = urllib.request.Request('http://127.0.0.1:8001/api/auth/login', data=body, headers={'Content-Type': 'application/json'})
tok = json.load(urllib.request.urlopen(req, timeout=15))['access_token']
H = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}

print('=== config ===')
cfg = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/config', headers=H), timeout=15))
for k in ('vm_url','vlogs_url','grafana_url','vlogs_ingest_url','remote_write_url'):
    print(k, '=', cfg.get(k))

print('=== self-check ===')
sc = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/self-check', data=b'{}', headers=H, method='POST'), timeout=30))
print('overall_ok', sc.get('overall_ok'))
for c in sc.get('checks') or []:
    print(' ', c.get('name'), '->', c.get('ok'), c.get('message'))

print('=== agents ===')
ags = json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001/api/monitoring/collector/agents', headers=H), timeout=15))
rows = ags.get('agents') or []
print('count', len(rows))
for a in rows[:15]:
    print(' ', a.get('agent_id'), a.get('hostname'), a.get('ip'), a.get('status'), a.get('last_seen_at'), a.get('current_version_id'))

print('=== storage ===')
for url in [
    'http://127.0.0.1:8428/-/healthy',
    'http://127.0.0.1:9428/-/healthy',
    'http://127.0.0.1:3000/api/health',
]:
    try:
        print(url, urllib.request.urlopen(url, timeout=5).read()[:60].decode(errors='ignore'))
    except Exception as e:
        print(url, 'FAIL', e)

print('=== alloy ===')
import subprocess, shutil
print('alloy bin', shutil.which('alloy'))
try:
    print(subprocess.check_output(['systemctl','is-active','alloy'], text=True, stderr=subprocess.DEVNULL).strip())
except Exception as e:
    print('systemctl', e)
try:
    out = subprocess.check_output(['docker','ps','--format','{{.Names}} {{.Image}}'], text=True)
    print([ln for ln in out.splitlines() if 'alloy' in ln.lower()] or 'no alloy container')
except Exception as e:
    print('docker', e)
PY
