# Glossary

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry
> disagrees with the code, fix the entry in the same PR.

Terms that are easy to misread here. The repository is insurance-only, but earlier demo names still show up in
deployed configuration and live resource names, so start with [Naming lineage](#naming-lineage).

## Naming lineage

The repo was reshaped twice. The earlier eras are gone from the code, UI, data and docs. Their names survive in
two forms only: **deprecated aliases** (still read so already deployed settings and clients keep working; the
canonical name always wins and an alias is never written) and **live identifiers** bound to the running
deployment (never renamed from code). `scripts/tests/test_no_legacy_vocabulary.py` keeps everything else clean
([gotchas](gotchas/names-and-retired-vocabulary.md)), and [legacy-decommission.md](../legacy-decommission.md) is
the operator runbook for retiring the old Azure resources.

| Era | Story | Where it still shows up |
|---|---|---|
| **Field service** (original scaffold, retired) | Customer voice, orchestrator, four prompt agents, HITL approval of a work order | Only as the retired app names and the live gateway identifiers below |
| **Order Concierge** (retail reshape, retired) | Customer contact, then a draft resolution of a retail order, then human approval | Only as the deprecated aliases below (department slug, `ORDER_*` / `ORDERS_*` settings, hosted role ids) and a Foundry project name |
| **Northwind Insurance** (current) | Fictional P&C insurer (WA and CO auto): an auto claim from first notice of loss, evidence review and a human-reviewed claim action draft | Everything else: [`data/northwind/company.yaml`](../../data/northwind/company.yaml), `agents/insurance/`, `/insurance/*` API routes ([README](../../README.md)) |

**Earlier names and what to use now** ([README: Configuration reference](../../README.md#configuration-reference)
names the files that hold the alias maps):

| Earlier name | Status | Use now |
|---|---|---|
| Department slugs `order-concierge`, `employee-help-desk`, `claims-warranty` | Deprecated alias, still accepted in `X-Department` and as `FOUNDRY_PROJECT_ENDPOINTS` keys (`evals/run_evals.py` also reads an older `order-concierge` baseline key) | `claims-concierge` (flagship), `adjuster-support`, `claims-documents`; `contact-center` never changed. Names and paths: [departments.md](../departments.md#the-four-departments), [`company.yaml`](../../data/northwind/company.yaml) |
| Hosted role ids `manager`, `orderlookup`, `policylookup`, `resolutiondraft` | Deprecated alias (`ROLE_ALIASES` in `agents/hosted/config.py`) | `orchestrator`, `policy-claim-lookup`, `policy-wording`, `claim-action-draft` |
| `ORDER_PROJECT_NAME`, `ORDER_AGENTS_HOSTED`, `ORDER_AGENT_ROLE`, `ORDERS_*` | Deprecated env aliases | `INSURANCE_PROJECT_NAME`, `HOSTED_AGENTS_ENABLED`, `AGENT_ROLE`, `AGENT_*` |
| `MCP_TECH_LOOKUP_URL`, `MCP_WORK_ORDER_URL`; `MCP_GATEWAY_CONNECTION_TECH_LOOKUP`, `MCP_GATEWAY_CONNECTION_WORK_ORDER` | Deprecated env aliases | `MCP_POLICY_LOOKUP_URL`, `MCP_CLAIM_REVIEW_URL`; `MCP_GATEWAY_CONNECTION_POLICY_LOOKUP`, `MCP_GATEWAY_CONNECTION_CLAIM_REVIEW` |
| `x-orders-workflow` request header | Deprecated alias; callers still send both | `x-workflow-id` |
| `GET /work-orders`, `DELETE /work-orders/{hitl_id}` | Deprecated routes, still served | `GET /hitl/resolved`, `DELETE /hitl/{hitl_id}` |
| `mcp-tech-lookup`, `mcp-work-order`, `web-tech` (app, image and directory names) | Retired by the rename; a registry repository may still carry the old name ([legacy-decommission](../legacy-decommission.md#4-container-apps-rg-northwind-apps)) | `mcp-policy-lookup`, `mcp-claim-review`, `web-portal` (Container Apps `ca-insurance-lookup`, `-review`, `-portal`) |
| `POST /chat`, `POST /qa`, `POST /agent/{slug}/chat`; MCP tools `lookup_order`, `propose_resolution` | Removed | `POST /insurance/chat`; `lookup_policy`, `lookup_claim`, `draft_claim_action` |
| `NORTHWIND_BUSINESS_DOMAIN`, `NORTHWIND_MCP_INSURANCE_ONLY` | Removed flags, ignored when set (the hosted-agent deploy script rejects any domain but `insurance`). `business_domain: insurance` survives only as a data tag | Nothing: the code is insurance-only |
| The model bake-off (`/bakeoff/*`), the safety tab (`/safety/*`), HR/IT help-desk content, the CU ingest worker | Removed features (the red-team CI in `pipelines/red-team` stays) | None |

- **Live identifiers are not renamed from code.** They are bound to the running deployment: the API Management
  instance `apim-northwind` (stack `insurance`; API ids `insurance-api`, `insurance-voice`,
  `insurance-mcp-tech-lookup` and `insurance-mcp-work-order`; gateway paths `/insurance/mcp/tech-lookup` and
  `/insurance/mcp/work-order`, which front `ca-insurance-lookup` and `ca-insurance-review`), the AI Gateway
  `ai-gateway-northwind` with tool servers `northwind-insurance-{lookup,knowledge,review}`, Search
  `northwind-insurance-knowledge` and `nw-insurance-adjuster-support`, the Cosmos database `northwind`, the resource
  groups `rg-northwind-*` and the Foundry project names
  ([api-management.md](../api-management.md#what-exists-in-rg-northwind-apps),
  [architecture.md](../architecture.md#data-stores)).
- **Foundry project names.** Configuration values, not product names: `company.yaml`, the Bicep defaults, the
  reuse-mode check and the API default use the live names (`nw-insurance-claims`, `nw-insurance-support`,
  `nw-insurance-documents`, `nw-insurance-contact-center`). The original native flagship project `northwind-orders`
  is still accepted by the API, `scripts/deploy-hosted-agents.py` and `scripts/validate-reuse.py`
  (`INSURANCE_PROJECTS` in [`insurance.py`](../../apps/api/app/insurance.py)). Which names exist in a tenant is an
  operator fact this repository cannot verify ([departments.md](../departments.md#the-four-departments),
  [gotchas](gotchas/names-and-retired-vocabulary.md)).
- **The `northwind` stack is not managed here.** The earlier stack (its Container Apps, API Management APIs
  `northwind-*`, AI Gateway tool servers, Foundry agents and Search indexes) is not deployed, released or tested by
  this repository ([README](../../README.md#legacy-northwind-stack)). `NORTHWIND_GATEWAY_STACK` must be
  `insurance`: an azd environment that still says `northwind` fails `preprovision`.

## Platform and agents

- **Shared AI Services account / Foundry project.** One account, one project per department, one user-assigned
  managed identity (UAMI), account-scoped RBAC inherited by every project
  ([architecture.md](../architecture.md#foundry-projects),
  [Networking, regions and identity](../architecture.md#networking-regions-and-identity)). The project names are
  covered under [Naming lineage](#naming-lineage).
- **Prompt agent.** A Foundry Agent Service agent defined by `data/northwind/departments/<slug>/prompts/*.yaml` and
  created by `scripts/create-prompt-agents.py` (dry run first). Three are defined:
  `northwind-insurance-adjuster-support`, `northwind-claims-triage` and `northwind-intent-classifier`; the flagship
  has none because its agents are hosted. Prompt files are the source of truth
  ([ADR 0001](decisions/0001-prompt-files-are-source-of-truth.md),
  [departments.md](../departments.md#source-of-truth)).
- **Hosted agent.** A container-hosted Foundry agent. One runtime (`agents/hosted`, entrypoint
  `agents/orchestrator/main.py`) serves every role and `AGENT_ROLE` selects it; Responses protocol on port 8088.
  Five are defined: `northwind-insurance-{orchestrator,triage,policy-claim-lookup,policy-wording,claim-action-draft}`,
  with prompts in `agents/insurance/*.yaml`. They are deployed by `scripts/deploy-hosted-agents.py`, **not** by azd.
  Versions are immutable and a selector routes traffic to one
  ([architecture.md](../architecture.md#hosted-agents),
  [gotchas](gotchas/agents-and-approvals.md#hosted-agent-versions)).
- **Copilot SDK harness (Agent Harness).** An optional, application-hosted orchestration loop inside FastAPI
  (`/harness/runs`), enabled by `COPILOT_HARNESS_ENABLED`. It is **not** a Foundry hosted agent, **not** a fifth
  department and has no portal tab ([architecture.md](../architecture.md#copilot-sdk-agent-harness),
  [differentiators.md](../differentiators.md#harness-choice-without-replacing-the-foundry-platform),
  [departments.md](../departments.md#what-each-department-shows)).
- **Copilot coding agent.** GitHub's cloud agent that drafts fix PRs for `copilot-fix` issues such as the
  `eval-regression` template ([`copilot-setup-steps.yml`](../../.github/copilot-setup-steps.yml),
  [genaiops.md](../genaiops.md#auto-remediation)). Unrelated to the Copilot SDK harness despite the shared word.
- **MCP servers.** Small FastMCP apps the agents call as tools: `mcp-policy-lookup` (read-only `lookup_policy` and
  `lookup_claim`; Container App `ca-insurance-lookup`) and `mcp-claim-review` (`draft_claim_action`, which writes
  the HITL queue; `ca-insurance-review`). `scripts/bundle-insurance-assets.py` copies the shared records module and
  the gateway guard into each MCP image ([architecture.md](../architecture.md#mcp-servers)).
- **HITL (`/hitl`).** The human-in-the-loop approval queue. Agents only enqueue *pending* items; approval and
  business execution are never agent tools, and approving a draft is a review decision only
  (`execution_status: not_executed`) ([architecture.md](../architecture.md#human-approval)).
- **Claim action draft.** The reviewable artifact an agent proposes, one of five action types (`request_evidence`,
  `assign_adjuster`, `customer_update`, `escalate_review`, `settlement_recommendation`). It lands in HITL as a
  pending item ([architecture.md](../architecture.md#human-approval)).
- **`X-Department` header.** Sent on every portal call; API middleware resolves it to the department's Foundry
  project endpoint (`FOUNDRY_PROJECT_ENDPOINTS`) and stamps spans with `department` and `foundry_project`
  ([architecture.md](../architecture.md#foundry-projects)).
- **Voice Live / Content Understanding (CU).** The realtime voice front door (browsers get a short-lived app
  connect ticket, never an Azure token) and Foundry document extraction (called directly by the API for claim
  documents) ([architecture.md](../architecture.md#voice-live),
  [claim documents](../architecture.md#claim-documents-and-evidence-records)).

## Gateways

- **`apim-northwind` (classic API Management).** Fronts the apps' REST, WebSocket and MCP surfaces and
  **forwards** client headers. Opt-in and off by default in a fresh deployment; the status notes at the top of
  [api-management.md](../api-management.md) say what is applied live.
- **`ai-gateway-northwind` (AI Gateway tier).** Fronts model inference (hosted agents and the API app, not
  prompt-agent models) and header-free MCP (lookup, knowledge base) and **drops** client headers. Opt-in and off
  by default in a fresh deployment. Its lookup and review tool servers are declared to call the apps through
  `apim-northwind` ([`infra/live/ai-gateway.json`](../../infra/live/ai-gateway.json)), so those calls pass both
  gateways ([ai-gateway.md](../ai-gateway.md#two-gateways)).
- **Gateway guard.** `apps/api/app/gateway_guard.py` plus `NORTHWIND_GATEWAY_AUTH_REQUIRED=true`: the app
  rejects requests without a valid gateway token; `/health` (API) and `/healthz` (MCP apps) stay open
  ([api-management.md](../api-management.md#security-model),
  [ADR 0003](decisions/0003-gateways-opt-in-fail-closed-two-tiers.md)).
- **Agents product and key (`<stack>-agents`).** The API Management product whose subscription key MCP callers
  send as `Ocp-Apim-Subscription-Key`, one per stack (`insurance-agents` here; `northwind-agents` belongs to the
  earlier stack this repository no longer manages); browser calls to
  the REST API need no key. Keys never appear in the repository
  ([api-management.md](../api-management.md#security-model), [gotchas](gotchas/api-and-gateways.md)).

## Evals and delivery

Triggers below come from the workflow files; [genaiops.md](../genaiops.md#the-four-eval-tiers) is the
narrative.

| Tier | Workflow | Runs on | Purpose |
|---|---|---|---|
| 1 Smoke | [`smoke-eval.yml`](../../.github/workflows/smoke-eval.yml) | PRs touching `agents/`, `apps/api/`, `apps/mcp-claim-review/`, `evals/` or `data/golden/` | Free GitHub Models check; no Azure credentials |
| 2 Golden set | [`evals-on-pr.yml`](../../.github/workflows/evals-on-pr.yml) | The same paths | The 50-row golden set posted to `POST /insurance/chat`, scored with Foundry evaluators and compared with `evals/baseline.json` |
| 3 Red team | [`red-team.yml`](../../.github/workflows/red-team.yml) | Weekly (Mon 09:00 UTC), pushes to `main` touching agent, rubric, red-team or harness paths, manual | PyRIT-backed jailbreak, harm, PII and tool-abuse attacks |
| 4 Continuous | [`continuous-eval.yml`](../../.github/workflows/continuous-eval.yml) | Nightly 07:00 UTC, manual | Re-scores about 5% of the last 24 hours of `AI-conversation` traces (emitted by Copilot SDK harness runs); drift alerting |

- **Golden set.** JSONL eval rows per department at `data/northwind/departments/<slug>/golden/dataset.jsonl`
  (at least 10 rows for a new department); the flagship uses `data/golden/dataset.jsonl` (50 rows) and
  `data/golden/harness.jsonl` (17 rows, Copilot SDK harness) ([departments.md](../departments.md#adding-a-new-department)).
  Tier 2 scores only the 50-row set: the harness set is run by hand and no workflow runs the other departments'
  rows ([genaiops.md](../genaiops.md#what-is-and-is-not-enforced-today)).
- **`evals/baseline.json`.** The regression reference (schema v2), keyed by **department slug** so one department's
  regression cannot hide in another's numbers; an older `order-concierge` key is still read. Changes only with
  explicit reviewer approval ([architecture.md](../architecture.md#evaluation-baselines),
  [ADR 0002](decisions/0002-four-eval-tiers-and-department-baselines.md)).
- **Prompt registry.** On push to `main` touching `agents/insurance/`, `agents/copilot-harness/` or
  `evals/evaluators/rubrics/`, [`prompt-registry.yml`](../../.github/workflows/prompt-registry.yml) publishes the
  agent, harness and rubric prompts to the Foundry Prompt Store tagged with the git SHA; department prompts are not
  published ([genaiops.md](../genaiops.md#prompt-as-code)).
- **Drift detector, red team and optimizer.** [`drift-detector.yml`](../../.github/workflows/drift-detector.yml)
  runs after Continuous Eval and files `eval-regression` issues (label `copilot-fix`);
  [`optimizer.yml`](../../.github/workflows/optimizer.yml) opens PRs labelled `optimizer, needs-review`. The
  red-team job fails a run below its pass rate but files no issue itself
  ([genaiops.md](../genaiops.md#the-loop),
  [what is and is not enforced](../genaiops.md#what-is-and-is-not-enforced-today)).
- **Reuse mode.** `NORTHWIND_REUSE_EXISTING_RESOURCES=true` turns `azd provision` into a reference-only adoption:
  `infra/main.bicep` skips every resource module and `infra/modules/existing-resources.bicep` only reads the
  existing resources. `scripts/validate-reuse.py` validates the inputs in azd's `preprovision` and `predeploy` hooks
  ([`azure.yaml`](../../azure.yaml), [README](../../README.md#existing-deployment-reuse-mode)).
