# Run scripts/create-department-indexes.py in the current PowerShell window,
# using the repo's .venv Python. Loads .env + azd env vars first so the
# script has everything it needs (Search endpoint/key, Storage, Foundry).
#
# Usage:
#   .\scripts\index-departments.ps1
#
# Runs synchronously; output streams into the current console.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'

# 1. Load .env into this process.
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        if ($line -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
            $val = $Matches[2].Trim('"')
            Set-Item -Path "Env:$($Matches[1])" -Value $val
        }
    }
}

# 2. Load azd Bicep outputs (STORAGE_ACCOUNT, AZURE_SUBSCRIPTION_ID, etc.).
. "$root/scripts/load-azd-env.ps1"

# 3. Pick the venv Python explicitly. Running python.exe (not py.exe or the
# file association) keeps the process attached to THIS console — no new
# window pops up.
$py = if (Test-Path "$root/.venv/Scripts/python.exe") {
    "$root/.venv/Scripts/python.exe"
} else {
    "python"
}

Write-Host "==> Running create-department-indexes.py with $py" -ForegroundColor Cyan
& $py "$root/scripts/create-department-indexes.py"
if ($LASTEXITCODE -ne 0) {
    Write-Error "create-department-indexes.py exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "`n==> Recreating Foundry IQ knowledge bases + sources" -ForegroundColor Cyan
& $py "$root/scripts/create-knowledge-base.py"
if ($LASTEXITCODE -ne 0) {
    Write-Error "create-knowledge-base.py exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "`nDone. Check indexer status in the portal:" -ForegroundColor Green
Write-Host "  Search service → srch-... → Indexers"
