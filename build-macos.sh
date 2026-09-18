#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3.11 -m pip install -r requirements.txt
python3.11 -m playwright install chromium
python3.11 -m PyInstaller --noconfirm --clean packaging/macos.spec
test -d "dist/红薯雷达.app"
test -x "dist/红薯雷达MCP"
du -sh "dist/红薯雷达.app" "dist/红薯雷达MCP"
echo "Built macOS artifacts. Sign and notarize separately before public distribution."
