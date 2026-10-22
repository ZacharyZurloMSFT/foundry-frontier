# AI Gateway routing

How Northwind sends model inference and MCP traffic through the Azure API Management **AI Gateway tier** instance `ai-gateway-northwind`, what is verified live, what is deliberately not routed, and how to roll it out and roll it back.

> Status: the repository changes are implemented and **off by default** (a fresh `azd up` is unchanged). Live changes (tool server registration, role grants, connections, new agent versions, app settings, enforcement) are **not applied automatically**; each is a dry-run-by-default script described in the [runbook](#runbook). The live state in this document was read on 2026-10-22; the only live writes made while preparing it were temporary canary tool servers and a temporary role assignment, both deleted again.

## Scope

| In scope | Out of scope (stay direct) |
|---|---|
| Foundry hosted agents and prompt agents | Search knowledge-base planner and vectorizer calls to models |
| `apps/api` model calls (safety, bake-off, adjuster support, Copilot harness) | Content Understanding, Voice Live (`gpt-realtime-2.1`) |
| MCP servers used by agents (insurance lookup, insurance KB) | Foundry evaluations, red-team, optimizer, fine-tuning |
| | GitHub Models smoke tier, eval/seed scripts |
| | Agent-to-agent calls (Agent Service traffic is neither model nor MCP) |

## The gateway (verified 2026-10-22, read-only plus temporary canaries)

- Resource `Microsoft.ApiManagement/service/ai-gateway-northwind`, SKU **`AIGateway`**, East US 2, system-assigned identity, public network access enabled. Classic APIM entities (APIs, backends, products, policies) are **not manageable** on this tier (`MethodNotAllowedInPricingTier`); the control plane is `workspaces/default/{modelProviders,models,toolServers}` and `apiKeys`, ARM `2025-09-01-preview`, or the preview `az aigateway` CLI.
- Model provider `aisvc-zurlo-northwind` (Foundry, managed identity). Registered models and the alias callers must send as `model`:

| Deployment | Registration | `model` value | Endpoints |
|---|---|---|---|
| gpt-4o | gpt-4o | `aisvc-zurlo-northwind/gpt-4o` | chat completions, responses |
| gpt-5.4 | gpt-5-4 | `aisvc-zurlo-northwind/gpt-5-4` | chat completions, responses |
| gpt-5.4-mini | gpt-5-4-mini | `aisvc-zurlo-northwind/gpt-5-4-mini` | chat completions, responses |
| gpt-5.6-luna | gpt-5-6-luna | `aisvc-zurlo-northwind/gpt-5-6-luna` | chat completions, responses |
| Kimi-K2.6 | kimi-k2-6 | `aisvc-zurlo-northwind/kimi-k2-6` | chat completions |
| text-embedding-3-small | text-embedding-3-small | `aisvc-zurlo-northwind/text-embedding-3-small` | embeddings |

  Alias rule: `<provider>/<registration>`, registration = deployment name lowercased with every character outside `[a-z0-9-]` replaced by `-`. Bare names (`gpt-5.4`, `gpt-5-4`) return 404.
- Runtime URLs: models `https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/`; MCP `https://ai-gateway-northwind.azure-api.net/default/toolservers/<server>/mcp`.
- Auth: Entra bearer token for scope `https://azure-api.net/aigateway/.default` with the data-plane roles below, or a gateway runtime key in the `api-key` header (gateway-wide: it can call every model and tool server). Model traffic uses Entra tokens. Provider-executed MCP from hosted and prompt agents uses a dedicated runtime key held in a project connection, because the tool's headers are fixed when the agent starts and an Entra token would expire (see finding 6).

| Role | Id | Grants |
|---|---|---|
| AI Gateway Models User | `a6b68e3d-a1f0-4c60-8678-237c050cf4ea` | invoke models |
| AI Gateway Tools User | `dce03f8d-857f-4e40-a90d-67770fa6ecd2` | invoke tool servers |
| AI Gateway User | `caad7a21-134e-43f4-a550-c5ffc17e051c` | both |

- MCP: the gateway terminates MCP itself (protocol `2025-06-18`, stateless, JSON or SSE) and federates backend servers. **Tools are namespaced `<namespace>_<tool>`** (`lookup` + `lookup_policy` = `lookup_lookup_policy`).
- **The gateway does not forward client request headers to MCP backends.** Verified by sending `x-harness-workflow` through the gateway to the work-order server: the backend never saw it.

## Live inventory

| Item | State |
|---|---|
| Foundry projects | `nw-insurance-claims` (default), `nw-insurance-documents`, `nw-insurance-support`, `nw-insurance-contact-center` |
| Hosted agents (`nw-insurance-claims`) | `northwind-insurance-{orchestrator,triage,policy-claim-lookup,policy-wording,claim-action-draft}` v1; models **already via gateway** (`ORDERS_MODEL_GATEWAY_*`, manager `.../gpt-5-4`, others `.../gpt-4o`); MCP **direct** |
| Prompt agents | `northwind-claims-triage` (`nw-insurance-documents`), `northwind-intent-classifier` (`nw-insurance-contact-center`): gpt-4o, no tools, model **direct** |
| API app | `ca-insurance-api`: planner gpt-5.4, worker gpt-5.4-mini, prompt-agent gpt-4o, bake-off `gpt-5.4,gpt-5.4-mini`, harness gpt-5.4; all model calls **direct**; no `AI_GATEWAY_*` settings |
| MCP apps | `ca-insurance-lookup` (read-only synthetic lookups), `ca-insurance-review` (work-order drafts), legacy `ca-mcp-tech-lookup`, `ca-mcp-work-order`; external ingress, no IP restrictions, no auth |
| Gateway tool servers | working KB servers `northwind-{claims,contact-center,hr,orders}-knowledge`; `northwind-api` (OpenAPI facade, 23 tools); **stale** `northwind-order-lookup` and `northwind-resolutions` (retired `bluecliff` FQDNs, HTTP 404); **missing** insurance lookup, insurance KB and review |
| Foundry connections | one `RemoteTool` KB connection `nw-insurance-claims-kb-mcp` (CustomKeys, Search admin key); no `ApiManagement` model connection |
| Account | `disableLocalAuth=true`, public network enabled, no private endpoints |
| RBAC | Models User on the gateway for the 5 hosted agent identities (plus 5 orphaned identities of the removed `northwind-orders` project); the app UAMI `id-app-zurlo-northwind` has **no** gateway role yet; hosted identities also hold Cognitive Services OpenAI User (account) and Foundry User (project) |

## Routing design

| Traffic | Route | Notes |
|---|---|---|
| Hosted agent inference | client-side gateway (`ORDERS_MODEL_GATEWAY_*`) | `AI_GATEWAY_REQUIRED=true` makes direct paths a startup error |
| Hosted provider-executed MCP (order lookup, policy lookup) | gateway tool server via Responses `mcp` tool; runtime key read from a project connection at startup | `ORDERS_MCP_GATEWAY`, `AI_GATEWAY_MCP_REQUIRED` |
| Hosted resolution draft / work-order MCP | **direct (documented exception)** | needs application-bound headers the gateway drops |
| Prompt agent inference | Foundry model connection (`ApiManagement`), model string `<connection>/<alias>` | connection payload for this tier is unverified; canary first |
| Prompt agent knowledge-base MCP | the KB's gateway tool server, connection holding the runtime key | `AI_GATEWAY_MCP_KB_ROUTES`; a KB without a route stays on Search unless `AI_GATEWAY_MCP_REQUIRED` |
| API app inference | `apps/api/app/model_gateway.py` with Entra token | `AI_GATEWAY_MODEL_*` settings; `GET /gateway/status` |
| Copilot harness inference | SDK provider pointed at the gateway | same settings |

### Configuration contract

All optional; empty means unset and keeps the current direct behaviour (fresh `azd up` is unchanged).

| Setting | Purpose |
|---|---|
| `AI_GATEWAY_REQUIRED` | models must use the gateway (hosted agents, API app, prompt-agent creation); fail closed |
| `AI_GATEWAY_MCP_REQUIRED` | hosted order/policy lookup MCP and prompt-agent knowledge-base MCP must use the gateway; fail closed |
| `ORDERS_MODEL_GATEWAY_ENDPOINT/_MODEL/_DEPLOYMENT/_SCOPE` | existing hosted-agent contract |
| `ORDERS_MCP_GATEWAY` | hosted agents: JSON per role: `url`, `tool_prefix`, `connection`, optional `forwards_headers` |
| `AI_GATEWAY_MODEL_ENDPOINT`, `AI_GATEWAY_SCOPE`, `AI_GATEWAY_MODEL_PROVIDER`, `AI_GATEWAY_MODEL_ALIASES` | API app |
| `AI_GATEWAY_MODEL_CONNECTION` | Foundry model connection used by prompt agents |
| `AI_GATEWAY_MCP_KB_ROUTES` | prompt agents: JSON keyed by Search knowledge-base name, `{"toolserver", "connection", "tool_prefix"?}` (prefix defaults to `knowledge_`) |

## Findings to act on

1. **Work-order MCP cannot be routed yet.** Its `x-orders-workflow` / `x-harness-workflow` binding is an application-bound proof; the gateway drops it. Options: ask for header forwarding on the AI Gateway tier, or move the binding into a server-verifiable argument minted by application code (a security redesign that needs an explicit decision). Until then it stays direct and this change registers no gateway tool server for it (the existing stale `northwind-resolutions` registration is finding 3).
2. **`northwind-api` tool server publishes HITL decide and delete operations** (23 tools). The repository's golden rules say approval and business execution are never agent tools, and every gateway key or Tools User can call every published tool. Remove or narrow that registration.
3. **Two stale tool servers** point at retired hosts; fix or delete them deliberately.
4. **RBAC alone cannot force gateway-only inference**: `Foundry User` includes `Microsoft.CognitiveServices/*` and agents need it. Enforcement here means code fail-closed, removing redundant account-scope OpenAI User from hosted identities, and detecting bypass; a network restriction is a separate decision.
5. Five orphaned agent identities from the removed `northwind-orders` project still hold Models User and OpenAI User.
6. **The runtime key is gateway-wide.** A gateway API key can call every model and every published tool server; it cannot be scoped to one tool server. The runbook therefore creates a dedicated named key for MCP (`northwind-mcp-runtime`) so it can be rotated or deleted without touching the built-in `master` key. Replacing the key with per-identity Entra tokens (project managed identity on the connection, or a token minted per request by the agent) would remove the stored secret; that path is not implemented here and was only verified manually with a user token.
7. **The gateway has no diagnostic settings**, so `ApiManagementGatewayLogs` and `ApiManagementGatewayLlmLog` are empty and gateway-side usage cannot be queried. The Foundry account sends `allLogs` only to a policy-assigned governance workspace (`setByPolicy-MCAPSGovernance`), which the team may not be able to query. Add a diagnostic setting on the gateway to a workspace you own before relying on the bypass-detection queries.
8. The MCP container apps (`ca-insurance-lookup`, `ca-insurance-review`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`) have public ingress, no authentication and no IP restrictions, so routing through the gateway does not stop anyone calling them directly. Locking them down (Container Apps authentication accepting only the gateway identity, then switching the tool server credential from `none` to `managedIdentity`) is a gated manual decision, not done here.

## Runbook

Run everything from the repository root after `az login`. `plan`, `audit`, `verify` and `--dry-run` are read-only; a change needs `--yes` (release, grants, enforce) or `--apply` (connections, hosted deploy). Grants and enforcement write receipts to the git-ignored `.release-state/` folder; keep them, rollback needs them. The repo scripts call Azure through `az rest` only; the `aigateway` CLI extension is needed just to manage gateway keys (step 3).

Order is **route, verify, then enforce**: nothing is enforced until the traffic it would block is shown to flow through the gateway. Steps 1-3 are prerequisites; steps 4-6 are independent of each other and can ship one at a time; step 8 comes last.

| Step | Access the operator needs |
|---|---|
| 0 baseline, every `plan`/`audit` | Reader on the gateway and the Foundry account; Foundry User on the project for `discover-agents` |
| 1 tool servers | API Management Service Contributor on the gateway; AI Gateway Tools User for `verify` |
| 2 grants | User Access Administrator or Role Based Access Control Administrator on the gateway |
| 3 connections | Contributor on the Foundry account and project |
| 4 API app | Contributor on `ca-insurance-api` |
| 5-6 agents | the deploy identity already used for `deploy-orders-hosted.py` / `create-prompt-agents.py` |
| 8 enforce | Role Based Access Control Administrator on the Foundry account |

### 0. Baseline (read-only)

```powershell
python scripts/ai-gateway-release.py plan --out plan.json
python scripts/ai-gateway-grants.py discover-agents `
  --project-endpoint https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims `
  --out principals.json
python scripts/ai-gateway-grants.py plan --principals principals.json
python scripts/ai-gateway-grants.py plan --preset app-uami
python scripts/ai-gateway-enforce.py audit --out audit.json
```

On 2026-10-22 this showed two tool servers to create, two stale ones, every model registered, Models User already held by the five hosted agent identities and **not** by the app identity, key authentication disabled on the account but public network access on, and four public unauthenticated MCP apps.

### 1. Register the managed tool servers

`infra/live/ai-gateway.json` is the desired state: `northwind-insurance-lookup` (the read-only lookup app, no credential) and `northwind-insurance-knowledge` (Search KB `northwind-insurance-kb`, the gateway injects the Search `api-key`). The other servers in the manifest are only reported, never changed.

```powershell
python scripts/ai-gateway-release.py plan --out plan.json      # review plan.json: it is secret-free
$env:SEARCH_ADMIN_KEY = '<Search admin key>'                   # only the insurance KB server needs it; this shell only
python scripts/ai-gateway-release.py apply --plan plan.json --yes --receipt .release-state/ai-gateway-release.json
Remove-Item Env:SEARCH_ADMIN_KEY
python scripts/ai-gateway-release.py verify
```

`apply` re-reads the gateway and refuses when the live diff no longer matches the reviewed plan; it creates or updates only the managed servers and has no delete operation. `verify` sends `initialize` and `tools/list` only (no tool is called) and expects `lookup_lookup_policy`, `lookup_lookup_claim` and `knowledge_knowledge_base_retrieve`.

### 2. Grant the data-plane roles

```powershell
python scripts/ai-gateway-grants.py plan --preset app-uami
python scripts/ai-gateway-grants.py apply --preset app-uami --yes
```

The preset gives the API app identity **AI Gateway Models User** only: the app calls models, not tool servers, through the gateway. The hosted agents already hold Models User (`apply --principals principals.json --yes` is a no-op for them) and new versions get it from `deploy-orders-hosted.py --grant-runtime-roles`. **No agent needs AI Gateway Tools User**, because provider-executed MCP authenticates with the connection's runtime key; grant Tools User only to people or pipelines that run `release verify`.

### 3. Foundry connections

The runtime key is gateway-wide (finding 6), so create a dedicated named key rather than reusing `master`:

```powershell
az extension add --name aigateway --version 1.0.0b6
az aigateway api-key create -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime --display-name "Northwind MCP runtime"
az aigateway api-key list-secrets -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime   # copy the primary value
$env:AI_GATEWAY_RUNTIME_KEY = '<primary value>'                # never print or commit it
$project = '/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims'
python scripts/connect-mcp-gateway.py --project-arm-id $project --toolserver northwind-insurance-lookup --connection-name nw-insurance-claims-gateway-mcp
python scripts/connect-mcp-gateway.py --project-arm-id $project --toolserver northwind-insurance-knowledge --connection-name nw-insurance-claims-gateway-kb
# the dry runs print the matching ORDERS_MCP_GATEWAY entry; add --apply to each command to create the connections
Remove-Item Env:AI_GATEWAY_RUNTIME_KEY
```

Prompt agents need a model connection as well. **Its payload for the AI Gateway tier is not verified live**, so create it and run one canary before anything else uses it:

```powershell
python scripts/connect-model-gateway.py --name nw-gateway-models            # dry run: prints the PUT and the model strings
python scripts/connect-model-gateway.py --name nw-gateway-models --apply
```

Canary: create one throwaway prompt agent (`zz-gateway-canary`) with `model` `nw-gateway-models/aisvc-zurlo-northwind/gpt-4o`, send it one message in the Foundry playground, confirm it answered and delete it. The identity Foundry calls the gateway with (the project or account managed identity) needs Models User on the gateway; the script does not grant it. Prompt agents use chat completions only, so `Kimi-K2.6` works there but not through the Responses API.

### 4. API app

```powershell
az containerapp update -g rg-northwind-apps -n ca-insurance-api --set-env-vars `
  AI_GATEWAY_MODEL_ENDPOINT=https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/ `
  AI_GATEWAY_MODEL_PROVIDER=aisvc-zurlo-northwind
```

The app is single-revision, so this creates and activates a new revision. Check `GET /gateway/status` and exercise a safety check and the bake-off (see [Verification](#verification)); then add `AI_GATEWAY_REQUIRED=true` so a missing or broken gateway configuration is an error rather than a silent direct call. The planner, worker, prompt-agent and bake-off models all map to registered aliases (`gpt-5.4`, `gpt-5.4-mini`, `gpt-4o`); the app never calls gateway MCP because the work-order server stays direct.

### 5. Hosted agents

The five agents already send models through the gateway. A new version adds the fail-closed flags and, for the two lookup roles, MCP routing. Start from the live version's non-secret settings and add:

```json
{
  "common": {
    "AI_GATEWAY_REQUIRED": "true",
    "AI_GATEWAY_MCP_REQUIRED": "true",
    "ORDERS_MCP_GATEWAY": "{\"orderlookup\": {\"url\": \"https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp\", \"tool_prefix\": \"lookup_\", \"connection\": \"nw-insurance-claims-gateway-mcp\"}, \"policylookup\": {\"url\": \"https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-knowledge/mcp\", \"tool_prefix\": \"knowledge_\", \"connection\": \"nw-insurance-claims-gateway-kb\"}}"
  }
}
```

```powershell
$project = '/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims'
python scripts/deploy-orders-hosted.py --role orderlookup --business-domain insurance `
  --project-endpoint https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims `
  --project-arm-id $project --tenant <tenant id> --subscription 137c905b-7734-479e-acc5-ab8222462936 `
  --requirements agents/hosted/requirements.txt --config hosted-gateway.json --output .release-state/orderlookup.json
```

That is a dry run; it validates the gateway settings before any network call and prints the plan. Add `--apply` to deploy. Deploy `orderlookup` and `policylookup` first and run an end-to-end claim through them (the canary); then the other three with `AI_GATEWAY_REQUIRED` only. `resolutiondraft` keeps its direct work-order MCP. The guard in this script now accepts both `northwind-orders` and `nw-insurance-claims`; the endpoint and the ARM id must name the same project.

### 6. Prompt agents

```powershell
$env:FOUNDRY_PROMPT_AGENT_DEPLOYMENT = 'gpt-4o'                    # plus the usual azd environment values
$env:AI_GATEWAY_MODEL_CONNECTION = 'nw-gateway-models'
$env:AI_GATEWAY_MCP_KB_ROUTES = '{"nw-hr-kb": {"toolserver": "northwind-hr-knowledge", "connection": "nw-hr-kb-gw"}}'
python scripts/create-prompt-agents.py --dry-run
```

The dry run prints every planned agent, for example `model=nw-gateway-models/aisvc-zurlo-northwind/gpt-4o; tools=mcp:nw-hr-kb@gateway`, and sends nothing. Each routed knowledge base needs its own project connection (`connect-mcp-gateway.py --toolserver northwind-hr-knowledge --connection-name nw-hr-kb-gw`). The four existing knowledge servers map to `nw-hr-kb`, `nw-claims-kb`, `nw-contact-center-kb` and `northwind-kb` (`northwind-{hr,claims,contact-center,orders}-knowledge`). With `AI_GATEWAY_REQUIRED=true` the script exits before creating anything when the model connection is missing. With `AI_GATEWAY_MCP_REQUIRED=true` an agent whose knowledge base has no route (or whose yaml `mcp` tool is not on the gateway) is **not created** and the run exits non-zero, while agents that comply are still created, so run `--dry-run` first: it lists every such agent (marked `x`, exit code 1) before anything is sent. Re-run without `--dry-run` once the canary passed.

### 7. Verify

Follow [Verification](#verification) before moving on.

### 8. Enforce (gated; last)

```powershell
python scripts/ai-gateway-enforce.py plan --principals principals.json --out enforce-plan.json
python scripts/ai-gateway-enforce.py apply --principals principals.json --yes --expect-plan-sha256 <planSha256 from enforce-plan.json>
```

This removes only the account-scope **Cognitive Services OpenAI User** assignment of each listed hosted-agent identity that already holds Models User on the gateway, and writes a rollback receipt. It never removes Foundry User, which the agents need and which still permits direct inference, so the real controls are the fail-closed flags plus the bypass-detection queries. These stay manual decisions the scripts only report: restricting the account's network (public network access is on), Container Apps authentication for the MCP apps, deleting the orphaned identities, and the `northwind-api` and stale tool servers (findings 2, 3, 5, 8).

## Rollback

Each step undoes independently, newest first.

| Step | Undo |
|---|---|
| 8 enforce | `python scripts/ai-gateway-enforce.py rollback --receipt <receipt> --yes` recreates exactly the removed assignments |
| 6 prompt agents | re-run `create-prompt-agents.py` with the `AI_GATEWAY_*` variables unset: a new version calls the deployment and Search directly; delete the connections with `az rest --method delete --url "https://management.azure.com<connection id>?api-version=2025-10-01-preview"` |
| 5 hosted agents | deploy a new version from the previous config (flags removed); hosted versions are immutable, so there is no in-place edit |
| 4 API app | `az containerapp update -g rg-northwind-apps -n ca-insurance-api --remove-env-vars AI_GATEWAY_REQUIRED AI_GATEWAY_MODEL_ENDPOINT AI_GATEWAY_MODEL_PROVIDER` (remove `AI_GATEWAY_REQUIRED` first) |
| 3 connections and key | delete the connections as above; `az aigateway api-key delete -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime --yes` |
| 2 grants | `python scripts/ai-gateway-grants.py revoke --receipt <receipt> --yes` deletes only what that receipt created |
| 1 tool servers | `az aigateway mcp delete -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-insurance-lookup --yes` (and `northwind-insurance-knowledge`); the backend apps are untouched |

Removing the gateway flags is enough to return the repository code to the direct paths; none of the changes alters `azd up`.

## Verification

Repository (no cloud or model calls; the commands CI runs):

```powershell
python data/seed/generate.py --domain insurance                    # synthetic inputs, needs reportlab
python -m pytest apps/api/tests scripts/tests/test_ai_gateway_common.py scripts/tests/test_ai_gateway_release.py `
  scripts/tests/test_ai_gateway_grants.py scripts/tests/test_ai_gateway_enforce.py scripts/tests/test_model_gateway_connection.py `
  scripts/tests/test_mcp_gateway_connection.py scripts/tests/test_prompt_agents_gateway.py `
  scripts/tests/test_validate_reuse_gateway.py scripts/tests/test_deploy_hosted_gateway_grants.py -q
python -m pytest agents/tests -q                                    # isolated venv from agents/hosted/requirements.txt; not in CI
python evals/smoke_eval.py --self-check
```

Live, after each routing step:

- **Tool servers:** `python scripts/ai-gateway-release.py plan` shows no pending change, and `verify --include-observed --strict` lists only the expected tools (add `--include-observed` to see whether `northwind-api` still publishes decide/delete operations).
- **Grants:** `python scripts/ai-gateway-grants.py plan --principals principals.json` reports everything already present.
- **API app:** `GET https://ca-insurance-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/gateway/status` returns `"enabled": true`, `"required": true` once enforced, `"features"` all `"gateway"` and `"problem": null`, plus the deployment to alias map (`gpt-5.4` to `aisvc-zurlo-northwind/gpt-5-4`). Traces carry `gateway.route` and `gateway.model_alias`, and the trace panel marks each model call the gateway served with an **AI Gateway** badge showing the alias.
- **Hosted agents:** responses carry `model_gateway_required` and `mcp_route` metadata; a version without the flags omits them.
- **Prompt agents:** `create-prompt-agents.py --dry-run` shows `@gateway` on every MCP tool and a `nw-gateway-models/...` model string on every agent.
- **Bypass detection:** `python scripts/ai-gateway-enforce.py audit` prints ready-to-run KQL (`foundryNotFromGateway`, `inScopeIdentitiesBypassing`, `gatewayTraffic`, `gatewayModelUsage`). They are templates built on the standard diagnostics schemas and were not run against a live workspace; the gateway has no diagnostic settings today (finding 7), so add one first.
