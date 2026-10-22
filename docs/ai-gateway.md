# AI Gateway routing

How Northwind sends model inference and MCP traffic through the Azure API Management **AI Gateway tier** instance `ai-gateway-northwind`, what is verified live, what is deliberately not routed, and how to roll it out and roll it back.

> Status: the repository changes are implemented and **off by default** (a fresh `azd up` is unchanged). The live gateway was changed on 2026-10-22 at the owner's direction: two stale tool servers were removed, the three insurance MCP tool servers were registered, and diagnostic settings were enabled on the gateway and the Foundry account (details below). The code is merged (PR #61) and its release put it on the northwind-stack apps, where it is inactive; the insurance apps and the agents are **not deployed** (see [Deployment status](#deployment-status)). Everything else live (role grants, connections, new agent versions, app settings, enforcement) is **not applied automatically**; each is a dry-run-by-default script described in the [runbook](#runbook). Earlier inspection used only read-only calls plus temporary canaries and a temporary role assignment, all deleted again.

## Scope

| In scope | Out of scope (stay direct) |
|---|---|
| Foundry hosted agents (models and MCP) and prompt-agent knowledge-base MCP | Search knowledge-base planner and vectorizer calls to models |
| `apps/api` model calls (safety, bake-off, adjuster support, Copilot harness) | Content Understanding, Voice Live (`gpt-realtime-2.1`) |
| Insurance MCP servers used by agents: policy/claim lookup, insurance knowledge base, claim-action review | Foundry evaluations, red-team, optimizer, fine-tuning |
| | The retail orders MCP apps (`ca-mcp-tech-lookup`, `ca-mcp-work-order`): dropped from scope at the owner's direction, neither registered nor changed |
| | Prompt-agent model calls: Foundry cannot address the gateway's `provider/registration` aliases (finding 9) |
| | GitHub Models smoke tier, eval/seed scripts |
| | Agent-to-agent calls (Agent Service traffic is neither model nor MCP) |

The AI Gateway is not the only gateway in this repository. [`docs/api-management.md`](./api-management.md) describes the classic API Management instance `apim-northwind`, which publishes the REST, WebSocket and MCP surfaces of the apps in `rg-northwind-apps` and **forwards client headers**. This document's AI Gateway tier instance fronts models and header-free MCP servers; header-bound MCP calls (the work-order proposal) belong on `apim-northwind`. See [Two gateways](#two-gateways).

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
- **`failureMode` is accepted but never returned.** The `2025-09-01-preview` service stored no `failureMode` for any registration, and a throwaway canary registered with the non-default `failOpen` came back without it (canary deleted, 2026-10-22). The manifest still declares `failClosed` as intent and sends it, but only the per-endpoint `required: true`, which the service does return, is observable. A missing live `failureMode` is therefore not reported as drift; a returned value that differs still is.

## Live inventory

