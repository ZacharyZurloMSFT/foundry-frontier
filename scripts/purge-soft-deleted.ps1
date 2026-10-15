#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Purges soft-deleted Key Vaults and Cognitive Services (Foundry) accounts
  so `azd up` can redeploy them without name conflicts.

.PARAMETER NamePrefix
  Optional filter — only purge resources whose name contains this substring.
  Useful if you share a subscription with other projects. Defaults to '' (all).

.PARAMETER ResourceGroup
  If set, restrict Cognitive Services purge to items originally in this RG.
  Key Vaults are subscription-scoped when soft-deleted; the RG filter is best-effort.

.EXAMPLE
  ./scripts/purge-soft-deleted.ps1 -NamePrefix 3wwysrvgerlhi
  ./scripts/purge-soft-deleted.ps1 -ResourceGroup rg-northwind-dev
#>
[CmdletBinding()]
param(
  [string]$NamePrefix = '',
  [string]$ResourceGroup
)

$ErrorActionPreference = 'Stop'
Write-Host "==> Scanning for soft-deleted Key Vaults..." -ForegroundColor Cyan
$kvs = az keyvault list-deleted --query "[].{name:name, location:properties.location, rg:properties.deletionDate && properties.vaultId}" -o json | ConvertFrom-Json
if ($NamePrefix) { $kvs = $kvs | Where-Object { $_.name -like "*$NamePrefix*" } }

foreach ($kv in $kvs) {
  Write-Host "→ Purging Key Vault: $($kv.name) ($($kv.location))" -ForegroundColor Yellow
  az keyvault purge --name $kv.name --location $kv.location
  if ($LASTEXITCODE -ne 0) { Write-Warning "  failed to purge $($kv.name)" }
  else { Write-Host "  ✓ purged" -ForegroundColor Green }
}

Write-Host ""
Write-Host "==> Scanning for soft-deleted Cognitive Services / Foundry accounts..." -ForegroundColor Cyan
$cogs = az cognitiveservices account list-deleted --query "[].{name:name, location:location, rg:resourceGroup}" -o json | ConvertFrom-Json
if ($NamePrefix)   { $cogs = $cogs | Where-Object { $_.name -like "*$NamePrefix*" } }
if ($ResourceGroup) { $cogs = $cogs | Where-Object { $_.rg -eq $ResourceGroup } }

foreach ($c in $cogs) {
  Write-Host "→ Purging Cognitive Services: $($c.name) ($($c.location), rg=$($c.rg))" -ForegroundColor Yellow
  az cognitiveservices account purge --name $c.name --location $c.location --resource-group $c.rg
  if ($LASTEXITCODE -ne 0) { Write-Warning "  failed to purge $($c.name)" }
  else { Write-Host "  ✓ purged" -ForegroundColor Green }
}

if (-not $kvs -and -not $cogs) {
  Write-Host "Nothing to purge." -ForegroundColor Green
} else {
  Write-Host ""
  Write-Host "==> Done. Re-run 'azd up' or 'azd provision'." -ForegroundColor Cyan
}
