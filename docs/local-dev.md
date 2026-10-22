# Local dev — running the demo on your laptop

## TL;DR

Two terminals. The portal and API speak to the same Azure Foundry / Search /
Cosmos / Storage your `azd up` provisioned; nothing here runs Foundry locally.
The web app is the Northwind Insurance portal: it signs in with Microsoft Entra
and calls only the API's `/api/v1` routes. There is no department picker, no
"AI workspace" tab and no voice app.

### One-time setup

From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r apps\api\requirements.txt
cd apps\web-tech; npm ci; cd ..\..
```

The API needs a configuration file and, to use the signed-in portal, an Entra
registration plus a seeded run. See [Configuration](#configuration) and
[Sign-in prerequisites](#sign-in-prerequisites) below. Without them the public
pages (Home, Coverage information, Claims help) still load and **My account**
reports that it is unavailable; no demo account is substituted.

### Terminal 1 — API (FastAPI)

```powershell
.\scripts\dev-api.ps1              # PowerShell
# or
./scripts/dev-api.sh               # Git Bash / WSL
```

Ctrl+C to stop. `--reload` picks up Python edits.

Serves on `http://127.0.0.1:8001`. **Port 8001 is deliberate** — Windows'
WSL2 port relay grabs `:8000` (both uvicorn and `wslrelay` end up listening
there and IPv6 `localhost` gets black-holed). Use `PORT=<n>` env var to
override.

### Terminal 2 — web-tech (React + Vite)

```powershell
cd apps\web-tech
$env:VITE_API_BASE_URL             = "http://127.0.0.1:8001"
$env:VITE_INSURANCE_AUTH_TENANT_ID = "<Entra tenant GUID>"
$env:VITE_INSURANCE_AUTH_CLIENT_ID = "<SPA app (client) GUID>"
$env:VITE_INSURANCE_AUTH_SCOPE     = "api://<API app GUID>/access_as_user"
$env:VITE_INSURANCE_RUN_ID         = "<id of a seeded run>"
npm run dev
```

Serves on `http://localhost:5173`. Vite reads the `VITE_*` values when the dev
server starts, so restart it after changing them. `VITE_API_BASE_URL` must be
HTTPS, except `http://localhost`, `http://127.0.0.1` or `http://[::1]`, with no
credentials, query or fragment. Anything else, or a missing `VITE_INSURANCE_*`
value, leaves **My account** unavailable. The SPA registration must list
`http://localhost:5173/` as a redirect URI.

## Sign-in prerequisites

The portal trusts only the API, and the API trusts only a real Entra token that
the persisted run maps to a role (`policyholder`, `adjuster` or `presenter`).
Both pieces are operator steps (see the [README](../README.md)):

- `scripts\configure-insurance-identity.py` previews offline by default; with
  `--apply` it creates the API and SPA registrations. It prints the
  `INSURANCE_AUTH_*` and `VITE_INSURANCE_*` values used here, and
  `--development` allows `http://localhost:5173/` as the SPA redirect.
- `scripts\seed-insurance-run.py` previews by default; with `--apply` it creates
  (never overwrites) the run that maps your Entra identity to a role. Use that
  run id as `VITE_INSURANCE_RUN_ID`.

## Sanity-check the API from another shell

```powershell
curl.exe -sS http://127.0.0.1:8001/health
curl.exe -sS http://127.0.0.1:8001/build
curl.exe -sS -i http://127.0.0.1:8001/api/v1/me
```

`/health` and `/build` return JSON immediately; `/health` should report
`"business_domain":"insurance"`. `/build` reports `git_sha: "local"` (or your
`GIT_SHA`), and prompt SHAs only after `scripts\bundle-assistant-assets.py` has
packaged them. `/api/v1/me` needs a bearer token, so without one it returns the
typed error envelope: `503 business_api_disabled` until the business API is
enabled and configured, then `401 authentication_required`. The API serves only
`/health`, `/build` and `/api/v1/*`; `/openapi.json` lists every route.

## Configuration

Put settings in `apps\api\.env` or the repo-root `.env` (a real environment
variable wins over both). `scripts\fetch-keys.ps1` writes a repo-root `.env` from
your azd environment: service endpoints and deployment names, plus service keys
when local auth is enabled on the resources. Add the `NORTHWIND_*` and
`INSURANCE_AUTH_*` values below to it. Each capability stays off until its settings
are present, so leave out anything you do not need.