| Item | State |
|---|---|
| Foundry projects | `nw-insurance-claims` (default), `nw-insurance-documents`, `nw-insurance-support`, `nw-insurance-contact-center` |
| Hosted agents (`nw-insurance-claims`) | `northwind-insurance-{orchestrator,triage,policy-claim-lookup,policy-wording,claim-action-draft}` v1; models **already via gateway** (`ORDERS_MODEL_GATEWAY_*`, manager `.../gpt-5-4`, others `.../gpt-4o`); MCP **direct** |
| Prompt agents | `northwind-claims-triage` (`nw-insurance-documents`), `northwind-intent-classifier` (`nw-insurance-contact-center`): gpt-4o, no tools, model **direct** |
| API app | `ca-insurance-api`, the manual `insurance-names-20261020` build (no gateway code; no workflow releases it): planner gpt-5.4, worker gpt-5.4-mini, prompt-agent gpt-4o, bake-off `gpt-5.4,gpt-5.4-mini`, harness gpt-5.4; all model calls **direct**; no `AI_GATEWAY_*` settings |
| MCP apps | `ca-insurance-lookup` (read-only synthetic lookups), `ca-insurance-review` (work-order drafts), legacy `ca-mcp-tech-lookup`, `ca-mcp-work-order`; external ingress, no IP restrictions, no auth |
| Gateway tool servers | registered by this change (2026-10-22): `northwind-insurance-lookup`, `northwind-insurance-knowledge` (the gateway holds a read-only Search query key), `northwind-insurance-review` (fails closed through the gateway, see finding 1); pre-existing KB servers `northwind-{claims,contact-center,hr,orders}-knowledge`; `northwind-api` (OpenAPI facade, 23 tools). The two stale servers `northwind-order-lookup` and `northwind-resolutions` (retired `bluecliff` FQDNs, HTTP 404) were **deleted** on 2026-10-22 |
| Diagnostics | gateway: `northwind-gateway-to-law` sends `GatewayLogs`, `GatewayLlmLogs`, `GatewayMCPLogs` and `AllMetrics` to Log Analytics workspace `log-zurlo-northwind` (resource-specific tables); Foundry account: `northwind-account-to-law` sends `RequestResponse` to the same workspace, next to the existing policy-assigned `setByPolicy-MCAPSGovernance` setting |
| Foundry connections | one `RemoteTool` KB connection `nw-insurance-claims-kb-mcp` (CustomKeys, Search admin key); no `ApiManagement` model connection |
| Account | `disableLocalAuth=true`, public network enabled, no private endpoints |
| RBAC | Models User on the gateway for the 5 hosted agent identities (plus 5 orphaned identities of the removed `northwind-orders` project); the app UAMI `id-app-zurlo-northwind` has **no** gateway role yet; hosted identities also hold Cognitive Services OpenAI User (account) and Foundry User (project) |

## Deployment status

Verified on 2026-10-22 after PR #61 merged (`87731ac`), from the workflow runs and read-only checks of Azure:

