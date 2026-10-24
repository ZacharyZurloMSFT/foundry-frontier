# Local dev — running the demo on your laptop

## TL;DR

Two terminals, each a few seconds to start, plus an optional third for the voice app. The
API talks to the same Azure Foundry, Search, Cosmos and Storage resources your deployment
provisioned; nothing here runs Foundry locally, and the hosted agents run in Foundry, so
they cannot call a server on your laptop.

### One-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r apps\api\requirements.txt
pip install -r data\seed\requirements.txt
python data\seed\generate.py          # synthetic PDFs for Document Intelligence (git-ignored)
```

`scripts\dev-api.ps1` and `scripts/dev-api.sh` exit with an error if there is no `.venv` at
the repository root. The API reads settings from the environment or from a `.env` file at
the repository root or in `apps\api` (see [Environment](#environment)).

### Terminal 1 — API (FastAPI)

```powershell
.\scripts\dev-api.ps1              # PowerShell
# or
./scripts/dev-api.sh               # Git Bash / WSL
```

Ctrl+C to stop. `--reload` picks up Python edits and `--timeout-keep-alive 90` keeps long
agent streams alive.

Serves on `http://127.0.0.1:8001`. **Port 8001 is deliberate** — Windows' WSL2 port relay
grabs `:8000` (both uvicorn and `wslrelay` end up listening there and IPv6 `localhost` gets
black-holed). Use the `PORT=<n>` environment variable to override.

### Terminal 2 — web-portal (React + Vite)

```powershell
cd apps\web-portal
npm install                                     # first time; `npm ci` for a clean install
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
$env:VITE_INSURANCE_BUSINESS_ENABLED = "false"  # open the department workspace directly (see below)
$env:VITE_REPO_URL     = "https://github.com/<you>/foundry-frontier"   # optional; makes trace-panel SHAs clickable
npm run dev
```

Serves on `http://localhost:5173`.

The portal has two shells. Unless `VITE_INSURANCE_BUSINESS_ENABLED` is `"false"`, it opens
the **business-first portal** (public insurer pages and a Microsoft Entra sign-in for
policyholders, adjusters and presenters; staff reach the department workspace through
**My account → AI workspace**). Without the `VITE_INSURANCE_AUTH_TENANT_ID`,
`VITE_INSURANCE_AUTH_CLIENT_ID`, `VITE_INSURANCE_AUTH_SCOPE` and `VITE_INSURANCE_RUN_ID`
values it shows "My account is unavailable" and no sign-in is simulated. Setting the flag to
`"false"` skips sign-in and lands on the **department tiles** (Claims Concierge, Adjuster
Support, Claims Document Intelligence, Policyholder Contact Center), or on the department
you used last (it is remembered in the browser and in `?dept=`); that is what you want for
local work. Signing in locally additionally needs the API settings
`NORTHWIND_BUSINESS_API_ENABLED=true` and `INSURANCE_AUTH_*`, a seeded run
(`scripts/seed-insurance-run.py`) and Entra registrations with a localhost origin
(`scripts/configure-insurance-identity.py --development`, preview by default).

### Terminal 3 (optional) — web-voice

```powershell
cd apps\web-voice
npm install
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev
```

Serves on `http://localhost:5174`. The API's default `VOICE_LIVE_ALLOWED_ORIGINS` already
include `http://localhost:5173` and `http://localhost:5174`.

## Sanity-check the API from another shell

```powershell
curl.exe -sS http://127.0.0.1:8001/health
curl.exe -sS http://127.0.0.1:8001/build
curl.exe -sS http://127.0.0.1:8001/company
curl.exe -sS http://127.0.0.1:8001/insurance/fixtures
curl.exe -sS http://127.0.0.1:8001/adjuster-support/status
```

All five return JSON in under a second and need no Azure access. `/health` reports
`insurance_agent_runtime_ready`; `/insurance/fixtures` returns the synthetic policyholders,
policies, claims and wording plus `agent_runtime_ready`; `/adjuster-support/status`
explains whether grounded support is configured.

## Environment

`scripts\fetch-keys.ps1` (or `scripts/fetch-keys.sh`) writes a git-ignored `.env` at the
repository root with the keys and endpoints of the deployed resources. If your policy
disabled key authentication, run `scripts\enable-public-access.ps1` first; that script opens
public network access and key authentication on Foundry, Storage, Search and Cosmos DB
(`scripts\disable-cosmos-public-access.ps1` makes Cosmos private-only again).
`scripts\load-azd-env.ps1` (dot-sourced) loads
every azd value into the current session instead. Everything below is optional: the
records, drafting UI and department tiles work without it, and each missing piece shows up
as an explicit "unavailable" state rather than a fallback.

