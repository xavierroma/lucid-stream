#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Start Docker Desktop and retry."
  exit 1
fi

docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" ps
