#!/usr/bin/env bash
# Flip all data-plane resources in the azd resource group back to public network access.
# Tags the RG with SecurityControl=Ignore first so policy stops re-privatizing them.
# Usage: ./scripts/enable-public-access.sh [resource-group]
set -euo pipefail

RG="${1:-${AZURE_RESOURCE_GROUP:-}}"
if [[ -z "$RG" ]]; then
  if command -v azd >/dev/null 2>&1; then
    RG="$(azd env get-values 2>/dev/null | awk -F= '/^AZURE_RESOURCE_GROUP=/{gsub(/"/, "", $2); print $2}')"
  fi
fi
if [[ -z "$RG" ]]; then
  echo "ERROR: pass resource group as \$1 or set AZURE_RESOURCE_GROUP." >&2
  exit 1
fi

# ==========================================
# Apply SecurityControl Ignore Tag (FIRST)
# ==========================================
echo "Applying SecurityControl=Ignore tag to '$RG'..."
az group update --name "$RG" --set tags.SecurityControl=Ignore >/dev/null
sleep 5

# ==========================================
# Discover resources by type
# ==========================================
mapfile -t FOUNDRY_SERVICES < <(az resource list -g "$RG" \
  --resource-type "Microsoft.CognitiveServices/accounts" --query "[].name" -o tsv)
mapfile -t STORAGE_ACCOUNTS < <(az resource list -g "$RG" \
  --resource-type "Microsoft.Storage/storageAccounts" --query "[].name" -o tsv)
mapfile -t SEARCH_SERVICES < <(az resource list -g "$RG" \
  --resource-type "Microsoft.Search/searchServices" --query "[].name" -o tsv)
mapfile -t COSMOS_ACCOUNTS < <(az resource list -g "$RG" \
  --resource-type "Microsoft.DocumentDB/databaseAccounts" --query "[].name" -o tsv)

# ==========================================
# Foundry / Azure AI Services
# ==========================================
for SERVICE in "${FOUNDRY_SERVICES[@]:-}"; do
  [[ -z "$SERVICE" ]] && continue
  echo
  echo "Updating Foundry: $SERVICE"
  FOUNDRY_ID="$(az cognitiveservices account show -g "$RG" -n "$SERVICE" --query id -o tsv)"
  # PATCH directly — avoids az resource update's GET+PUT (returns 400) and
  # works on older az CLI without --public-network-access on cognitiveservices.
  az rest --method patch \
    --url "https://management.azure.com${FOUNDRY_ID}?api-version=2024-10-01" \
    --body '{"properties":{"publicNetworkAccess":"Enabled","disableLocalAuth":false,"networkAcls":{"defaultAction":"Allow"}}}' >/dev/null
done

# ==========================================
# Storage Accounts
# ==========================================
for STORAGE in "${STORAGE_ACCOUNTS[@]:-}"; do
  [[ -z "$STORAGE" ]] && continue
  echo
  echo "Updating Storage: $STORAGE"
  az storage account update -g "$RG" -n "$STORAGE" --public-network-access Enabled >/dev/null
  az storage account update -g "$RG" -n "$STORAGE" --allow-shared-key-access true >/dev/null
done

# ==========================================
# Azure AI Search
# ==========================================
for SEARCH in "${SEARCH_SERVICES[@]:-}"; do
  [[ -z "$SEARCH" ]] && continue
  echo
  echo "Updating Search: $SEARCH"
  az search service update -g "$RG" -n "$SEARCH" --public-network-access enabled >/dev/null
  # Force apiKeyOnly auth so admin/query keys always work. The Foundry KB
  # MCP connection (scripts/connect-knowledge-base.py) uses CustomKeys /
  # api-key header — if this flips to `aad` we get 403 on every MCP call.
  az search service update -g "$RG" -n "$SEARCH" --auth-options apiKeyOnly >/dev/null
done

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
for COSMOS in "${COSMOS_ACCOUNTS[@]:-}"; do
  [[ -z "$COSMOS" ]] && continue
  echo
  echo "Updating Cosmos: $COSMOS"

  # 1) Stamp the exemption tag on the account itself (MCAPS ignores
  #    RG-level tag inheritance for this policy).
  az cosmosdb update -g "$RG" -n "$COSMOS" --tags SecurityControl=Ignore >/dev/null

  # 2) Wait out any lock / re-enforcement pass so the patch below can
  #    acquire the exclusive lock on the account.
  for _ in $(seq 1 12); do
    STATE="$(az cosmosdb show -g "$RG" -n "$COSMOS" --query provisioningState -o tsv)"
    [[ "$STATE" == "Succeeded" ]] && break
    sleep 15
  done

  # 3) Flip publicNetworkAccess + local auth + firewall bypass in one PATCH.
  #    az rest guarantees the bypass/ipRules/vnet fields hit ARM even on
  #    older CLIs that don't expose them on `az cosmosdb update`.
  COSMOS_ID="$(az cosmosdb show -g "$RG" -n "$COSMOS" --query id -o tsv)"
  az rest --method patch \
    --url "https://management.azure.com${COSMOS_ID}?api-version=2024-05-15" \
    --body '{"properties":{"disableLocalAuth":false,"publicNetworkAccess":"Enabled","networkAclBypass":"AzureServices","ipRules":[],"virtualNetworkRules":[],"isVirtualNetworkFilterEnabled":false}}' >/dev/null
done

echo
echo "Completed successfully."
