#!/usr/bin/env bash
set -euo pipefail
echo '=== listen ==='
ss -ltnp | grep -E ':(8428|9428|3000|8001|8080)\s' || true
echo '=== docker port ==='
docker ps --format '{{.Names}} {{.Ports}}' | grep opspilot
echo '=== app network gateway ==='
docker inspect opspilot-app_default -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
echo '=== curl via gateway from host ==='
GW=$(docker inspect opspilot-app_default -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
curl -m 3 -sS -o /dev/null -w "gw_vm %{http_code}\n" "http://${GW}:8428/-/healthy" || true
curl -m 3 -sS -o /dev/null -w "ip_vm %{http_code}\n" "http://10.10.0.9:8428/-/healthy" || true
curl -m 3 -sS -o /dev/null -w "lo_vm %{http_code}\n" "http://127.0.0.1:8428/-/healthy" || true
echo '=== from backend to gw ==='
docker exec opspilot-backend python -c "import urllib.request; print(urllib.request.urlopen('http://${GW}:8428/-/healthy', timeout=4).read())" || true
echo '=== ip addr 10.10 ==='
ip -4 addr | grep -E '10.10.0|192.168.1' || true
