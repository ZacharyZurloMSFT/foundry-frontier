#!/usr/bin/env sh
set -e
case "${NORTHWIND_REUSE_EXISTING_RESOURCES-false}" in
  true)
    echo "==> Reuse mode: skipping dependency installation, public-access changes, key fetching, and data/agent bootstrap."
    exit 0
    ;;
  false) ;;
  *)
    echo "NORTHWIND_REUSE_EXISTING_RESOURCES must be exactly true or false." >&2
    exit 1
    ;;
esac
case "${NORTHWIND_BUSINESS_DOMAIN-retail}" in
  insurance)
    echo "==> Insurance mode: skipping all legacy bootstrap. Publish synthetic knowledge explicitly with publish-insurance-knowledge.py."
    exit 0
    ;;
  retail) ;;
  *)
    echo "NORTHWIND_BUSINESS_DOMAIN must be exactly retail or insurance." >&2
    exit 1
    ;;
esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# UTF-8 stdout so Python scripts printing unicode arrows don't crash the hook.
export PYTHONIOENCODING=utf-8
# 1. Flip everything to public + key-auth-enabled (undoes MCAPS baseline drift).
bash "$ROOT/scripts/enable-public-access.sh"
# 2. Pull admin/API keys into .env at repo root for local dev + seed scripts.
bash "$ROOT/scripts/fetch-keys.sh"
# 2b. Load .env into this process so children (Python scripts) see keys.
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
# 2c. Load azd Bicep outputs (FOUNDRY_PROJECT_ENDPOINTS, MCP_*, etc.)
# into the process. fetch-keys.sh only pulls resource keys; azd outputs
# are separately queryable via `azd env get-values`.
while IFS='=' read -r key value; do
  [ -z "$key" ] && continue
  case "$key" in \#*) continue ;; esac
  # strip surrounding double-quotes from the value
  value="${value%\"}"; value="${value#\"}"
  export "$key=$value"
done <<EOF_AZD
$(azd env get-values 2>/dev/null || true)
EOF_AZD
# 3. Ensure Python deps for the post-provision scripts are present.
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python"; fi
"$PY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/scripts/requirements.txt"
"$PY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/data/seed/requirements.txt"
"$PY" "$ROOT/data/seed/generate.py"
"$PY" "$ROOT/data/seed/upload_blobs.py"
"$PY" "$ROOT/scripts/create-vector-indexer.py"
"$PY" "$ROOT/data/seed/seed_cosmos.py"
"$PY" "$ROOT/scripts/create-department-indexes.py"
"$PY" "$ROOT/scripts/create-knowledge-base.py"
"$PY" "$ROOT/scripts/connect-knowledge-base.py"
"$PY" "$ROOT/scripts/create-prompt-agents.py"
echo "Web-Tech:  ${WEB_TECH_URI}"
echo "API:       ${API_URI}"
