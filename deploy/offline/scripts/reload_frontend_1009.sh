#!/usr/bin/env bash
set -euo pipefail
export PATH="${PATH}:/usr/libexec/docker/cli-plugins:/usr/lib/docker/cli-plugins:/usr/local/lib/docker/cli-plugins"
cd /opt/opspilot-src/opspilot-offline-stage
if docker compose version >/dev/null 2>&1; then
  docker compose -f app/docker-compose.yaml --env-file .env up -d frontend
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f app/docker-compose.yaml --env-file .env up -d frontend
else
  echo "no compose"; exit 1
fi
sleep 3
docker ps --format '{{.Names}} {{.Status}} {{.Image}}' | grep frontend || true
curl -fsS -m 5 -o /dev/null -w 'frontend %{http_code}\n' http://127.0.0.1:8080/ || true
echo DONE
