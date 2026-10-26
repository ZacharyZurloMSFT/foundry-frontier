# AI Gateway routing

How Northwind sends model inference and MCP traffic through the Azure API Management **AI Gateway tier** instance `ai-gateway-northwind`, what is verified live, what is deliberately not routed, and how to roll it out and roll it back.

> **Current status (live rollout 2026-10-26):** all five hosted agents now require the AI Gateway for models. Policy/claim lookup and policy wording also require its MCP routes; claim-action drafting keeps classic APIM so workflow-binding headers survive. The unsafe OpenAPI facade and the two retired non-insurance knowledge registrations are blocked, not deleted. Both managed MCP servers have inline request-rate policies. Classic APIM apply completed, and model/MCP logs are now visible in Log Analytics. Prompt-agent models remain direct; end-to-end distributed trace correlation is not yet proven.

The repo keeps AI Gateway rollout opt-in; a fresh `azd up` is unchanged. The runbook and historical inventory below retain dated deployment evidence. Azure writes are deliberately separate from ordinary deployment and use reviewed plan/apply scripts.

## Current demo readiness (verified 2026-10-26)

| Area | Live evidence | Demo status |
|---|---|---|
| Gateway and models | `ai-gateway-northwind` is `Succeeded` (SKU `AIGateway`); all six expected model registrations are present. A minimal synthetic `gpt-5.4-mini` chat request through the gateway returned HTTP 200. | Model route works. |
| Insurance API | `/insurance/gateway/status` through `apim-northwind` returned 200 with `enabled=true`, `required=true`, all reported API model features on `gateway`, and `problem=null`. The direct Container App URL returned 401 `gateway_required`. | API inference is routed and fail-closed. |
| Hosted agents | Active selected versions: triage **3**, policy/claim lookup **4**, policy wording **4**, claim-action draft **3**, orchestrator **3**. Successful responses from each selected version report gateway inference and `model_gateway_required=true`. | Model routing and fail-closed runtime guards are verified. Lookup and wording also use gateway MCP; drafting stays on classic APIM. |
| Prompt agents | Current prompt-agent models cannot consume the AI Gateway's `provider/registration` alias and remain direct. | Explicit exception; no safe gateway route is established. |
| AI Gateway MCP | Lookup v4 returned `lookup_lookup_claim`; wording v4 returned `knowledge_knowledge_base_retrieve`, both successfully with `mcp_route=ai-gateway`. Dedicated key `insurance-demo-agents` is held only in project connections `nw-insurance-aigw-lookup` / `nw-insurance-aigw-kb`. The unsafe facade and retired knowledge servers return MCP `AccessBlockedByGateway`. | Compatible MCP routes work. Keys remain gateway-wide: keep unsafe servers blocked. Drafting remains a deliberate classic-APIM exception. |
| Policies | Standard `workspaces/default/policies` still rejects operations, but **inline `properties.policies` on tool-server resources works**. Both managed MCP servers round-trip `requestRateLimit`, `callsPerPeriod=120`, `periodSeconds=60`, `counterKey=["identity"]`, `scope=resource`. An isolated one-per-minute canary returned 200 then 429 on read-only `tools/call`, with `Retry-After: 49`; the canary was then removed. | AI Gateway inline policies are applied and invocation rate enforcement is proven. `tools/list` alone did not exercise the counter. These are not classic APIM XML policies. Classic APIM policies were also applied successfully. |
| Observability | Model/token rows and MCP rows now ingest into `ApiManagementGatewayLlmLog` and `ApiManagementGatewayMCPLog` in `log-zurlo-northwind`. Model rows include actual `gpt-4o` / `gpt-5.4` provider traffic from this rollout. | Gateway metrics and logs are demoable. Application-to-gateway distributed trace correlation still needs separate proof. |

### Live rollout evidence and rollback

