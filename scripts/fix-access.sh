#!/usr/bin/env bash
# One-shot repair for Northwind Copilot data-plane access.
#
# Fixes the whole system after any drift on Foundry / Storage / Cosmos /
# Search / Container Apps caused by policy resets, key rotation, or a
# portal flip to private / AAD-only. Symptoms this repairs:
#
#   - Foundry prompt agent knowledge-base tool: "Access denied when
#     connecting to the MCP server ... 403 Forbidden"
#   - fetch-keys returns empty strings (local auth was disabled)
#   - Any data-plane call from the API / seed scripts returning 401 / 403
#   - Public endpoints becoming unreachable after a policy sweep
#
# Steps (all idempotent - safe to re-run):
#
#   1. enable-public-access.sh   -> tag RG with SecurityControl=Ignore,
#      flip Foundry / Storage / Search back to public network access,
#      re-enable local (key) auth, pin Search to apiKeyOnly.
#   2. fetch-keys                -> write fresh admin/API keys for
#      Foundry, Cosmos, Storage, and Search into <repo>/.env.
#   3. load .env                 -> export those keys so subsequent
#      Python scripts see them (fills gaps that azd env doesn't mirror
#      - most notably AZURE_SUBSCRIPTION_ID and every *_KEY).
#   4. connect-knowledge-base.py -> re-PUT each department's `<kb>-mcp`
#      Foundry project connection (for example nw-claims-kb-mcp) with the
#      fresh Search admin key so the prompt agents' knowledge-base MCP
#      tools keep working. The isolated insurance connection
#      northwind-insurance-kb-mcp belongs to publish-insurance-knowledge.py
#      and is not touched.
#
# If you only need one of the steps, run the underlying script directly.
#
# Usage: ./scripts/fix-access.sh [resource-group]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RG="${1:-${AZURE_RESOURCE_GROUP:-}}"
export PYTHONIOENCODING=utf-8

echo "==> [1/4] Enabling public network access + local auth on all data-plane resources..."
bash "$ROOT/scripts/enable-public-access.sh" ${RG:+"$RG"}

echo
echo "==> [2/4] Fetching fresh admin/API keys into .env..."
# fetch-keys.sh is the sibling of enable-public-access.sh; if only .ps1
# exists, fall back to pwsh so sh users don't need a duplicate file.
if [[ -x "$ROOT/scripts/fetch-keys.sh" ]]; then
  bash "$ROOT/scripts/fetch-keys.sh" ${RG:+"$RG"}
elif command -v pwsh >/dev/null 2>&1; then
  pwsh "$ROOT/scripts/fetch-keys.ps1" ${RG:+-ResourceGroup "$RG"}
else
  echo "ERROR: neither scripts/fetch-keys.sh nor pwsh is available." >&2
  exit 1
fi

echo
echo "==> [3/4] Loading .env into current process..."
if [[ ! -f "$ROOT/.env" ]]; then
  echo "ERROR: .env not found at $ROOT/.env - fetch-keys must run first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. "$ROOT/.env"
set +a
if [[ -z "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  export AZURE_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
fi

echo
echo "==> [4/4] Re-provisioning the Foundry KB MCP project connections with the fresh key..."
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python"
fi
"$PY" "$ROOT/scripts/connect-knowledge-base.py"

echo
echo "==> Access repaired across Foundry / Storage / Search / Cosmos."
echo "    Retry the affected agent in the Foundry portal, or exercise the API endpoints."
