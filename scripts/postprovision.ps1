#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'
if ($env:NORTHWIND_REUSE_EXISTING_RESOURCES -ceq 'true') {
  Write-Host "==> Reuse mode: skipping dependency installation, public-access changes, key fetching, and data/agent bootstrap."
  exit 0
}
if ($null -ne $env:NORTHWIND_REUSE_EXISTING_RESOURCES -and $env:NORTHWIND_REUSE_EXISTING_RESOURCES -cne 'false') {
  throw "NORTHWIND_REUSE_EXISTING_RESOURCES must be exactly true or false."
}
if ($env:NORTHWIND_BUSINESS_DOMAIN -ceq 'insurance') {
  Write-Host "==> Insurance mode: skipping all legacy bootstrap. Publish synthetic knowledge explicitly with publish-insurance-knowledge.py."
  exit 0
}
if ($null -ne $env:NORTHWIND_BUSINESS_DOMAIN -and $env:NORTHWIND_BUSINESS_DOMAIN -cne 'retail') {
  throw "NORTHWIND_BUSINESS_DOMAIN must be exactly retail or insurance."
}
Write-Host "==> Northwind post-provision starting..."
$root = Split-Path -Parent $PSScriptRoot
# UTF-8 stdout so Python scripts printing '->', check-marks etc don't crash the hook.
$env:PYTHONIOENCODING = 'utf-8'

# 1. Ensure Python deps before opening the short Cosmos public-access window.
# Cosmos is private-endpoint-only at steady state; MCAPS policy closes the
# temporary public access quickly, so dependency installation must happen first.
$py = if (Test-Path "$root/.venv/Scripts/python.exe") { "$root/.venv/Scripts/python.exe" } else { "python" }
& $py -m pip install --quiet --disable-pip-version-check -r "$root/scripts/requirements.txt"
& $py -m pip install --quiet --disable-pip-version-check -r "$root/data/seed/requirements.txt"

# 2. Temporarily enable public access for local post-provision seeding.
& "$root/scripts/enable-public-access.ps1"

try {
  # 3. Pull admin/API keys into .env at repo root for local dev + seed scripts.
  & "$root/scripts/fetch-keys.ps1"

  # 3b. Load .env into this process so children (Python scripts) see keys.
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
  # 3c. Load azd Bicep outputs into the process (FOUNDRY_PROJECT_ENDPOINTS,
  # MCP_TECH_LOOKUP_URL, MCP_WORK_ORDER_URL, etc.). These are emitted as
  # ARM outputs by main.bicep and stored in .azure/<env>/.env by azd,
  # but fetch-keys.ps1 only pulls the Azure resource keys — not the
  # Bicep outputs. Dot-sourcing load-azd-env.ps1 fills that gap.
  . "$root/scripts/load-azd-env.ps1"

  # Seed Cosmos immediately while the temporary public-access window is open.
  & $py "$root/data/seed/seed_cosmos.py"

  & $py "$root/data/seed/generate.py"
  & $py "$root/data/seed/upload_blobs.py"
  & $py "$root/scripts/create-vector-indexer.py"
  & $py "$root/scripts/create-department-indexes.py"
  & $py "$root/scripts/create-knowledge-base.py"
  & $py "$root/scripts/connect-knowledge-base.py"
  & $py "$root/scripts/create-prompt-agents.py"
}
finally {
  & "$root/scripts/disable-cosmos-public-access.ps1"
}
Write-Host "==> Post-provision complete."
Write-Host "Web-Tech:  $env:WEB_TECH_URI"
Write-Host "API:       $env:API_URI"