| Layer | State |
|---|---|
| Code on `main` | merged; CI passed |
| Northwind-stack apps (`ca-api`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`, `ca-web-tech`, `ca-web-voice`) | released by the app-release workflow (`deploy-apps.yml`), all five ready and smoke-tested. The gateway code is **inactive** (no `AI_GATEWAY_*` settings): `GET /northwind/gateway/status` through API Management returns `"enabled": false` |
| Insurance apps (`ca-insurance-*`) | **not deployed**: no workflow or script releases them (`scripts/release-app.py` accepts only the five apps above). They run manual builds of the unmerged `zacharyzurlo-microsoft-insurance-resource-cleanup` work (`ca-insurance-api` is image tag `insurance-names-20261020`), so `/gateway/status` is 404 and its model calls go direct. A `main` image is not a drop-in replacement (runbook step 4) |
| Gateway tool servers and diagnostics | applied live (see the inventory above) |
| Hosted agents, prompt agents, connections, role grants, enforcement | **not applied**: runbook steps 2, 3, 5, 6 and 8 |
| Infrastructure | unchanged: the plan is identical before and after the merge, and apply is a manual dispatch |

### Verified live (2026-10-22, after the table above)

- **Tool servers through the gateway:** `release verify` listed exactly the expected tools on all three servers, and read-only `lookup_policy`, `lookup_claim` and `knowledge_base_retrieve` calls succeeded (the review tool was not called). The temporary Tools User assignment used for this was revoked from its receipt.
- **The API's model client:** the real app, run in-process with the operator's identity against the live gateway (no deployed app was touched), reported `/gateway/status` enabled with the alias map, answered `/safety/check` (`foundry-guardrails via AI Gateway`) and streamed the two-model bake-off, all served by the gateway.
- **The hosted agents' MCP design:** one Responses call through the gateway, with an `mcp` tool pointed at a gateway tool server and a runtime key in the `api-key` header, made Foundry list the prefixed tool and execute it (`lookup_lookup_policy`, `knowledge_knowledge_base_retrieve`), and the model answered from the result. The canary keys were deleted afterwards; only `master` remains.
- **Prompt agents cannot use the gateway** for models (finding 9).

## Routing design

| Traffic | Route | Notes |
|---|---|---|
| Hosted agent inference | client-side gateway (`ORDERS_MODEL_GATEWAY_*`) | `AI_GATEWAY_REQUIRED=true` makes direct paths a startup error |
| Hosted provider-executed MCP (order lookup, policy lookup) | gateway tool server via Responses `mcp` tool; runtime key read from a project connection at startup | `ORDERS_MCP_GATEWAY`, `AI_GATEWAY_MCP_REQUIRED` |
| Hosted resolution draft / work-order MCP | **`apim-northwind` or direct, never the AI Gateway** | needs application-bound headers the AI Gateway drops; `northwind-insurance-review` is registered on the AI Gateway at the owner's request but rejects every call by design (finding 1) |
| Prompt agent inference | **direct (cannot be routed today)** | Foundry rejects the gateway's `provider/registration` aliases as model ids (finding 9) |
| Prompt agent knowledge-base MCP | the KB's gateway tool server, connection holding the runtime key | `AI_GATEWAY_MCP_KB_ROUTES`; a KB without a route stays on Search unless `AI_GATEWAY_MCP_REQUIRED` |
| API app inference | `apps/api/app/model_gateway.py` with Entra token | `AI_GATEWAY_MODEL_*` settings; `GET /gateway/status` |
| Copilot harness inference | SDK provider pointed at the gateway | same settings |

### Configuration contract

All optional; empty means unset and keeps the current direct behaviour (fresh `azd up` is unchanged).

| Setting | Purpose |
|---|---|
| `AI_GATEWAY_REQUIRED` | models must use the gateway (hosted agents, API app); fail closed. **Do not set it in the azd environment yet**: `postprovision` runs `create-prompt-agents.py`, which currently refuses to run when it is set (it demands a prompt-agent model connection that cannot work, finding 9). Set it per app or agent instead, as the runbook does |
| `AI_GATEWAY_MCP_REQUIRED` | hosted order/policy lookup MCP and prompt-agent knowledge-base MCP must use the gateway; fail closed |
| `ORDERS_MODEL_GATEWAY_ENDPOINT/_MODEL/_DEPLOYMENT/_SCOPE` | existing hosted-agent contract |
| `ORDERS_MCP_GATEWAY` | hosted agents: JSON per role: `url`, `tool_prefix`, `connection`, optional `forwards_headers` |
| `MCP_GATEWAY_CONNECTION` | hosted agents routing `orderlookup` / `resolutiondraft` through `apim-northwind` (the [API Management](./api-management.md) contract); mutually exclusive with `ORDERS_MCP_GATEWAY` for the same role |
| `AI_GATEWAY_MODEL_ENDPOINT`, `AI_GATEWAY_SCOPE`, `AI_GATEWAY_MODEL_PROVIDER`, `AI_GATEWAY_MODEL_ALIASES` | API app |
| `AI_GATEWAY_MODEL_CONNECTION` | Foundry model connection for prompt agents; **not usable with the AI Gateway tier** (finding 9), leave it unset |
| `AI_GATEWAY_MCP_KB_ROUTES` | prompt agents: JSON keyed by Search knowledge-base name, `{"toolserver", "connection", "tool_prefix"?}` (prefix defaults to `knowledge_`) |

### Two gateways

| | AI Gateway tier `ai-gateway-northwind` (this document) | Classic API Management `apim-northwind` ([api-management.md](./api-management.md)) |
|---|---|---|
| Fronts | models; header-free MCP (insurance lookup, knowledge bases, claim-action review) | REST, WebSocket and MCP surfaces of the apps in `rg-northwind-apps` |
| Client headers | **dropped** before the MCP backend | forwarded |
| Caller auth | Entra token (Models/Tools User) or gateway runtime key | subscription key `Ocp-Apim-Subscription-Key` in the `MCP_GATEWAY_CONNECTION` project connection |
| Agent settings | `AI_GATEWAY_*`, `ORDERS_MCP_GATEWAY` | hosted agents: `NORTHWIND_GATEWAY_*`, `MCP_GATEWAY_CONNECTION`; prompt agents: one connection per MCP endpoint, `MCP_GATEWAY_CONNECTION_TECH_LOOKUP` / `MCP_GATEWAY_CONNECTION_WORK_ORDER` |

For one hosted role the two routes are mutually exclusive: setting both `MCP_GATEWAY_CONNECTION` and an `ORDERS_MCP_GATEWAY` entry for `orderlookup` or `resolutiondraft` is a startup error. `policylookup` (the knowledge base) exists only on the AI Gateway, and `resolutiondraft` can only use `apim-northwind` or stay direct. The hosted response metadata `mcp_route` reports `ai-gateway`, `api-management` or `direct`, and `create-prompt-agents.py --dry-run` marks each MCP tool `@ai-gateway`, `@apim` or `@direct`; the `apim-northwind` connections are never attached to an AI Gateway URL.

## Findings to act on

1. **Work-order MCP cannot be routed through the AI Gateway.** Its `x-orders-workflow` / `x-harness-workflow` binding is an application-bound proof, and the gateway drops client headers. `ca-insurance-review` publishes only `draft_claim_action`, whose server requires `x-orders-workflow` to equal the `workflow_id` argument. `northwind-insurance-review` was registered on 2026-10-22 because the owner asked for every public insurance MCP server on the gateway; it therefore rejects each call by design. It fails closed and does not weaken the binding, but it is not a usable route. Consumers use `apim-northwind` (which forwards headers) or stay direct. A usable AI Gateway route needs either header forwarding on the tier or a server-verifiable argument minted by application code (a security redesign that needs an explicit decision).
2. **`northwind-api` tool server publishes HITL decide and delete operations** (23 tools). The repository's golden rules say approval and business execution are never agent tools, and every gateway key or Tools User can call every published tool. Remove or narrow that registration.
3. **Resolved (2026-10-22): two stale tool servers.** `northwind-order-lookup` and `northwind-resolutions` pointed at retired hosts (HTTP 404) and were deleted at the owner's direction. The manifest keeps them as `stale` tombstones, so `plan` reports them `absent` and flags them again if they reappear.
4. **RBAC alone cannot force gateway-only inference**: `Foundry User` includes `Microsoft.CognitiveServices/*` and agents need it. Enforcement here means code fail-closed, removing redundant account-scope OpenAI User from hosted identities, and detecting bypass; a network restriction is a separate decision.
5. Five orphaned agent identities from the removed `northwind-orders` project still hold Models User and OpenAI User.
6. **The runtime key is gateway-wide.** A gateway API key can call every model and every published tool server; it cannot be scoped to one tool server. The runbook therefore creates a dedicated named key for MCP (`northwind-mcp-runtime`) so it can be rotated or deleted without touching the built-in `master` key. Replacing the key with per-identity Entra tokens (project managed identity on the connection, or a token minted per request by the agent) would remove the stored secret; that path is not implemented here and was only verified manually with a user token.
7. **Resolved (2026-10-22): the gateway had no diagnostic settings.** `northwind-gateway-to-law` now sends `GatewayLogs`, `GatewayLlmLogs`, `GatewayMCPLogs` and metrics to `log-zurlo-northwind`, and `northwind-account-to-law` sends the Foundry account's `RequestResponse` logs there too; the policy-assigned `setByPolicy-MCAPSGovernance` setting is untouched. The bypass-detection KQL was run against that workspace and corrected: on this tier model calls land in `ApiManagementGatewayLlmLog` and MCP calls in `ApiManagementGatewayMCPLog` (`ApiManagementGatewayLogs` stayed empty), Foundry's `RequestResponse` rows are in `AzureDiagnostics` with the caller in `properties_s.objectId`, and Responses calls are named `create-response` (the first template filtered on operation names that never occur, so it would have reported nothing).
8. The MCP container apps (`ca-insurance-lookup`, `ca-insurance-review`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`) have public ingress, no authentication and no IP restrictions, so routing through the gateway does not stop anyone calling them directly. Locking them down (Container Apps authentication accepting only the gateway identity, then switching the tool server credential from `none` to `managedIdentity`) is a gated manual decision, not done here.
9. **Prompt agents cannot route model calls through the AI Gateway tier** (tried 2026-10-22 with throwaway connections and a throwaway agent, all deleted afterwards). Foundry resolves the connection only when it uses `ApiKey` auth (an `AAD` connection answers `Connection ... not found`), then validates the model id as letters, digits, `-`, `_` and `.`, so the gateway's `provider/registration` alias is rejected (`Invalid model_id 'aisvc-zurlo-northwind/gpt-4o'`). The gateway in turn accepts only that alias in the request body: a bare registration is `model_not_found`, and path-style routes answer `A model alias is required in the request body`. Mapping a short connection entry name to the alias does not help, because Foundry sends the entry `name` (`'gpt-4o' is not a known model alias`). The two live prompt agents therefore stay direct. Options, all needing a decision: move them to hosted agents (which call the gateway themselves with the alias), publish them through a classic API Management Azure OpenAI API instead, or ask the platform teams for slash-free aliases or a mapping field.

## Runbook

Run everything from the repository root after `az login`. `plan`, `audit`, `verify` and `--dry-run` are read-only; a change needs `--yes` (release, grants, enforce) or `--apply` (connections, hosted deploy). Grants and enforcement write receipts to the git-ignored `.release-state/` folder; keep them, rollback needs them. The repo scripts call Azure through `az rest` only; the `aigateway` CLI extension is needed just to manage gateway keys (step 3).

Order is **route, verify, then enforce**: nothing is enforced until the traffic it would block is shown to flow through the gateway. Steps 1-3 are prerequisites; steps 4-6 are independent of each other and can ship one at a time; step 8 comes last.

| Step | Access the operator needs |
|---|---|
| 0 baseline, every `plan`/`audit` | Reader on the gateway and the Foundry account; Foundry User on the project for `discover-agents` |
| 1 tool servers | API Management Service Contributor on the gateway; AI Gateway Tools User for `verify` |
| 2 grants | User Access Administrator or Role Based Access Control Administrator on the gateway |
| 3 connections | Contributor on the Foundry account and project |
| 4 API app | Contributor on `ca-insurance-api`; AcrPush on `acrzurlonorthwind` to publish the image |
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

Before the owner-directed changes on 2026-10-22 this showed three tool servers to create, two stale ones, every model registered, Models User already held by the five hosted agent identities and **not** by the app identity, key authentication disabled on the account but public network access on, and four public unauthenticated MCP apps. Now it shows the three managed tool servers in sync, both stale servers `absent`, and `bypassDetectionReady: true`; the rest is unchanged.

### 1. Register the managed tool servers

`infra/live/ai-gateway.json` is the desired state: `northwind-insurance-lookup` (the read-only lookup app, no credential), `northwind-insurance-knowledge` (Search KB `northwind-insurance-kb`; the gateway injects a read-only Search **query key** as the `api-key` header, so no admin key is stored on the gateway) and `northwind-insurance-review` (the claim-action draft app, no credential; it rejects calls through the gateway by design, see finding 1). The other servers in the manifest are only reported, never changed. This step was applied on 2026-10-22; re-running it on an unchanged gateway is a no-op.

```powershell
python scripts/ai-gateway-release.py plan --out plan.json      # review plan.json: it is secret-free
$env:SEARCH_QUERY_KEY = az search query-key list --service-name srch-zurlo-northwind -g rg-northwind-data --query "[0].key" -o tsv   # only the insurance KB server needs it; this shell only
python scripts/ai-gateway-release.py apply --plan plan.json --yes --receipt .release-state/ai-gateway-release.json
Remove-Item Env:SEARCH_QUERY_KEY
python scripts/ai-gateway-release.py verify
```

`apply` re-reads the gateway and refuses when the live diff no longer matches the reviewed plan; it creates or updates only the managed servers and has no delete operation. `verify` sends `initialize` and `tools/list` only (no tool is called) and expects `lookup_lookup_policy`, `lookup_lookup_claim`, `knowledge_knowledge_base_retrieve` and `review_draft_claim_action`; it needs the Tools User role (step 2) and returns HTTP 403 without it. Each backend was also checked directly on 2026-10-22: the lookup, knowledge (with the query key) and review servers answered `initialize` and `tools/list` with exactly those tools, before the gateway prefix.

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

Do **not** create a prompt-agent model connection for the AI Gateway tier: it was tried on 2026-10-22 and cannot work (finding 9). `connect-model-gateway.py` remains for gateways whose model aliases are slash-free.

### 4. API app

`ca-insurance-api` is outside the app-release workflow and `scripts/release-app.py` (they release only the five northwind-stack apps). It runs a manual build (`api` image tag `insurance-names-20261020`) of the unmerged `zacharyzurlo-microsoft-insurance-resource-cleanup` work, which renames the insurance project and settings. That build has no gateway code, and a `main` image is **not** a drop-in replacement: on 2026-10-22 releasing the CI-built `main` image (`87731ac`) made `/health` report `insurance_agent_runtime_ready: false`, because `main` still hard-codes the project name `northwind-orders` in `apps/api/app/insurance.py`. It was rolled back to the previous digest within about three minutes. So, in this order:

1. **Land the insurance fleet work in `main` first** (the branch above; it also adds guarded insurance releases to `scripts/release-app.py`), then release an image built from that `main`. Before going on, confirm `GET /health` still reports `insurance_agent_runtime_ready: true` and `GET /gateway/status` returns 200 with `"enabled": false`, and keep the previous digest for rollback (`az containerapp update -g rg-northwind-apps -n ca-insurance-api --image acrzurlonorthwind.azurecr.io/api@sha256:<digest> --set-env-vars GIT_SHA=<commit> DEPLOYED_AT=<UTC timestamp>`; the two variables make `/build` report the new build).
2. **Grant the app identity Models User** (step 2) before the gateway is enabled.
3. **Enable the gateway:**

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

That is a dry run; it validates the gateway settings before any network call and prints the plan. Add `--apply` to deploy. Deploy `orderlookup` and `policylookup` first and run an end-to-end claim through them (the canary); then the other three with `AI_GATEWAY_REQUIRED` only. `resolutiondraft` keeps its direct work-order MCP, or reaches it through `apim-northwind` with `MCP_GATEWAY_CONNECTION` ([api-management.md](./api-management.md)); it never uses the AI Gateway (finding 1), and configuring both routes for one role is a startup error. The guard in this script now accepts both `northwind-orders` and `nw-insurance-claims`; the endpoint and the ARM id must name the same project.

### 6. Prompt agents

Prompt-agent **model** calls stay direct (finding 9); only their knowledge-base MCP can be routed, and that route is **not verified live** for prompt agents (the hosted-agent MCP path is, see [Deployment status](#deployment-status)).

```powershell
$env:FOUNDRY_PROMPT_AGENT_DEPLOYMENT = 'gpt-4o'                    # plus the usual azd environment values
$env:AI_GATEWAY_MCP_KB_ROUTES = '{"nw-hr-kb": {"toolserver": "northwind-hr-knowledge", "connection": "nw-hr-kb-gw"}}'
python scripts/create-prompt-agents.py --dry-run
```

The dry run prints every planned agent, for example `model=gpt-4o; tools=mcp:nw-hr-kb@ai-gateway`, and sends nothing. Each routed knowledge base needs its own project connection (`connect-mcp-gateway.py --toolserver northwind-hr-knowledge --connection-name nw-hr-kb-gw`). The four existing knowledge servers map to `nw-hr-kb`, `nw-claims-kb`, `nw-contact-center-kb` and `northwind-kb` (`northwind-{hr,claims,contact-center,orders}-knowledge`). With `AI_GATEWAY_MCP_REQUIRED=true` an agent whose knowledge base has no route (or whose yaml `mcp` tool is not on the gateway) is **not created** and the run exits non-zero, while agents that comply are still created, so run `--dry-run` first: it lists every such agent (marked `x`, exit code 1) before anything is sent. `create-prompt-agents.py` iterates every department in `data/northwind/company.yaml` and creates the flagship order-concierge set too, so against the live insurance projects run it only with `--dry-run` until that mapping matches them.

### 7. Verify

Follow [Verification](#verification) before moving on.

### 8. Enforce (gated; last)

```powershell
python scripts/ai-gateway-enforce.py plan --principals principals.json --out enforce-plan.json
python scripts/ai-gateway-enforce.py apply --principals principals.json --yes --expect-plan-sha256 <planSha256 from enforce-plan.json>
```

This removes only the account-scope **Cognitive Services OpenAI User** assignment of each listed hosted-agent identity that already holds Models User on the gateway, and writes a rollback receipt. It never removes Foundry User, which the agents need and which still permits direct inference, so the real controls are the fail-closed flags plus the bypass-detection queries. These stay manual decisions the scripts only report: restricting the account's network (public network access is on), Container Apps authentication for the MCP apps, deleting the orphaned identities, and the `northwind-api` tool server (findings 2, 5, 8).

## Rollback

Each step undoes independently, newest first.

| Step | Undo |
|---|---|
| 8 enforce | `python scripts/ai-gateway-enforce.py rollback --receipt <receipt> --yes` recreates exactly the removed assignments |
| 6 prompt agents | re-run `create-prompt-agents.py` with the `AI_GATEWAY_*` variables unset: a new version calls the deployment and Search directly; delete the connections with `az rest --method delete --url "https://management.azure.com<connection id>?api-version=2025-10-01-preview"` |
| 5 hosted agents | deploy a new version from the previous config (flags removed); hosted versions are immutable, so there is no in-place edit |
| 4 API app | `az containerapp update -g rg-northwind-apps -n ca-insurance-api --remove-env-vars AI_GATEWAY_REQUIRED AI_GATEWAY_MODEL_ENDPOINT AI_GATEWAY_MODEL_PROVIDER` (remove `AI_GATEWAY_REQUIRED` first); the newer image can stay, because its gateway code is inactive without these settings |
| 3 connections and key | delete the connections as above; `az aigateway api-key delete -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime --yes` |
| 2 grants | `python scripts/ai-gateway-grants.py revoke --receipt <receipt> --yes` deletes only what that receipt created |
| 1 tool servers | `az aigateway mcp delete -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-insurance-lookup --yes` (and `northwind-insurance-knowledge`, `northwind-insurance-review`); the backend apps are untouched |

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
- **API app:** `GET https://ca-insurance-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/gateway/status`, or through API Management at `https://apim-northwind.azure-api.net/insurance/gateway/status` (the northwind-stack `ca-api` rejects direct calls once `NORTHWIND_GATEWAY_AUTH_REQUIRED` is on, so use `.../northwind/gateway/status` for it). A 404 means the app still runs a build without the gateway code (the insurance API on 2026-10-22). Once routed it returns `"enabled": true`, `"required": true` once enforced, `"features"` all `"gateway"` and `"problem": null`, plus the deployment to alias map (`gpt-5.4` to `aisvc-zurlo-northwind/gpt-5-4`). Traces carry `gateway.route` and `gateway.model_alias`, and the trace panel marks each model call the gateway served with an **AI Gateway** badge showing the alias.
- **Hosted agents:** responses carry `model_gateway_required` and `mcp_route` metadata; a version without the flags omits them.
- **Prompt agents:** `create-prompt-agents.py --dry-run` shows `@ai-gateway` on every gateway-routed MCP tool (`@apim` for ones through `apim-northwind`, `@direct` for the rest); their model strings stay plain deployment names (finding 9).
- **Bypass detection:** `python scripts/ai-gateway-enforce.py audit` prints ready-to-run KQL (`foundryNotFromGateway`, `inScopeIdentitiesBypassing`, `gatewayTraffic`, `gatewayModelUsage`). All four were run against `log-zurlo-northwind` on 2026-10-22 (finding 7): in the 12 hours checked every inference call to the account was attributed to the gateway identity, so `foundryNotFromGateway` returned nothing. Gateway tables take 5 to 15 minutes to fill after the first traffic.
