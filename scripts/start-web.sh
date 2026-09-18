#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d web/dist ]; then (cd web && npm ci && npm run build); fi
exec python3.11 -m uvicorn server.main:app --host "${RED_POTATO_RADAR_HOST:-127.0.0.1}" --port "${RED_POTATO_RADAR_PORT:-8000}"