```dotenv
# Insurance runtime gate. Chat, /handoff and voice need ALL of these, and only after the
# hosted agents are deployed and verified. The project name must be one the code accepts
# (nw-insurance-claims or the original northwind-orders) and match the end of
# FOUNDRY_PROJECT_ENDPOINT, which is the value azd outputs (.../api/projects/<INSURANCE_PROJECT_NAME>).
INSURANCE_PROJECT_NAME=nw-insurance-claims
HOSTED_AGENTS_ENABLED=true
NORTHWIND_INSURANCE_AGENTS_READY=true
FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID=northwind-insurance-orchestrator
FOUNDRY_PROJECT_ENDPOINT=<project endpoint from azd, ending in /api/projects/nw-insurance-claims>

# Adjuster support: grounded RAG over nw-insurance-adjuster-support
ADJUSTER_SUPPORT_ENABLED=true
# ADJUSTER_SUPPORT_SEARCH_AUTH=entra_id           # or query_key plus ADJUSTER_SUPPORT_SEARCH_QUERY_KEY

# Document Intelligence: live Content Understanding (no authored fallback)
NORTHWIND_CU_ENABLED=true
NORTHWIND_CU_COMPLETION_DEPLOYMENT=<a gpt-4.1 or gpt-4o deployment name>
# NORTHWIND_CU_COMPLETION_MODEL=gpt-4.1           # or gpt-4o

# Voice Live (also needs the runtime gate above and a model from MODELS in apps/api/app/voice.py)
VOICE_LIVE_ENABLED=true
VOICE_LIVE_READY=true
VOICE_LIVE_MODEL=<native Voice Live model>
VOICE_LIVE_API_VERSION=2026-07-15                 # 2025-10-01, 2026-04-10 or 2026-07-15

# Copilot SDK harness (API-only: POST /harness/runs). Needs the claim-review MCP endpoint
COPILOT_HARNESS_ENABLED=true
MCP_CLAIM_REVIEW_URL=https://ca-insurance-review.<container-apps-domain>/mcp/

# In-app trace panel (unset = status "unavailable" with a note; no stub trace)
APPINSIGHTS_WORKSPACE_ID=<log-analytics-workspace-guid>

# Human-readable build stamp shown in the trace panel and GET /build
GIT_SHA=local-dev
BUILD_ENV=local

# AI Gateway routing (off by default). Needs `az login` as an identity holding the
# "AI Gateway Models User" role on ai-gateway-northwind. See docs/ai-gateway.md.
AI_GATEWAY_MODEL_ENDPOINT=https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/
AI_GATEWAY_MODEL_PROVIDER=aisvc-zurlo-northwind
# AI_GATEWAY_REQUIRED=true                        # refuse direct Foundry model calls
```

Cosmos DB is reachable only through a private endpoint in the Bicep, so from a laptop the
Cosmos-backed routes (`GET /hitl`, `POST /insurance/actions/draft`, conversations, harness
runs, voice tickets, the `/api/v1` workbench) fail until `COSMOS_ENDPOINT` points at an
account you can reach. Without `COSMOS_ENDPOINT`, `POST /insurance/actions/draft` says no
draft was saved. For those flows run the portal against the deployed API instead (set
`VITE_API_BASE_URL` to its URL). `docs/architecture.md` lists every switch in its
[feature-switch table](./architecture.md#runtime-readiness-and-feature-switches);
`apps/api/app/settings.py` is the full reference, and it also still reads the deprecated
names that older azd environments carry (the canonical name wins).

## Run the checks CI runs

```powershell
$env:APPLICATIONINSIGHTS_CONNECTION_STRING = ""
python -m pytest apps\api\tests -q                  # needs the generated PDFs from setup
cd apps\web-portal; npm run typecheck; npm test; npm run build
cd ..\web-voice;    npm run build
```

CI also runs a fixed list of `scripts\tests` and compiles the Bicep (see
`.github\workflows\ci.yml`). Not in CI but worth running when you touch them:
`python -m pytest agents\tests -q` (install `agents\hosted\requirements.txt` first),
`python -m pytest evals\tests -q` (`evals\requirements.txt`), and
`python evals\smoke_eval.py --self-check`. Lint with
`python -m ruff check apps\api evals pipelines data\seed`.

## Common gotchas

- **`ERR_CONNECTION_RESET` on every endpoint** → wslrelay is on `:8000`. Use port 8001
  (the scripts do this automatically) or kill wslrelay.
- **`localhost` fails, `127.0.0.1` works** → IPv6 vs IPv4 mismatch. Either bind uvicorn
  with `--host ::` or point the frontend at `127.0.0.1` explicitly (`VITE_API_BASE_URL`).
- **"My account is unavailable" in the portal** → you are on the business-first shell;
  set `VITE_INSURANCE_BUSINESS_ENABLED=false` (and restart `npm run dev`).
- **Claims Concierge chat says insurance agents are not available** → `/insurance/chat`
  returns `503` until the runtime gate above is complete; Claims / Policies still works.
- **Document Intelligence says the synthetic PDF bytes are not packaged** → run
  `python data\seed\generate.py`; extraction itself needs the Content Understanding
  settings.
- **Trace panel says live telemetry is unavailable** → set `APPINSIGHTS_WORKSPACE_ID`,
  and note that ingestion into Application Insights takes a few minutes.

## Docker-compose alternative

`docker compose up --build` starts the API on `:8000` and the two web apps on `:5173` and
`:5174` (built by `Dockerfile.local`, which defaults the portal to the department
workspace). It needs the `.env` file from `scripts\fetch-keys.ps1`, and on Windows you will
hit the wslrelay problem on `:8000`. Prefer the terminals above.
