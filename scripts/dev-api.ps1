# Local dev launcher — starts uvicorn on 127.0.0.1:8001 with --reload.
# Ctrl+C to stop. Port 8001 avoids the wslrelay collision on :8000.
#
# Usage:
#   .\scripts\dev-api.ps1
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (Test-Path .\.venv\Scripts\Activate.ps1) {
    & .\.venv\Scripts\Activate.ps1
} else {
    Write-Error "No .venv found at $repo\.venv. Create one with: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r apps\api\requirements.txt"
    exit 1
}

Set-Location apps\api
$port = if ($env:PORT) { $env:PORT } else { 8001 }
uvicorn app.main:app --host 127.0.0.1 --port $port --reload --timeout-keep-alive 90
