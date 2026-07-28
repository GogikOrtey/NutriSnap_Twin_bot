#!/usr/bin/env bash
# deploy.sh — обновляет код с ветки build и пересобирает контейнер на SkyNode.
# Используется: вручную на VPS и из GitHub Actions (SSH → bash deploy.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> git fetch/reset origin/build"
git fetch origin build
git checkout build
git reset --hard origin/build

echo "==> prepare volumes"
mkdir -p logs
if [[ ! -f .reminder_day_keys.json ]]; then
  echo '{}' > .reminder_day_keys.json
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $ROOT — copy secrets before deploy." >&2
  exit 1
fi

echo "==> docker compose up --build -d"
docker compose -f docker-compose.yml -f docker-compose.vps.yml up --build -d

echo "==> status"
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
echo "Deploy done."
