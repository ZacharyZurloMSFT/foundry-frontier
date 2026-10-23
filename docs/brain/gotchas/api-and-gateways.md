# API and gateways

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
changing the API surface, gateway routing, the lock-down, keys or Foundry connections. Keep each entry short and
cite a source.

- **Changing the API has two follow-ups.** After changing the FastAPI surface, run
  `python scripts/export-openapi.py` and commit the outputs under `infra/live/apim/specs/` (CI's
  `test_export_openapi.py` compares the gateway-visible surface and the operations table; `--check` is the strict
  byte comparison). Adding an APIM stack or resource type is a deliberate boundary change: update
  `infra/live/apim-apis.bicep`, the allow-list and role in `scripts/apim-release.py` and
  `scripts/configure-github-oidc.py`, and the tests together. Source:
  [api-management.md](../../api-management.md#changing-the-api).
- **APIM plans are noisy.** Every API, policy and the logger show as *Modify* and product links as *Create* even
  when nothing changed. The real change signal is the diff of
  `infra/live/apim/specs/northwind-api.openapi.json`, and every entry must still fall inside the
  `apim-release.py` allow-list. Source: [api-management.md](../../api-management.md#repository-visibility).
- **A missing required query parameter returns 404 through APIM**, not the backend's 422, because OpenAPI import
  puts required query parameters into the URL template. Source:
  [api-management.md](../../api-management.md#findings-that-shaped-the-design-verified-live-on-scratch-apis).
- **The claim-review MCP cannot go through the AI Gateway.** The gateway drops client headers, and the claim-review
  server binds `x-workflow-id` to the workflow. Route it through `apim-northwind`; it can stay direct only while its
  app is not locked behind the gateway guard, and a locked app rejects direct calls with 401. For one hosted role,
  `MCP_GATEWAY_CONNECTION` and an `AGENT_MCP_GATEWAY` entry are mutually exclusive (startup error). Sources:
  [ai-gateway.md](../../ai-gateway.md#two-gateways), [ai-gateway.md](../../ai-gateway.md#findings-to-act-on).
- **AI Gateway model names are `<provider>/<registration>`.** The registration is the deployment name lowercased
  with every character outside `[a-z0-9-]` replaced by `-` (`gpt-5.4` becomes `gpt-5-4`); bare deployment names
  return 404, and `AGENT_MODEL_GATEWAY_DEPLOYMENT` must match the selected deployment. Prompt agents cannot use
  these aliases (Foundry rejects them as model ids), so their model calls stay direct and
  `AI_GATEWAY_MODEL_CONNECTION` should stay unset. Sources:
  [architecture.md](../../architecture.md#model-gateway-opt-in),
  [ai-gateway.md](../../ai-gateway.md#findings-to-act-on) (finding 9).
- **Lock an app last, and prove the lock.** Move every caller first, then release a guard-capable image, then enable
  the guard. An image without `gateway_guard.py` silently ignores the guard variables, and a full-Bicep provision
  drops them, so run `apim-smoke.py --stack <stack> --expect-lockdown` after any change. Sources:
  [runbook step 8](../../api-management.md#rollout-runbook), [operations](../../api-management.md#operations).
- **Foundry calls a connection's *target*.** Prompt agents need one connection per MCP endpoint with the exact URL
  (a base URL gets a 404); hosted agents read the key themselves, so one connection on the gateway base URL covers
  both (`connection_covers` in [`runtime.py`](../../../agents/hosted/runtime.py)). Connection names are
  account-wide. Source: [runbook step 6](../../api-management.md#rollout-runbook).
- **One key, several copies.** The `insurance-agents` key sits in a Foundry connection, an API secret and the AI
  Gateway credentials, and hosted agents read it at start-up: rotate every copy, then restart or re-version the
  agents. Never commit or print a key. Source: [operations](../../api-management.md#operations).
