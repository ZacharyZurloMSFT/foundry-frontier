#!/usr/bin/env pwsh
<#
.SYNOPSIS
  One-shot repair for Northwind Copilot data-plane access.

.DESCRIPTION
  Fixes the whole system after any drift on Foundry / Storage / Cosmos /
  Search / Container Apps caused by policy resets, key rotation, or a
  portal flip to private / AAD-only. Symptoms this repairs:

    - Foundry manual-retrieval agent: "Access denied when connecting to
      the MCP server ... 403 Forbidden"
    - fetch-keys returns empty strings (local auth was disabled)
    - Any data-plane call from the API / seed scripts returning 401 / 403
    - Public endpoints becoming unreachable after a policy sweep

  Steps (all idempotent - safe to re-run):

    1. enable-public-access.ps1  -> tags RG with SecurityControl=Ignore,
       flips Foundry / Storage / Search back to public network access,
       re-enables local (key) auth, pins Search to apiKeyOnly.
    2. fetch-keys.ps1            -> writes fresh admin/API keys for
       Foundry, Cosmos, Storage, and Search into <repo>/.env.
    3. load .env                 -> exports those keys so subsequent
       Python scripts see them (fills gaps that azd env doesn't mirror
       - most notably AZURE_SUBSCRIPTION_ID and every *_KEY).
    4. connect-knowledge-base.py -> re-PUTs the northwind-kb-mcp Foundry
       project connection with the fresh Search admin key so the
       manual-retrieval agent's MCP tool keeps working.

  If you only need one of the steps, run the underlying script directly.

.PARAMETER ResourceGroup
  Optional override. Defaults to $env:AZURE_RESOURCE_GROUP (set by `azd env`).

.EXAMPLE
  ./scripts/fix-access.ps1
  ./scripts/fix-access.ps1 -ResourceGroup rg-northwind-dev
#>
[CmdletBinding()]
param(
  [string]$ResourceGroup = $env:AZURE_RESOURCE_GROUP
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'

Write-Host "==> [1/4] Enabling public network access + local auth on all data-plane resources..." -ForegroundColor Cyan
& "$root/scripts/enable-public-access.ps1" -ResourceGroup $ResourceGroup

Write-Host ""
Write-Host "==> [2/4] Fetching fresh admin/API keys into .env..." -ForegroundColor Cyan
& "$root/scripts/fetch-keys.ps1" -ResourceGroup $ResourceGroup

Write-Host ""
Write-Host "==> [3/4] Loading .env into current process..." -ForegroundColor Cyan
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
  throw ".env not found at $envFile - fetch-keys must run first."
}
foreach ($line in Get-Content $envFile) {
  if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
  if ($line -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
    Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim('"')
  }
}
if (-not $env:AZURE_SUBSCRIPTION_ID) {
  $env:AZURE_SUBSCRIPTION_ID = (az account show --query id -o tsv)
}

Write-Host ""
Write-Host "==> [4/4] Re-provisioning the Foundry KB MCP project connection with the fresh key..." -ForegroundColor Cyan
$py = if (Test-Path "$root/.venv/Scripts/python.exe") { "$root/.venv/Scripts/python.exe" } else { "python" }
& $py "$root/scripts/connect-knowledge-base.py"

Write-Host ""
Write-Host "==> Access repaired across Foundry / Storage / Search / Cosmos." -ForegroundColor Green
Write-Host "    Retry the manual-retrieval agent in the Foundry portal, or exercise the API endpoints." -ForegroundColor Green
