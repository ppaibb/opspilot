#!/usr/bin/env bash
set -euo pipefail
echo '=== from host ==='
curl -m 3 -sS -o /dev/null -w 'host_ip %{http_code}\n' http://10.10.0.9:8428/-/healthy || echo host_ip fail
curl -m 3 -sS -o /dev/null -w 'loopback %{http_code}\n' http://127.0.0.1:8428/-/healthy || echo loopback fail

echo '=== from backend container ==='
docker exec opspilot-backend python - <<'PY'
import urllib.request
for url in [
    'http://10.10.0.9:8428/-/healthy',
    'http://172.17.0.1:8428/-/healthy',
    'http://127.0.0.1:8428/-/healthy',
]:
    try:
        print(url, urllib.request.urlopen(url, timeout=4).read()[:40])
    except Exception as e:
        print(url, 'FAIL', type(e).__name__, e)
PY

echo '=== docker network ==='
docker inspect opspilot-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
ip -4 addr show docker0 | head -3 || true
ufw status 2>/dev/null || true
