#!/usr/bin/env bash
# Promote the current staging image digests to the prod Container Apps
# environment WITHOUT rebuilding. Called by the prod job in
# .github/workflows/deploy-progressive.yml when a human dispatches with
# promote_to_prod=true (which is gated by the `prod` environment's required
# reviewers).
#
# Requires:
#   AZURE_ENV_NAME=prod (already selected)
#   Env vars from azd env (RESOURCE_GROUP_PROD, ACR_NAME, etc.)
set -euo pipefail

STAGING_RG="${STAGING_RESOURCE_GROUP:-rg-northwind-staging}"
PROD_RG="${PROD_RESOURCE_GROUP:-rg-northwind-prod}"
APPS=(api mcp-tech-lookup mcp-work-order cu-ingest)

for app in "${APPS[@]}"; do
  # Read the currently active image ref from staging.
  image=$(az containerapp show \
    --resource-group "$STAGING_RG" \
    --name "ca-$app" \
    --query "properties.template.containers[0].image" -o tsv)
  echo "promoting $app: $image"

  # Update prod to that same digest — no build, no rebuild, byte-identical.
  az containerapp update \
    --resource-group "$PROD_RG" \
    --name "ca-$app" \
    --image "$image" >/dev/null
done

echo "promotion complete."
