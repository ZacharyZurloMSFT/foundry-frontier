# Local dev — running the demo on your laptop

## TL;DR

Two terminals, each ~15 seconds to start. The demo speaks to the same Azure
Foundry / Search / Cosmos / Storage your `azd up` provisioned; nothing here
runs Foundry locally.

### Terminal 1 — API (FastAPI + orchestrator)

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
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
$env:VITE_REPO_URL     = "https://github.com/<you>/foundry-frontier"   # optional; makes trace-panel SHAs clickable
npm run dev
```

Serves on `http://localhost:5173`.

### Terminal 3 (optional) — web-voice

```powershell
cd apps\web-voice
$env:VITE_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev
```

Serves on `http://localhost:5174`.

## Sanity-check the API from another shell

```powershell
curl.exe -sS http://127.0.0.1:8001/health
curl.exe -sS http://127.0.0.1:8001/build
curl.exe -sS http://127.0.0.1:8001/bakeoff/models
curl.exe -sS http://127.0.0.1:8001/safety/attacks
```

All four should return JSON in under a second.

## Optional env vars

Add to `apps\api\.env` (or the repo-root `.env`) — everything else has
graceful fallbacks, so the demo runs even if these are unset.

```dotenv
# A1 Model bake-off — deployments MUST exist in your Foundry project
FOUNDRY_BAKEOFF_DEPLOYMENTS=gpt-5.4,gpt-5.4-mini

# A5 Foundry Guardrails — deployment with a Guardrail policy attached
# (defaults to the worker deployment, which already has Microsoft.DefaultV2)
FOUNDRY_GUARDRAIL_DEPLOYMENT=gpt-5.4-mini

# A8/B6 real trace panel (unset = canned stub trace)
APPINSIGHTS_WORKSPACE_ID=<log-analytics-workspace-guid>

# B6 human-readable build stamp shown in the sidebar
GIT_SHA=session-2-local
BUILD_ENV=local

# AI Gateway routing (off by default). Needs `az login` as an identity holding
# the "AI Gateway Models User" role on ai-gateway-northwind. See docs/ai-gateway.md.
AI_GATEWAY_MODEL_ENDPOINT=https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/
AI_GATEWAY_MODEL_PROVIDER=aisvc-zurlo-northwind
# AI_GATEWAY_REQUIRED=true   # refuse direct Foundry model calls
```

## Common gotchas

- **`ERR_CONNECTION_RESET` on every endpoint** → wslrelay is on `:8000`.
  Use port 8001 (the scripts do this automatically) or kill wslrelay.
- **`localhost` fails, `127.0.0.1` works** → IPv6 vs IPv4 mismatch. Either
  bind uvicorn with `--host ::` or point the frontend at `127.0.0.1`
  explicitly (VITE_API_BASE_URL).
- **`max_tokens` unsupported errors** → gpt-5.x + o-series need
  `max_completion_tokens`. The bake-off and guardrail modules already
  handle this; only affects any custom code you add.
- **`DeploymentNotFound`** in bake-off column → that deployment doesn't
  exist in your Foundry project. Update `FOUNDRY_BAKEOFF_DEPLOYMENTS`
  to match what's actually in Build → Models + deployments.

## Docker-compose alternative

`docker-compose up --build` still works for the older `:8000` layout, but
you'll hit the wslrelay problem on Windows. Prefer the two-terminal
approach above until we bake `--host 127.0.0.1 --port 8001` into the
Dockerfile.
