#!/usr/bin/env bash
# Local dev launcher. Starts uvicorn on port 8001 (avoids the wslrelay
# collision that Windows puts on :8000), with the reload watcher and a
# long keep-alive so 30-second Foundry hosted-agent responses don't get
# killed mid-stream.
#
# Usage:
#   scripts/dev-api.sh              # start API
#   Ctrl+C                          # stop API
#
# See docs/local-dev.md for the matching web-tech instructions.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Activate the venv the same way both PowerShell and Git Bash can source.
if [ -f ".venv/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/Scripts/activate
elif [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "No .venv found at ${REPO}/.venv — create one with:"
  echo "  python -m venv .venv && source .venv/Scripts/activate && pip install -r apps/api/requirements.txt"
  exit 1
fi

cd apps/api
exec uvicorn app.main:app \
  --host 127.0.0.1 \
  --port "${PORT:-8001}" \
  --reload \
  --timeout-keep-alive 90
