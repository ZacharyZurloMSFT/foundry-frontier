#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Flips all data-plane resources in the azd resource group to public network access.

.DESCRIPTION
  Tags the resource group with SecurityControl=Ignore first (so policy stops
  flipping resources back to private), then enables public network access and
  local (key) auth on Foundry / AI Services, Storage, and Azure AI Search.

.PARAMETER ResourceGroup
  Resource group name. Defaults to $env:AZURE_RESOURCE_GROUP (set by `azd env`).

.PARAMETER Subscription
  Optional subscription ID or name. Defaults to the current az context.

.EXAMPLE
  ./scripts/enable-public-access.ps1
  ./scripts/enable-public-access.ps1 -ResourceGroup rg-northwind-dev
#>
[CmdletBinding()]
param(
  [string]$ResourceGroup = $env:AZURE_RESOURCE_GROUP,
  [string]$Subscription
)

$ErrorActionPreference = 'Stop'

if (-not $ResourceGroup) {
  try {
    $azdVals = azd env get-values 2>$null
    $match = $azdVals | Select-String -Pattern '^AZURE_RESOURCE_GROUP="?([^"]+)"?$'
    if ($match) { $ResourceGroup = $match.Matches[0].Groups[1].Value }
  } catch { }
}
if (-not $ResourceGroup) {
  throw "ResourceGroup not provided and AZURE_RESOURCE_GROUP is not set. Pass -ResourceGroup <name> or run 'azd env select <env>'."
}

if ($Subscription) {
  az account set --subscription $Subscription | Out-Null
}

# ==========================================
# Apply SecurityControl Ignore Tag (FIRST)
# ==========================================

Write-Host "Applying SecurityControl=Ignore tag to '$ResourceGroup'..." -ForegroundColor Cyan

az group update `
  --name $ResourceGroup `
  --set tags.SecurityControl=Ignore | Out-Null

Start-Sleep -Seconds 5

# ==========================================
# Discover resources by type
# ==========================================

$FoundryServices = az resource list --resource-group $ResourceGroup `
  --resource-type "Microsoft.CognitiveServices/accounts" --query "[].name" -o tsv
$StorageAccounts = az resource list --resource-group $ResourceGroup `
  --resource-type "Microsoft.Storage/storageAccounts" --query "[].name" -o tsv
$SearchServices  = az resource list --resource-group $ResourceGroup `
  --resource-type "Microsoft.Search/searchServices" --query "[].name" -o tsv
$CosmosAccounts  = az resource list --resource-group $ResourceGroup `
  --resource-type "Microsoft.DocumentDB/databaseAccounts" --query "[].name" -o tsv

# ==========================================
# Foundry / Azure AI Services
# ==========================================

foreach ($Service in $FoundryServices) {
  if (-not $Service) { continue }
  Write-Host ""
  Write-Host "Updating Foundry: $Service" -ForegroundColor Yellow

  $FoundryId = az cognitiveservices account show `
    --resource-group $ResourceGroup `
    --name $Service `
    --query id -o tsv

  # PATCH directly — avoids az resource update's GET+PUT (which returns 400)
  # and works even on older az CLI without --public-network-access flag.
  az rest --method patch `
    --url "https://management.azure.com$FoundryId`?api-version=2024-10-01" `
    --body '{\"properties\":{\"publicNetworkAccess\":\"Enabled\",\"disableLocalAuth\":false,\"networkAcls\":{\"defaultAction\":\"Allow\"}}}' | Out-Null
}

# ==========================================
# Storage Accounts
# ==========================================

foreach ($Storage in $StorageAccounts) {
  if (-not $Storage) { continue }
  Write-Host ""
  Write-Host "Updating Storage: $Storage" -ForegroundColor Yellow

  az storage account update `
    --resource-group $ResourceGroup `
    --name $Storage `
    --public-network-access Enabled | Out-Null

  az storage account update `
    --resource-group $ResourceGroup `
    --name $Storage `
    --allow-shared-key-access true | Out-Null
}

# ==========================================
# Azure AI Search
# ==========================================

foreach ($Search in $SearchServices) {
  if (-not $Search) { continue }
  Write-Host ""
  Write-Host "Updating Search: $Search" -ForegroundColor Yellow

  az search service update `
    --resource-group $ResourceGroup `
    --name $Search `
    --public-network-access enabled | Out-Null

  # Force apiKeyOnly auth so admin/query keys always work. The Foundry KB
  # MCP connection (scripts/connect-knowledge-base.py) uses CustomKeys /
  # api-key header — if this flips to `aad` we get 403 on every MCP call.
  az search service update `
    --resource-group $ResourceGroup `
    --name $Search `
    --auth-options apiKeyOnly | Out-Null
}

# ==========================================
# Cosmos DB
# ==========================================
# Local (key) auth is required for the FastAPI backend to read/write
# conversations, workflow state, and the HITL queue when running from
# docker-compose or a local venv — DefaultAzureCredential inside the
# container can't see the host's `az login`, so key auth is the simplest
# path. Prod Container Apps still use managed identity via RBAC.
#
# The RG-level SecurityControl=Ignore tag isn't enough for Cosmos — the
# MCAPS policy re-flips publicNetworkAccess=Disabled unless the tag lives
# on the account itself. Tag first, wait for any in-flight update, then
# patch. Also disable the network ACL filter and set the bypass to
# AzureServices so Container Apps outbound IPs (dynamic) can reach the
# account without an ipRules maintenance burden.
foreach ($Cosmos in $CosmosAccounts) {
  if (-not $Cosmos) { continue }
  Write-Host ""
  Write-Host "Updating Cosmos: $Cosmos" -ForegroundColor Yellow

  # 1) Stamp the exemption tag on the account. This is the piece MCAPS
  #    honors — an RG-level tag isn't inherited to child resources by the
  #    policy engine, so the account needs its own.
  az cosmosdb update `
    --resource-group $ResourceGroup `
    --name $Cosmos `
    --tags SecurityControl=Ignore | Out-Null

  # 2) Wait out any lock/re-enforcement pass so the follow-up patch can
  #    acquire the exclusive lock on the account.
  for ($i = 0; $i -lt 12; $i++) {
    $state = az cosmosdb show --resource-group $ResourceGroup --name $Cosmos --query provisioningState -o tsv
    if ($state -eq 'Succeeded') { break }
    Start-Sleep -Seconds 15
  }

  # 3) Flip publicNetworkAccess + local auth + firewall bypass in one PATCH.
  #    We use az rest to guarantee the bypass/ipRules/vnet fields hit ARM
  #    even on older CLIs that don't expose them on `az cosmosdb update`.
  $CosmosId = az cosmosdb show `
    --resource-group $ResourceGroup `
    --name $Cosmos `
    --query id -o tsv
  az rest --method patch `
    --url "https://management.azure.com$CosmosId`?api-version=2024-05-15" `
    --body '{\"properties\":{\"disableLocalAuth\":false,\"publicNetworkAccess\":\"Enabled\",\"networkAclBypass\":\"AzureServices\",\"ipRules\":[],\"virtualNetworkRules\":[],\"isVirtualNetworkFilterEnabled\":false}}' | Out-Null
}

Write-Host ""
Write-Host "Completed successfully." -ForegroundColor Green