Classic APIM [apply run 40358557741](https://github.com/ZacharyZurloMSFT/foundry-frontier/actions/runs/40358557741)
completed successfully, including its public smoke job, after the normal environment approval.
It used the reviewed plan from run `40356515644` at main `844da45d759dc2168ab3652739f4fa56d1bdebff`.
No existing Azure resource was deleted or renamed; blocked MCP registrations still appear in the portal.

The deployed agents contained two runtime generations. Setting a flag on the older generation was
**not** enough: it ignored both the fail-closed flags and the new MCP route. Triage, wording and
orchestrator were therefore reconciled by copying only four runtime modules (`config`, `main`,
`runtime`, `tools`) from the already-deployed lookup/draft generation. Their prompts and all other
archive entries remain byte-identical to their original deployment. Reconciled archive SHA-256:
`52be62362a623877db071780c91969fb695599bdb11dd9bf42ab28580889ede6`.
Lookup and drafting retain their original `b3bff914...` source archive. This is not a blanket
redeployment of the repository bundle.

One synthetic request-evidence draft was saved to confirm the classic-APIM workflow-binding path;
it remains pending human approval with `execution_status=not_executed`. No claim was approved,
denied, settled or paid.

Initial agent selector rollback targets are triage **1**, lookup **2**, wording **1**, drafting **2**,
and orchestrator **1**. Roll back selectors before changing or removing the new MCP connections/key.
Restoring a server's `accessState` from `blocked` to `active` is reversible but reopens its exposure;
never unblock the unsafe facade while agent runtime credentials can reach it.
The release manifest now owns the two inline rate policies and detects their drift; additional live
policy types are a conflict rather than silently removed.

Gateway log ingestion includes successful `tools/call` rows for `lookup_claim` at
`2026-10-26T16:50:23Z` and `knowledge_base_retrieve` at `2026-10-26T16:57:32Z`, on the corresponding
insurance tool-server endpoints. These log rows name the backend tool; the hosted response names
the gateway-prefixed tool. Inline policy types are documented in the
[Azure API specification](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/apimanagement/resource-manager/Microsoft.ApiManagement/AIGateway/AIGatewayModel.tsp);
support on this live preview was established by apply/readback and the isolated invocation canary,
not inferred from the newer specification alone.

### Demo telemetry queries

In Azure Monitor, select `ai-gateway-northwind` and chart the `Requests`, `SuccessfulRequests`,
`UnauthorizedRequests`, and `FailedRequests` metrics. In Logs, select workspace `log-zurlo-northwind`
in `rg-northwind-apps` and run these after traffic; gateway log ingestion may lag the request.

```kusto
ApiManagementGatewayLlmLog
| where TimeGenerated > ago(24h) and _ResourceId has "ai-gateway-northwind"
| summarize Calls=count(), PromptTokens=sum(PromptTokens), CompletionTokens=sum(CompletionTokens)
    by ModelName, bin(TimeGenerated, 1h)
| order by TimeGenerated desc
```

```kusto
ApiManagementGatewayMCPLog
| where TimeGenerated > ago(24h) and _ResourceId has "ai-gateway-northwind"
| summarize Calls=count(), Failures=countif(isnotempty(Error))
    by Server=tostring(McpServerEndpoint), bin(TimeGenerated, 1h)
| order by TimeGenerated desc
```

```kusto
AzureDiagnostics
| where TimeGenerated > ago(24h)
| where ResourceProvider == "MICROSOFT.COGNITIVESERVICES"
    and _ResourceId has "aisvc-zurlo-northwind"
    and Category == "RequestResponse"
| summarize Calls=count(), Latest=max(TimeGenerated)
    by OperationName, Caller=tostring(parse_json(tostring(properties_s)).objectId)
| order by Latest desc
```

The API trace panel can show its `gateway.route` and `gateway.model_alias` metadata. End-to-end
trace correlation between an agent/API request and the AI Gateway log row still needs live proof;
until then, present gateway metrics/logs and application traces as separate evidence rather than
claiming a correlated distributed trace.

## Scope

| In scope | Out of scope (stay direct) |
|---|---|
| Foundry hosted agents (models and MCP) and prompt-agent knowledge-base MCP | Search knowledge-base planner and vectorizer calls to models |
| `apps/api` model calls (adjuster support, Copilot harness) | Content Understanding, Voice Live (`gpt-realtime-2.1`) |
| Insurance MCP servers used by agents: policy/claim lookup, insurance knowledge base, claim-action review | Foundry evaluations, red-team, optimizer, fine-tuning |
| | The legacy MCP apps `ca-mcp-tech-lookup` and `ca-mcp-work-order` (legacy `northwind` stack, retired from the repo, see [`legacy-decommission.md`](./legacy-decommission.md)): dropped from scope at the owner's direction, neither registered nor changed |
| | Prompt-agent model calls: Foundry cannot address the gateway's `provider/registration` aliases (finding 9) |
| | GitHub Models smoke tier, eval/seed scripts |
| | Agent-to-agent calls (Agent Service traffic is neither model nor MCP) |

The AI Gateway is not the only gateway in this repository. [`docs/api-management.md`](./api-management.md) describes the classic API Management instance `apim-northwind`, which publishes the REST, WebSocket and MCP surfaces of the insurance apps in `rg-northwind-apps` and **forwards client headers**. This document's AI Gateway tier instance fronts models and header-free MCP servers; header-bound MCP calls (the claim-action draft) belong on `apim-northwind`. See [Two gateways](#two-gateways).

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
- **The gateway does not forward client request headers to MCP backends.** Verified by sending `x-harness-workflow` (the workflow-binding header the harness sent at the time) through the gateway to the claim-review server: the backend never saw it.
- **`failureMode` is accepted but never returned.** The `2025-09-01-preview` service stored no `failureMode` for any registration, and a throwaway canary registered with the non-default `failOpen` came back without it (canary deleted, 2026-10-22). The manifest still declares `failClosed` as intent and sends it, but only the per-endpoint `required: true`, which the service does return, is observable. A missing live `failureMode` is therefore not reported as drift; a returned value that differs still is.

## Live inventory

Historical baseline recorded on 2026-10-22 and updated with the changes of 2026-10-23. Use [Current demo readiness](#current-demo-readiness) for the 2026-10-26 live check. Resource, project and tool-server names below (for example `northwind-insurance-{lookup,knowledge,review}`) are live identifiers, not renamed; the project names are bound to the live deployment.

| Item | State |
|---|---|
| Foundry projects | `nw-insurance-claims` (default), `nw-insurance-documents`, `nw-insurance-support`, `nw-insurance-contact-center` |
| Hosted agents (`nw-insurance-claims`) | `northwind-insurance-{orchestrator,triage,policy-claim-lookup,policy-wording,claim-action-draft}` v1, except `policy-claim-lookup` and `claim-action-draft`, which are v2 since 2026-10-23; models **already via gateway** (`AGENT_MODEL_GATEWAY_*`, set under their pre-rename names on the deployed versions, see the compatibility note under [Configuration contract](#configuration-contract); orchestrator `.../gpt-5-4`, others `.../gpt-4o`); MCP: those two through `apim-northwind` ([api-management.md](./api-management.md#insurance-stack-cut-over-2026-10-23)), the other three never call an MCP app |
| Prompt agents | `northwind-claims-triage` (`nw-insurance-documents`), `northwind-intent-classifier` (`nw-insurance-contact-center`): gpt-4o, no tools, model **direct** |
| API app | `ca-insurance-api`, since 2026-10-23 `main` `ab32e73` plus a `company.yaml` overlay (released by hand: no workflow had released it, and `deploy-apps.yml` now covers it; see [api-management.md](./api-management.md#insurance-stack-cut-over-2026-10-23)): planner gpt-5.4, worker gpt-5.4-mini, prompt-agent gpt-4o, harness gpt-5.4. **Model calls go through the gateway since 2026-10-23** (`AI_GATEWAY_MODEL_ENDPOINT`, `AI_GATEWAY_MODEL_PROVIDER`, `AI_GATEWAY_REQUIRED=true`, revision `ca-insurance-api--gw-aigw-1`); adjuster support and the harness report route `gateway` |
| MCP apps | `ca-insurance-lookup` (read-only synthetic lookups), `ca-insurance-review` (claim-action drafts), plus the legacy `ca-mcp-tech-lookup` and `ca-mcp-work-order` (legacy stack, retired from the repo; never registered on the gateway); external ingress, no IP restrictions; since 2026-10-23 direct calls without the `apim-northwind` token get 401 |
| Gateway tool servers | The live lookup, knowledge, and review registrations remain. The repo manages lookup and knowledge only; review is observed/unmanaged because its required workflow-binding header cannot pass through this gateway. `northwind-api` (OpenAPI facade, 23 tools) remains observed with the decide/delete governance concern. Pre-existing KB servers `northwind-{claims,contact-center,hr,orders}-knowledge` and two stale servers `northwind-order-lookup` / `northwind-resolutions` retain the dated historical treatment below. |
| Diagnostics | gateway: `northwind-gateway-to-law` sends `GatewayLogs`, `GatewayLlmLogs`, `GatewayMCPLogs` and `AllMetrics` to Log Analytics workspace `log-zurlo-northwind` (resource-specific tables); Foundry account: `northwind-account-to-law` sends `RequestResponse` to the same workspace, next to the existing policy-assigned `setByPolicy-MCAPSGovernance` setting |
| Foundry connections | one `RemoteTool` KB connection `nw-insurance-claims-kb-mcp` (CustomKeys, Search admin key); no `ApiManagement` model connection |
| Account | `disableLocalAuth=true`, public network enabled, no private endpoints |
| RBAC | Models User on the gateway for the 5 hosted agent identities (plus 5 orphaned identities of the removed `northwind-orders` project) and, since 2026-10-23, for the app UAMI `id-app-zurlo-northwind` (shared by all ten Container Apps; it has no Tools User); hosted identities also hold Cognitive Services OpenAI User (account) and Foundry User (project) |

## Deployment status

Historical deployment snapshot from 2026-10-22/23, superseded for current state by [Current demo readiness](#current-demo-readiness). The insurance apps were later switched to the AI Gateway for API model calls and locked behind `apim-northwind`.

| Layer | State |
|---|---|
| Code on `main` | merged; CI passed |
| Legacy northwind-stack apps (`ca-api`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`, `ca-web-tech`, `ca-web-voice`; retired from the repo) | released by the app-release workflow (`deploy-apps.yml`, which no longer covers them), all five ready and smoke-tested. The gateway code is **inactive** (no `AI_GATEWAY_*` settings): `GET /northwind/gateway/status` through API Management returns `"enabled": false` |
| Insurance apps (`ca-insurance-*`) | **not released by any workflow** (state of 2026-10-22, see the update above): they ran manual builds of the then-unmerged `zacharyzurlo-microsoft-insurance-resource-cleanup` work (`ca-insurance-api` was image tag `insurance-names-20261020`), so `/gateway/status` is 404 and its model calls go direct, and a `main` image was not a drop-in replacement (runbook step 4). `deploy-apps.yml` and `scripts/release-app.py` now release exactly these five apps (azd services `api`, `mcp-policy-lookup`, `mcp-claim-review`, `web-portal`, `web-voice`), but no such release is recorded here |
| Gateway tool servers and diagnostics | applied live (see the inventory above); lookup and review were re-pointed at `apim-northwind` on 2026-10-23 and verified. Current managed set: lookup and knowledge; review is observed. |
| Hosted agents, prompt agents, connections, role grants, enforcement | **not applied**: runbook steps 2, 3, 5, 6 and 8 |
| Infrastructure | unchanged: the plan is identical before and after the merge, and apply is a manual dispatch |

### Verified live (2026-10-22, after the table above)

- **Tool servers through the gateway (2026-10-22 historical):** `release verify` listed the then-registered servers, and read-only `lookup_policy`, `lookup_claim` and `knowledge_base_retrieve` calls succeeded (the review tool was not called). The temporary Tools User assignment used for this was revoked from its receipt.
- **The API's model client:** the real app, run in-process with the operator's identity against the live gateway (no deployed app was touched), reported `/gateway/status` enabled with the alias map and served its model routes through the gateway. That build still had a safety check (`/safety/check`, answered as `foundry-guardrails via AI Gateway`) and a two-model bake-off (streamed); the insurance-only conversion removed both from the repository.
- **The hosted agents' MCP design:** one Responses call through the gateway, with an `mcp` tool pointed at a gateway tool server and a runtime key in the `api-key` header, made Foundry list the prefixed tool and execute it (`lookup_lookup_policy`, `knowledge_knowledge_base_retrieve`), and the model answered from the result. The canary keys were deleted afterwards; only `master` remains.
- **Prompt agents cannot use the gateway** for models (finding 9).

### Verified live (2026-10-23, after the re-point and the API switch)

- **Tool servers through the gateway to the locked apps (2026-10-23 historical):** the then-managed registrations were re-pointed and verified. Read-only lookup calls traversed AI Gateway, `apim-northwind`, the in-app guard, and `ca-insurance-lookup`, returning synthetic records. The review registration was not a usable route and is now observed/unmanaged.
- **The review server fails closed:** one `review_draft_claim_action` call with no workflow-binding header (see finding 1) was rejected by the app (`Insurance workflow does not match the application-bound request.`). The server checks that header before it touches storage, and the gateway forwards none, so a draft cannot be saved through this tool server. A wrong subscription key is answered with 401 by `apim-northwind`.
- The temporary Tools User assignment used for the checks was revoked from its receipt.
- **The API app on the gateway:** the build deployed then (`main` `ab32e73` plus the overlay) still had a safety check and a model bake-off, which the insurance-only conversion removed from the repository; they are named here as they were verified. After the app identity was granted Models User (runbook step 2), a read-only check inside the running container (`az containerapp exec`, with the gateway settings applied to the check process only) reached the gateway as the app's own managed identity: `/gateway/status` enabled with no problem, `/safety/check` answered 200 and both bake-off models replied. The three settings were then set as one new revision (`ca-insurance-api--gw-aigw-1`, image unchanged, ready in under a minute, rollback target `ca-insurance-api--gw92751092`). Through `apim-northwind`: `/health` still reports `insurance_agent_runtime_ready: true`; `/gateway/status` is enabled and required with every feature (`safety`, `bakeoff`, `adjuster_support`, `harness`) on `gateway` and no problem; `/safety/check` returns 200; `/bakeoff/chat` is answered by both models; `/harness/status` reports route `gateway`; and one read-only Agent Harness run (two lookups and an answer, 16 seconds) completed without creating a draft. Direct calls to the app are still rejected with 401.

## Routing design

| Traffic | Route | Notes |
|---|---|---|
| Hosted agent inference | client-side gateway (`AGENT_MODEL_GATEWAY_*`) | `AI_GATEWAY_REQUIRED=true` makes direct paths a startup error |
| Hosted MCP tools | **Lookup and wording through AI Gateway; drafting through classic APIM** | Lookup/knowledge use dedicated project connections and require the gateway. Unsafe/retired servers stay blocked. |
| Hosted claim-action draft MCP (`claim-action-draft`) | **`apim-northwind`, never the AI Gateway** (direct calls are rejected since 2026-10-23) | Needs application-bound headers the AI Gateway drops; the live `northwind-insurance-review` registration is observed but unmanaged and rejects calls by design |
| Prompt agent inference | **direct (cannot be routed today)** | Foundry rejects the gateway's `provider/registration` aliases as model ids (finding 9) |
| Prompt agent knowledge-base MCP | the KB's gateway tool server, connection holding the runtime key | `AI_GATEWAY_MCP_KB_ROUTES`; a KB without a route stays on Search unless `AI_GATEWAY_MCP_REQUIRED` |
| API app inference | `apps/api/app/model_gateway.py` with Entra token | `AI_GATEWAY_MODEL_*` settings; `GET /gateway/status` |
| Copilot harness inference | SDK provider pointed at the gateway | same settings |

### Configuration contract

All optional; empty means unset and keeps the current direct behaviour (fresh `azd up` is unchanged).

| Setting | Purpose |
|---|---|
| `AI_GATEWAY_REQUIRED` | models must use the gateway (hosted agents, API app); fail closed. It does not apply to prompt-agent models, which stay direct (finding 9): `create-prompt-agents.py` (a manual script; no azd hook runs it any more) validates the flag but ignores it, so setting the flag does not stop prompt-agent creation. Whenever the flag is set in the azd environment, `scripts/validate-reuse.py` (the `preprovision` / `predeploy` hook) checks that the hosted-agent gateway settings are present and well formed, and the hosted agents fail closed on their next deploy, so set it there only after the gateway route is verified |
| `AI_GATEWAY_MCP_REQUIRED` | hosted `policy-claim-lookup` and `policy-wording` MCP and prompt-agent knowledge-base MCP must use the gateway; fail closed |
| `AGENT_MODEL_GATEWAY_ENDPOINT/_MODEL/_DEPLOYMENT/_SCOPE` | existing hosted-agent contract |
| `AGENT_MCP_GATEWAY` | hosted agents: JSON keyed by role (`policy-claim-lookup`, `policy-wording`): `url`, `tool_prefix`, `connection`, optional `forwards_headers` (a `claim-action-draft` entry is accepted only with `forwards_headers: true`) |
| `MCP_GATEWAY_CONNECTION` | hosted agents routing `policy-claim-lookup` / `claim-action-draft` through `apim-northwind` (the [API Management](./api-management.md) contract); mutually exclusive with `AGENT_MCP_GATEWAY` for the same role |
| `AI_GATEWAY_MODEL_ENDPOINT`, `AI_GATEWAY_SCOPE`, `AI_GATEWAY_MODEL_PROVIDER`, `AI_GATEWAY_MODEL_ALIASES` | API app |
| `AI_GATEWAY_MODEL_CONNECTION` | Foundry model connection for prompt agents; **not usable with the AI Gateway tier** (finding 9), leave it unset |
| `AI_GATEWAY_MCP_KB_ROUTES` | prompt agents: JSON keyed by Search knowledge-base name, `{"toolserver", "connection", "tool_prefix"?}` (prefix defaults to `knowledge_`) |

Compatibility: the pre-cleanup spellings are still read while the canonical name is unset (the canonical name wins): `ORDERS_MODEL_GATEWAY_*` for `AGENT_MODEL_GATEWAY_*`, `ORDERS_MCP_GATEWAY` for `AGENT_MCP_GATEWAY`, `MCP_TECH_LOOKUP_URL` / `MCP_WORK_ORDER_URL` for `MCP_POLICY_LOOKUP_URL` / `MCP_CLAIM_REVIEW_URL`, and `MCP_GATEWAY_CONNECTION_TECH_LOOKUP` / `MCP_GATEWAY_CONNECTION_WORK_ORDER` for `MCP_GATEWAY_CONNECTION_POLICY_LOOKUP` / `MCP_GATEWAY_CONNECTION_CLAIM_REVIEW`. The old hosted role ids are still accepted by `--role`, in `--config` sections and as `AGENT_MCP_GATEWAY` keys (`ROLE_ALIASES` in `scripts/deploy-hosted-agents.py`); use the canonical ids.

### Two gateways

| | AI Gateway tier `ai-gateway-northwind` (this document) | Classic API Management `apim-northwind` ([api-management.md](./api-management.md)) |
|---|---|---|
| Fronts | models; header-free MCP (insurance lookup, knowledge bases, claim-action review) | REST, WebSocket and MCP surfaces of the insurance apps in `rg-northwind-apps` |
| Client headers | **dropped** before the MCP backend | forwarded |
| Caller auth | Entra token (Models/Tools User) or gateway runtime key | subscription key `Ocp-Apim-Subscription-Key` in the `MCP_GATEWAY_CONNECTION` project connection |
| Agent settings | `AI_GATEWAY_*`, `AGENT_MCP_GATEWAY` | hosted agents: `NORTHWIND_GATEWAY_*`, `MCP_GATEWAY_CONNECTION`; prompt agents: one connection per MCP endpoint, `MCP_GATEWAY_CONNECTION_POLICY_LOOKUP` / `MCP_GATEWAY_CONNECTION_CLAIM_REVIEW` |

For one hosted role the two routes are mutually exclusive: setting both `MCP_GATEWAY_CONNECTION` and an `AGENT_MCP_GATEWAY` entry for `policy-claim-lookup` or `claim-action-draft` is a startup error. `policy-wording` (the knowledge base) exists only on the AI Gateway, and `claim-action-draft` can only use `apim-northwind` (direct calls to the locked claim-review app are rejected). The hosted response metadata `mcp_route` reports `ai-gateway`, `api-management` or `direct`, and `create-prompt-agents.py --dry-run` marks each MCP tool `@ai-gateway`, `@apim` or `@direct`; the `apim-northwind` connections are never attached to an AI Gateway URL.

The two gateways can chain. The managed AI Gateway lookup tool server reaches `ca-insurance-lookup` through `apim-northwind` and carries the `insurance-agents` subscription key in an `Ocp-Apim-Subscription-Key` credential header. The live claim-review registration also points through classic APIM, but is observed/unmanaged and rejects calls because the AI Gateway drops the workflow-binding header. The lookup key is an environment placeholder (`${INSURANCE_AGENTS_KEY}`) in `infra/live/ai-gateway.json`, resolved at `apply` time only.

## Findings to act on

1. **The claim-review MCP cannot be routed through the AI Gateway.** Its `x-workflow-id` binding is an application-bound proof (the deprecated `x-orders-workflow` spelling is still accepted, and is the one the live app checked on 2026-10-23), and the gateway drops client headers. `ca-insurance-review` publishes only `draft_claim_action`, whose server requires `x-workflow-id` to equal the `workflow_id` argument. The live `northwind-insurance-review` registration therefore rejects calls by design; it remains observed but is no longer managed by `infra/live/ai-gateway.json`. Consumers use `apim-northwind` (which forwards headers); direct calls are rejected since 2026-10-23. A usable AI Gateway route needs either header forwarding on the tier or a server-verifiable argument minted by application code (a security redesign that needs an explicit decision).
2. **`northwind-api` tool server publishes HITL decide and delete operations** (23 tools). The repository's golden rules say approval and business execution are never agent tools, and every gateway key or Tools User can call every published tool. Remove or narrow that registration (see [`legacy-decommission.md`](./legacy-decommission.md), section 5).
3. **Resolved (2026-10-22): two stale tool servers.** `northwind-order-lookup` and `northwind-resolutions` pointed at retired hosts (HTTP 404) and were deleted at the owner's direction. Until the insurance-only conversion the manifest kept them as `stale` tombstones; it no longer lists them, so a reappearance would show up as `unlisted` in `plan`.
4. **RBAC alone cannot force gateway-only inference**: `Foundry User` includes `Microsoft.CognitiveServices/*` and agents need it. Enforcement here means code fail-closed, removing redundant account-scope OpenAI User from hosted identities, and detecting bypass; a network restriction is a separate decision.
5. Five orphaned agent identities from the removed `northwind-orders` project still hold Models User and OpenAI User.
6. **The runtime key is gateway-wide.** A gateway API key can call every model and every published tool server; it cannot be scoped to one tool server. The runbook therefore creates a dedicated named key for MCP (`northwind-mcp-runtime`) so it can be rotated or deleted without touching the built-in `master` key. Replacing the key with per-identity Entra tokens (project managed identity on the connection, or a token minted per request by the agent) would remove the stored secret; that path is not implemented here and was only verified manually with a user token.
7. **Resolved (2026-10-22): the gateway had no diagnostic settings.** `northwind-gateway-to-law` now sends `GatewayLogs`, `GatewayLlmLogs`, `GatewayMCPLogs` and metrics to `log-zurlo-northwind`, and `northwind-account-to-law` sends the Foundry account's `RequestResponse` logs there too; the policy-assigned `setByPolicy-MCAPSGovernance` setting is untouched. The bypass-detection KQL was run against that workspace and corrected: on this tier model calls land in `ApiManagementGatewayLlmLog` and MCP calls in `ApiManagementGatewayMCPLog` (`ApiManagementGatewayLogs` stayed empty), Foundry's `RequestResponse` rows are in `AzureDiagnostics` with the caller in `properties_s.objectId`, and Responses calls are named `create-response` (the first template filtered on operation names that never occur, so it would have reported nothing).
8. **Resolved (2026-10-23): the MCP container apps were public and unauthenticated.** All four (`ca-insurance-lookup`, `ca-insurance-review`, plus the legacy `ca-mcp-tech-lookup` and `ca-mcp-work-order`, retired from the repo) now run the in-app gateway guard and reject direct calls with 401. The lock is an application check of the `apim-northwind` managed-identity token, not Container Apps authentication ([api-management.md](./api-management.md)). What was left on the AI Gateway side was re-pointed the same day: `northwind-insurance-lookup` and `northwind-insurance-review` had registered the direct URLs with credential `none` and would get 401, so they now call `https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup` and `.../work-order` with the `insurance-agents` key in an `Ocp-Apim-Subscription-Key` header (the review server still cannot work through the AI Gateway, finding 1). **Key rotation:** each registration stores the key and the gateway never returns it, so a rotation is invisible to `plan`. After `apim-northwind` rotates the `insurance-agents` key, put the new key in `INSURANCE_AGENTS_KEY` and run `plan --refresh-secrets northwind-insurance-lookup`, then `apply` (step 1). The review registration is observed/unmanaged and needs a separate reviewed update if retained. The same key is also held by the `nw-insurance-claims-apim-mcp` connection and the `mcp-gateway-key` secret of `ca-insurance-api` ([api-management.md](./api-management.md#operations)).
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
| 4 API app | Contributor on `ca-insurance-api` for the `az containerapp update` of step 3; permission to dispatch and approve `Scoped App Release`, whose identity publishes the image to `acrzurlonorthwind` |
| 5-6 agents | the deploy identity already used for `deploy-hosted-agents.py` / `create-prompt-agents.py` |
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

Before the owner-directed changes on 2026-10-22 this showed three tool servers to create, two stale ones, every model registered, Models User already held by the five hosted agent identities and **not** by the app identity, key authentication disabled on the account but public network access on, and four public unauthenticated MCP apps. Now it shows the three managed tool servers in sync, both stale servers `absent`, and `bypassDetectionReady: true`; the rest is unchanged. Since the insurance-only conversion the manifest no longer lists the two deleted stale servers (its `stale` section is empty), so they no longer appear in the plan, and the audit covers only the two insurance MCP apps (`ca-insurance-lookup`, `ca-insurance-review`); a legacy KB server that is still live shows up as `unlisted`.

### 1. Register the managed tool servers

`infra/live/ai-gateway.json` manages two desired tool servers: `northwind-insurance-lookup` (the read-only lookup app; it accepts only `apim-northwind`'s token, so the registration points at `https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup` and the gateway holds the `insurance-agents` subscription key as an `Ocp-Apim-Subscription-Key` header) and `northwind-insurance-knowledge` (Search KB `northwind-insurance-kb`; the gateway injects a read-only Search **query key** as the `api-key` header, so no admin key is stored on the gateway). `northwind-insurance-review` remains observed, not managed: it rejects calls through the gateway by design, see finding 1. The other servers are only reported, never changed. This step was applied on 2026-10-22; lookup and review were re-pointed at `apim-northwind` on 2026-10-23.

```powershell
python scripts/ai-gateway-release.py plan --out plan.json      # review plan.json: it is secret-free
# set only the variables of the servers the plan changes (see requiredEnvironment in plan.json); this shell only
$env:SEARCH_QUERY_KEY = az search query-key list --service-name srch-zurlo-northwind -g rg-northwind-data --query "[0].key" -o tsv      # knowledge
$env:INSURANCE_AGENTS_KEY = az rest --method post --body "{}" --query primaryKey -o tsv `
  --url "https://management.azure.com/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-apps/providers/Microsoft.ApiManagement/service/apim-northwind/subscriptions/insurance-agents/listSecrets?api-version=2024-05-01"   # managed lookup
python scripts/ai-gateway-release.py apply --plan plan.json --yes --receipt .release-state/ai-gateway-release.json
Remove-Item Env:SEARCH_QUERY_KEY, Env:INSURANCE_AGENTS_KEY
python scripts/ai-gateway-release.py verify
```

`apply` re-reads the gateway and refuses when the live diff no longer matches the reviewed plan; it creates or updates only the managed servers and has no delete operation. `verify` sends `initialize` and `tools/list` only (no tool is called) and expects `lookup_lookup_policy`, `lookup_lookup_claim`, and `knowledge_knowledge_base_retrieve`. It needs the Tools User role, but **do not grant that role while `northwind-api` exposes HITL decide/delete operations**; narrow or remove that observed server through a separate reviewed change first. Each backend was also checked directly on 2026-10-22: the lookup, knowledge (with the query key) and review servers answered `initialize` and `tools/list` with exactly those tools, before the gateway prefix.

### 2. Grant the data-plane roles

```powershell
python scripts/ai-gateway-grants.py plan --preset app-uami
python scripts/ai-gateway-grants.py apply --preset app-uami --yes
```

The preset gives the API app identity **AI Gateway Models User** only: the app calls models, not tool servers, through the gateway. It was applied on 2026-10-23 (role assignment `6bca9f72-e92a-5c39-bf6f-aa9336be9c10`; if the receipt is lost, deleting that one assignment at the gateway scope is the equivalent of `revoke`). All ten Container Apps share the identity `id-app-zurlo-northwind`, so all of them (the SPAs and MCP apps included) can now call the registered models through the gateway; narrowing that would need per-app identities. The hosted agents already hold Models User (`apply --principals principals.json --yes` is a no-op for them) and new versions get it from `deploy-hosted-agents.py --grant-runtime-roles`. **No agent needs AI Gateway Tools User**, because provider-executed MCP authenticates with the connection's runtime key. Tools User also grants access to every published tool server, so do not grant it to a person or pipeline while the observed `northwind-api` includes decide/delete operations.

### 3. Foundry connections

**Live rollout 2026-10-26:** `northwind-api` and the retired knowledge registrations were blocked
and their data-plane denial verified before storing the dedicated `insurance-demo-agents` runtime key
in `nw-insurance-aigw-lookup` / `nw-insurance-aigw-kb`. The commands below remain an alternative
setup procedure, not a reason to create duplicate keys/connections. A newly created key initially
returned 401 while configuration propagated; bounded retries with the same key succeeded.

The runtime key is gateway-wide (finding 6), so after those prerequisites create a dedicated named key rather than reusing `master`:

```powershell
az extension add --name aigateway --version 1.0.0b6
az aigateway api-key create -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime --display-name "Northwind MCP runtime"
az aigateway api-key list-secrets -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-mcp-runtime   # copy the primary value
$env:AI_GATEWAY_RUNTIME_KEY = '<primary value>'                # never print or commit it
$project = '/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims'
python scripts/connect-mcp-gateway.py --project-arm-id $project --toolserver northwind-insurance-lookup --connection-name nw-insurance-claims-gateway-mcp
python scripts/connect-mcp-gateway.py --project-arm-id $project --toolserver northwind-insurance-knowledge --connection-name nw-insurance-claims-gateway-kb
# the dry runs print the matching AGENT_MCP_GATEWAY entry; add --apply to each command to create the connections
Remove-Item Env:AI_GATEWAY_RUNTIME_KEY
```

Do **not** create a prompt-agent model connection for the AI Gateway tier: it was tried on 2026-10-22 and cannot work (finding 9). `connect-model-gateway.py` remains for gateways whose model aliases are slash-free.

### 4. API app

`ca-insurance-api` is in the release lane of the `Scoped App Release` workflow (`.github/workflows/deploy-apps.yml`, service `api`, through `scripts/release-app.py`), but the live releases recorded here were done by hand. Until 2026-10-23 it ran a manual build (`api` image tag `insurance-names-20261020`) of the then-unmerged `zacharyzurlo-microsoft-insurance-resource-cleanup` work, which renames the insurance project and settings. That build has no gateway code, and a `main` image was **not** a drop-in replacement: on 2026-10-22 releasing the CI-built `main` image (`87731ac`) made `/health` report `insurance_agent_runtime_ready: false`, because that `main` hard-coded the project name `northwind-orders` in `apps/api/app/insurance.py` (`INSURANCE_PROJECTS` now accepts both `northwind-orders` and `nw-insurance-claims`). It was rolled back to the previous digest within about three minutes. **Update 2026-10-23:** the app now runs `main` `ab32e73` plus a `company.yaml` overlay (see [api-management.md](./api-management.md#insurance-stack-cut-over-2026-10-23)), so `GET /gateway/status` returned 200 with `"enabled": false` and steps 2 and 3 did not have to wait for step 1; step 1 remains the way to drop the overlay. **Steps 2 and 3 were applied on 2026-10-23** (see [Verified live](#verified-live-2026-10-23-after-the-re-point-and-the-api-switch)). In this order:

1. **Release an image built from `main`.** Dispatch `Scoped App Release` with `service=api` (the automatic run after CI on `main` releases the services that changed since the last successful automatic release). The workflow is gated by the `NORTHWIND_DEPLOY_ENABLED` repository variable and runs in the `northwind-apps` environment; it builds the image on the runner, pushes `acrzurlonorthwind.azurecr.io/api@sha256:<digest>`, keeps every existing app setting and sets `GIT_SHA` / `DEPLOYED_AT` so `/build` reports the new build. Before going on, confirm `GET /health` still reports `insurance_agent_runtime_ready: true` and `GET /gateway/status` returns 200 with `"enabled": false`, and keep the previous digest for rollback (`az containerapp update -g rg-northwind-apps -n ca-insurance-api --image acrzurlonorthwind.azurecr.io/api@sha256:<digest> --set-env-vars GIT_SHA=<commit> DEPLOYED_AT=<UTC timestamp>`; the two variables make `/build` report the restored build).
2. **Grant the app identity Models User** (step 2) before the gateway is enabled.
3. **Enable the gateway:**

```powershell
az containerapp update -g rg-northwind-apps -n ca-insurance-api --set-env-vars `
  AI_GATEWAY_MODEL_ENDPOINT=https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/ `
  AI_GATEWAY_MODEL_PROVIDER=aisvc-zurlo-northwind
```

The app is single-revision, so this creates and activates a new revision. Before changing the app you can prove the route from inside the running container without touching it: `az containerapp exec -g rg-northwind-apps -n ca-insurance-api --command "<shell command>"` can run a short Python script that sets the two `AI_GATEWAY_*` variables, disables the guard (`NORTHWIND_GATEWAY_AUTH_REQUIRED=false`) and blanks the Application Insights connection string for that process only, imports `app.main` and calls `/gateway/status` and one model route through an ASGI transport (the 2026-10-23 check called `/safety/check` and `/bakeoff/chat` of the build then deployed, routes the repository no longer has; call the adjuster-support chat, `/adjuster-support/{slug}/chat`, instead); that exercises the app's own managed identity, its Models User role and the container's network path (on Windows start the Azure CLI by its own Python so `|` in the command is not re-parsed by `az.cmd`). Then check `GET /gateway/status` through `apim-northwind`, ask adjuster support a question and run one read-only harness request (see [Verification](#verification)). The live change set `AI_GATEWAY_REQUIRED=true` in the same revision because that check had already proven the route; add it in a second revision if you prefer to observe first. `AI_GATEWAY_REQUIRED=true` makes a missing or broken gateway configuration an error rather than a silent direct call. The planner, worker, harness and adjuster-support deployments map to registered aliases (`gpt-5.4`, `gpt-5.4-mini`, `Kimi-K2.6`); the adjuster-support `kimi` option calls the Responses API while the `kimi-k2-6` registration lists chat completions only, so exercise the `gpt-5-4` and `gpt-5-4-mini` options and do not expect `kimi` to work through the gateway. The app never calls AI Gateway MCP tool servers: its harness reaches the lookup and claim-review servers through `apim-northwind` ([api-management.md](./api-management.md)).

### 5. Hosted agents

The five hosted agents (roles `orchestrator`, `triage`, `policy-claim-lookup`, `policy-wording` and `claim-action-draft`; Foundry agent names `northwind-insurance-<role>`, prompts in `agents/insurance/*.yaml`) already send models through the gateway. A new version adds the fail-closed flags and, for the two lookup roles (`policy-claim-lookup`, `policy-wording`), MCP routing. Start from the live version's non-secret settings and add:

```json
{
  "common": {
    "AI_GATEWAY_REQUIRED": "true",
    "AI_GATEWAY_MCP_REQUIRED": "true",
    "AGENT_MCP_GATEWAY": "{\"policy-claim-lookup\": {\"url\": \"https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp\", \"tool_prefix\": \"lookup_\", \"connection\": \"nw-insurance-claims-gateway-mcp\"}, \"policy-wording\": {\"url\": \"https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-knowledge/mcp\", \"tool_prefix\": \"knowledge_\", \"connection\": \"nw-insurance-claims-gateway-kb\"}}"
  }
}
```

```powershell
$project = '/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims'
python scripts/deploy-hosted-agents.py --role policy-claim-lookup `
  --project-endpoint https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims `
  --project-arm-id $project --tenant <tenant id> --subscription 137c905b-7734-479e-acc5-ab8222462936 `
  --requirements agents/hosted/requirements.txt --config hosted-gateway.json --output .release-state/policy-claim-lookup.json
```

That is a dry run; it validates the gateway settings before any network call and prints the plan. Add `--apply` only after reconciling the live source fingerprints with this checkout; the selected bundles still differ from the repository bundle. See the live rollout evidence above for the source-preserving reconciliation actually used. `claim-action-draft` keeps `MCP_GATEWAY_CONNECTION` and classic `apim-northwind`, never the AI Gateway, because the workflow-binding header must survive. Lookup now uses the AI Gateway instead: remove its classic `MCP_GATEWAY_CONNECTION` when configuring that route; both routes for one role are a startup error. The script accepts the two project names bound to the live deployment, `northwind-orders` and `nw-insurance-claims`; the endpoint and ARM id must name the same project (checked with `--apply`).

### 6. Prompt agents

Prompt-agent **model** calls stay direct (finding 9); only their knowledge-base MCP can be routed, and that route is **not verified live** for prompt agents (the hosted-agent MCP path is, see [Deployment status](#deployment-status)).

```powershell
$env:FOUNDRY_PROMPT_AGENT_DEPLOYMENT = 'gpt-4o'                    # plus the usual azd environment values
$env:AI_GATEWAY_MCP_KB_ROUTES = '{"northwind-insurance-kb": {"toolserver": "northwind-insurance-knowledge", "connection": "northwind-insurance-kb-gw"}}'
python scripts/create-prompt-agents.py --dry-run
```

The dry run prints every planned agent and sends nothing. The three current prompt agents (`northwind-insurance-adjuster-support`, `northwind-claims-triage`, `northwind-intent-classifier`) each print as `model=gpt-4o; tools=none`: none of them declares an MCP or knowledge tool, so the knowledge-base route only takes effect once a prompt agent adds one, and it would then print `tools=mcp:northwind-insurance-kb@ai-gateway`. Each routed knowledge base needs its own project connection (`connect-mcp-gateway.py --toolserver northwind-insurance-knowledge --connection-name northwind-insurance-kb-gw`); the manifest also lists the department KB servers `northwind-claims-knowledge` (`nw-claims-kb`) and `northwind-contact-center-knowledge` (`nw-contact-center-kb`) as observed. With `AI_GATEWAY_MCP_REQUIRED=true` an agent whose knowledge base has no route (or whose yaml `mcp` tool is not on the gateway) is **not created** and the run exits non-zero, while agents that comply are still created, so run `--dry-run` first: it lists every such agent (marked `x`, exit code 1) before anything is sent. The script skips the hosted `claims-concierge` department (its agents come from `deploy-hosted-agents.py`).

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
| 1 tool servers | `az aigateway mcp delete -g rg-northwind-ai --gateway-name ai-gateway-northwind -n northwind-insurance-lookup --yes` (and `northwind-insurance-knowledge`); the backend apps are untouched. `northwind-insurance-review` is observed/unmanaged and is not deleted by this rollback. To undo only the 2026-10-23 re-point, restore the previous manifest entries from git history (direct Container Apps URL, credential `none`) and `apply`; they work only while the MCP apps are unlocked |

Removing the gateway flags is enough to return the repository code to the direct paths; none of the changes alters `azd up`.

## Verification

Repository (no cloud or model calls; the seed generation and the pytest line are part of the `ci.yml` job, the self-check is the pre-PR check):

```powershell
python data/seed/generate.py --domain insurance                    # synthetic inputs, needs reportlab
python -m pytest apps/api/tests scripts/tests/test_ai_gateway_common.py scripts/tests/test_ai_gateway_release.py `
  scripts/tests/test_ai_gateway_grants.py scripts/tests/test_ai_gateway_enforce.py scripts/tests/test_model_gateway_connection.py `
  scripts/tests/test_mcp_gateway_connection.py scripts/tests/test_prompt_agents_gateway.py `
  scripts/tests/test_validate_reuse_gateway.py scripts/tests/test_deploy_hosted_gateway_grants.py -q
python -m pytest agents/tests -q                                    # isolated venv from agents/hosted/requirements.txt; not in CI
python evals/smoke_eval.py --self-check                             # not in ci.yml
```

Live, after each routing step:

- **Tool servers:** `python scripts/ai-gateway-release.py plan` shows no pending change, and `verify --strict` lists only the expected tools (add `--include-observed` to see whether `northwind-api` still publishes decide/delete operations).
- **Grants:** `python scripts/ai-gateway-grants.py plan --principals principals.json` reports everything already present.
- **API app:** `GET https://ca-insurance-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/gateway/status`, or through API Management at `https://apim-northwind.azure-api.net/insurance/gateway/status` (once the in-app guard of [api-management.md](./api-management.md) is enabled on `ca-insurance-api`, direct calls are rejected, so use the API Management URL). A 404 means the app still runs a build without the gateway code (the insurance API on 2026-10-22). Once routed it returns `"enabled": true`, `"required": true` once enforced, `"features"` all `"gateway"` and `"problem": null`, plus the deployment to alias map (`gpt-5.4` to `aisvc-zurlo-northwind/gpt-5-4`). Traces carry `gateway.route` and `gateway.model_alias`, and the trace panel marks each model call the gateway served with an **AI Gateway** badge showing the alias.
- **Hosted agents:** responses carry `model_gateway_required` and `mcp_route` metadata; a version without the flags omits them.
- **Prompt agents:** `create-prompt-agents.py --dry-run` shows a plain deployment name as the model of every agent (finding 9) and `tools=none` for the three current prompt agents; a prompt agent that adds an MCP or knowledge tool shows `@ai-gateway` for a gateway-routed tool, `@apim` for one through `apim-northwind` and `@direct` for the rest.
- **Bypass detection:** `python scripts/ai-gateway-enforce.py audit` prints ready-to-run KQL (`foundryNotFromGateway`, `inScopeIdentitiesBypassing`, `gatewayTraffic`, `gatewayModelUsage`). All four were run against `log-zurlo-northwind` on 2026-10-22 (finding 7): in the 12 hours checked every inference call to the account was attributed to the gateway identity, so `foundryNotFromGateway` returned nothing. Gateway tables take 5 to 15 minutes to fill after the first traffic.