```dotenv
# Connected insurance API. /api/v1/* answers 503 until all of these are set
# (business_api_disabled, identity_not_configured or storage_not_configured).
NORTHWIND_BUSINESS_DOMAIN=insurance
NORTHWIND_BUSINESS_API_ENABLED=true
INSURANCE_AUTH_TENANT_ID=<Entra tenant GUID>
INSURANCE_AUTH_AUDIENCE=<API app (client) GUID>
INSURANCE_AUTH_REQUIRED_SCOPE=access_as_user        # default
INSURANCE_AUTH_CLIENT_IDS=<SPA app (client) GUID>   # comma-separated allowlist
COSMOS_ENDPOINT=<Cosmos DB endpoint>
COSMOS_DATABASE=northwind                           # default

# Embedded assistants (optional, disabled by default). Name each surface you want.
NORTHWIND_ASSISTANTS_ENABLED=true
NORTHWIND_ASSISTANT_SURFACES=concierge,help_center
FOUNDRY_PROJECT_ENDPOINT=<Foundry project endpoint>
FOUNDRY_PLANNER_DEPLOYMENT=<chat deployment>        # or NORTHWIND_ASSISTANT_MODEL
AZURE_SEARCH_ENDPOINT=<https://your-search.search.windows.net>   # Help Center only
# The Help Center reads Search with an Entra token by default. For a read-only
# query key instead (no automatic fallback):
# NORTHWIND_HELP_CENTER_SEARCH_AUTH=query_key
# NORTHWIND_HELP_CENTER_SEARCH_QUERY_KEY=<query key>

# Build stamp reported by /build
GIT_SHA=local-dev
BUILD_ENV=local
```

Credentials: Cosmos uses `COSMOS_KEY` when it is present. Foundry and Search
calls use Entra tokens through `DefaultAzureCredential`, so run `az login` as an
identity with data-plane access (a managed identity is used instead when
`AZURE_CLIENT_ID` is set, as in Container Apps). Evidence storage and the
evidence worker use a managed identity only, so uploading and processing claim
documents is an in-Azure scenario rather than a laptop one.
`GET /api/v1/assistants/status` lists the surfaces your role may use and gives a
`reason` for any enabled surface whose configuration is incomplete.

## Run the checks

```powershell
# Web (from apps\web-tech)
npm run typecheck
npx vitest run
npm run build

# API (from the repo root; CI pins ruff 0.8.4 and mypy 1.13.0)
pip install ruff==0.8.4 mypy==1.13.0 reportlab==4.2.5
python data/seed/generate.py --domain insurance   # offline; some tests read these ignored PDFs
ruff check apps/api/app/business apps/api/app/assistants
mypy apps/api/app/business apps/api/app/assistants --ignore-missing-imports --follow-imports=silent
$env:APPLICATIONINSIGHTS_CONNECTION_STRING = ""
python -m pytest apps/api/tests -q
python evals/assistant_eval.py --self-check       # offline assistant golden-set check
```

## Common gotchas

- **`ERR_CONNECTION_RESET` on every endpoint** → wslrelay is on `:8000`.
  Use port 8001 (the scripts do this automatically) or kill wslrelay.
- **`localhost` fails, `127.0.0.1` works** → IPv6 vs IPv4 mismatch. Either
  bind uvicorn with `--host ::` or point the frontend at `127.0.0.1`
  explicitly (`VITE_API_BASE_URL`).
- **"My account is unavailable"** → a `VITE_INSURANCE_*` value is missing or
  malformed (tenant and client must be GUIDs, the scope a full delegated-scope
  URI such as `api://<guid>/access_as_user`, the run id 1–64 letters, digits, `_`
  or `-`), or `VITE_API_BASE_URL` is not an allowed URL. Restart `npm run dev`
  after fixing it.
- **`503 business_api_disabled`** from `/api/v1/*` → set
  `NORTHWIND_BUSINESS_DOMAIN=insurance` and `NORTHWIND_BUSINESS_API_ENABLED=true`.
  `503 identity_not_configured` or `storage_not_configured` → a tenant,
  audience, scope or client GUID, or `COSMOS_ENDPOINT`, is missing.
- **`503 storage_unavailable` or `dependency_unavailable`** → Cosmos is
  private-endpoint-only in the template (public network access disabled), so the
  business and assistant routes need a machine with a network path to it. Opening
  access is an operator decision.
- **`403 forbidden` ("This identity cannot access the demo run")** → the run
  named by `VITE_INSURANCE_RUN_ID` does not map the Entra identity you signed in
  with. `404` "Demo run not found" means the run was never seeded.
- **No assistant panels** → they stay hidden until `NORTHWIND_ASSISTANTS_ENABLED=true`
  and the surface is named in `NORTHWIND_ASSISTANT_SURFACES`; an enabled surface
  whose configuration is incomplete shows "not available right now" and
  `/api/v1/assistants/status` gives the reason.
- **Telemetry from a laptop** → if `.env` carries
  `APPLICATIONINSIGHTS_CONNECTION_STRING`, your local run exports traces to that
  Application Insights resource. Blank it to keep them out.

## Docker-compose alternative

`docker compose up --build` runs the API on `http://localhost:8000` and the
portal on `http://localhost:5173` (nginx serving a build made from source by
`apps\web-tech\Dockerfile.local`). Those are the only two services; there is no
voice app. It reads a repo-root `.env` (`env_file`) and mounts
`agents\insurance\assistants` and `data\northwind` read-only into the API
container at `/app/app/_agent_prompts` and `/app/app/_northwind`.

Compose passes only `VITE_API_BASE_URL` to the SPA build. To sign in, supply the
four `VITE_INSURANCE_*` values as well (under `web-tech.build.args` in
`docker-compose.yml`, or with `docker compose build --build-arg`); they are
baked in at build time. On Windows you'll hit the wslrelay problem on `:8000`,
so prefer the two-terminal approach above.
