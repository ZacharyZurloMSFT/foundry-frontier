#!/usr/bin/env pwsh
[CmdletBinding()]
param(
  [string]$ResourceGroup = $env:AZURE_RESOURCE_GROUP,
  [string]$Subscription
)

$ErrorActionPreference = 'Stop'

if ($Subscription) {
  az account set --subscription $Subscription
}
if (-not $ResourceGroup) {
  throw 'ResourceGroup is required. Set AZURE_RESOURCE_GROUP or pass -ResourceGroup.'
}

$accounts = az resource list `
  --resource-group $ResourceGroup `
  --resource-type 'Microsoft.DocumentDB/databaseAccounts' `
  --query '[].{id:id,name:name}' -o json | ConvertFrom-Json

$token = az account get-access-token `
  --resource 'https://management.azure.com/' `
  --query accessToken -o tsv

foreach ($account in $accounts) {
  Write-Host "Restoring private-only Cosmos access: $($account.name)" -ForegroundColor Yellow

  for ($attempt = 1; $attempt -le 80; $attempt++) {
    $state = az cosmosdb show `
      --resource-group $ResourceGroup `
      --name $account.name `
      --query provisioningState -o tsv
    if ($state -eq 'Succeeded') { break }
    if ($attempt -eq 80) {
      throw "Cosmos account $($account.name) did not leave '$state' before timeout."
    }
    Start-Sleep -Seconds 15
  }

  $url = "https://management.azure.com$($account.id)?api-version=2024-11-15"
  $body = @{
    properties = @{
      publicNetworkAccess = 'Disabled'
      networkAclBypass = 'None'
      isVirtualNetworkFilterEnabled = $false
      ipRules = @()
      virtualNetworkRules = @()
    }
  } | ConvertTo-Json -Depth 5 -Compress

  Invoke-RestMethod `
    -Method Patch `
    -Uri $url `
    -Headers @{ Authorization = "Bearer $token" } `
    -ContentType 'application/json' `
    -Body $body | Out-Null

  for ($attempt = 1; $attempt -le 80; $attempt++) {
    $accountState = az cosmosdb show `
      --resource-group $ResourceGroup `
      --name $account.name `
      --query '{state:provisioningState,public:publicNetworkAccess}' -o json | ConvertFrom-Json
    if ($accountState.state -eq 'Succeeded' -and $accountState.public -eq 'Disabled') {
      break
    }
    if ($attempt -eq 80) {
      throw "Cosmos account $($account.name) did not return to private-only access before timeout."
    }
    Start-Sleep -Seconds 15
  }

  az tag update `
    --resource-id $account.id `
    --operation Delete `
    --tags SecurityControl=Ignore | Out-Null
}

Write-Host 'Cosmos public access is disabled.' -ForegroundColor Green
