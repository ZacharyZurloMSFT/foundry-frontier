# Glossary

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry
> disagrees with the code, fix the entry in the same PR.

Terms that are easy to misread here. Names changed over time, so start with
[Naming lineage](#naming-lineage).

## Naming lineage

The repo was reshaped twice. Old names survive in identifiers, file names and comments, so a name
alone does not tell you which era it belongs to.

| Era | Story | Where it still shows up |
|---|---|---|
| **Field Services** (original scaffold) | Customer voice, orchestrator, four prompt agents, HITL approval, work order | `mcp-tech-lookup` / `mcp-work-order` app and directory names ([README](../../README.md#repository-layout)); `agents/tech_lookup.yaml`, `agents/work_order_draft.yaml`; legacy workflow keys in `evals/run_evals.py`; the one-line story in [`.github/copilot-instructions.md`](../../.github/copilot-instructions.md) |
| **Order Concierge** (retail reshape) | Customer contact, then a refund / reship / credit / RMA / cancel draft, then human approval | Department slug `order-concierge`; `agents/`; `data/seed/`, `data/golden/`; business domain `retail` |
| **Northwind Insurance** (current framing) | Fictional P&C insurer: auto claim from first notice of loss, evidence review, human-reviewed action draft | [`data/northwind/company.yaml`](../../data/northwind/company.yaml); business domain `insurance`; `agents/insurance/`; `/insurance/*` API routes |

**Slugs are stable; display names changed** ([departments.md](../departments.md#the-four-departments)):

| Slug | Display name today |
|---|---|
| `order-concierge` (flagship) | Claims Concierge |
| `employee-help-desk` | Employee & Adjuster Support |
| `claims-warranty` | Claims Document Intelligence |
| `contact-center` | Policyholder Contact Center |

Each department's paths, Foundry project and search index are in
[`company.yaml`](../../data/northwind/company.yaml).

- **Business domain (`retail` or `insurance`).** Set by `businessDomain` in `infra/main.bicep` (default
  `retail`) and the `NORTHWIND_BUSINESS_DOMAIN` variable. It changes what `postprovision` does and which
  agents are active ([gotchas](gotchas/deployment-and-releases.md#azd-up-and-deployment)).
- **Foundry projects `northwind` and `northwind-orders`.** Distinct. `northwind` is the retained legacy web
  project (its manager lives in `agents/legacyorchestrator`); `northwind-orders` is the native flagship
  project that hosts the five hosted agents. Neither was renamed or merged
  ([departments.md](../departments.md#the-four-departments), [architecture.md](../architecture.md#foundry-projects)).
  The live insurance projects use different names, see [gotchas](gotchas/retail-vs-insurance.md).
- **Container Apps stacks `northwind` and `insurance`.** Two stacks run the same code and are published side
  by side. The `northwind` stack (`ca-api`, `ca-mcp-*`, `ca-web-*`) is the repository-managed deployment; the
  `insurance` stack (`ca-insurance-*`) was created out of band. The release workflow builds only its two SPAs;
  its API and MCP apps are released by hand, so a merge does not reach them on its own
  ([what exists](../api-management.md#what-exists-in-rg-northwind-apps),
  [cut-over](../api-management.md#insurance-stack-cut-over-2026-10-23)).

## Platform and agents

- **Shared AI Services account / Foundry project.** One account, one project per department plus the legacy
  `northwind` project, one user-assigned managed identity (UAMI), account-scoped RBAC inherited by every
  project ([architecture.md](../architecture.md#foundry-projects),
  [Identity & access](../architecture.md#identity--access)).
- **Prompt agent.** A Foundry Agent Service agent defined by YAML (`agents/*.yaml`, or
  `data/northwind/departments/<slug>/prompts/*.yaml`) and created by `scripts/create-prompt-agents.py`. Prompt
  files are the source of truth ([ADR 0001](decisions/0001-prompt-files-are-source-of-truth.md),
  [departments.md](../departments.md#source-of-truth)).
- **Hosted agent.** A container-hosted Foundry agent deployed by azd (`host: azure.ai.agent`), Responses
  protocol on port 8088. Five are defined: `northwind-orchestrator` (the manager), `northwind-triage`,
  `northwind-order-lookup`, `northwind-policy-lookup`, `northwind-resolution-draft`; `ORDER_AGENT_ROLE` selects
  the role. The live insurance project runs `northwind-insurance-*` counterparts. Versions are immutable and a
  selector routes traffic to one ([`azure.yaml`](../../azure.yaml),
  [architecture.md](../architecture.md#order-concierge-hosted-runtime),
  [ai-gateway.md](../ai-gateway.md#live-inventory),
  [gotchas](gotchas/agents-and-approvals.md#hosted-agent-versions)).
- **Copilot SDK harness (Agent Harness).** An optional, application-hosted orchestration loop inside FastAPI
  (`/harness/runs`), enabled by `COPILOT_HARNESS_ENABLED`. It is **not** a Foundry hosted agent and **not** a
  fifth department ([architecture.md](../architecture.md#copilot-sdk-agent-harness),
  [differentiators.md](../differentiators.md#harness-choice-without-replacing-the-foundry-platform),
  [departments.md](../departments.md#agent-harness-platform-workspace)).
- **Copilot coding agent.** GitHub's cloud agent that drafts fix PRs for `copilot-fix` issues such as the
  `eval-regression` template ([`copilot-setup-steps.yml`](../../.github/copilot-setup-steps.yml),
  [genaiops.md](../genaiops.md#auto-remediation)). Unrelated to the Copilot SDK harness despite the shared word.
- **MCP servers.** Small FastMCP apps the agents call as tools, built from two images: `mcp-tech-lookup`
  (`lookup_order`) and `mcp-work-order` (`propose_resolution`, HITL queue). The northwind stack runs them as
  `ca-mcp-tech-lookup` and `ca-mcp-work-order`; the insurance stack runs the same images as `ca-insurance-lookup`
  and `ca-insurance-review`, where `NORTHWIND_MCP_INSURANCE_ONLY=true` limits them to the insurance tools
  (`lookup_policy` and `lookup_claim`; `draft_claim_action`)
  ([api-management.md](../api-management.md#what-exists-in-rg-northwind-apps)).
- **HITL (`/hitl`).** The human-in-the-loop approval queue. Agents only enqueue *pending* items; approval and
  business execution are never agent tools, and approving an insurance draft is an internal decision only
  ([architecture.md](../architecture.md#order-concierge-hosted-runtime),
  [departments.md](../departments.md#active-insurance-surfaces-and-rollout-boundary)).
- **Resolution / work order / action draft.** The reviewable artifact an agent proposes: a retail "resolution"
  or an insurance "claim action draft" (one of five allowed action types). Both land in HITL.
- **`X-Department` header.** Sent on every call; API middleware resolves it to the department's Foundry project
  endpoint (`FOUNDRY_PROJECT_ENDPOINTS`) and stamps spans with `department` and `foundry_project`
  ([architecture.md](../architecture.md#foundry-projects)).
- **Bake-off.** A UI tab that races model deployments; by default a 2-way race on the planner and worker
  deployments (`bakeoffDeployments` defaults to `[]`)
  ([departments.md](../departments.md#adding-partner-models-to-the-bake-off-tab)).
- **Voice Live / Content Understanding (CU).** The realtime voice front door (browsers get a short-lived app
  connect ticket, never an Azure token) and Foundry document extraction (called directly by the API for
  insurance documents) ([departments.md](../departments.md#active-insurance-surfaces-and-rollout-boundary)).

## Gateways

- **`apim-northwind` (classic API Management).** Fronts the apps' REST, WebSocket and MCP surfaces and
  **forwards** client headers. Opt-in and off by default in a fresh deployment; its Status section says what is
  applied live ([api-management.md](../api-management.md)).
- **`ai-gateway-northwind` (AI Gateway tier).** Fronts model inference (hosted agents and the API app, not
  prompt-agent models) and header-free MCP (lookup, knowledge base) and **drops** client headers. Opt-in and off
  by default in a fresh deployment. Its insurance lookup and review tool servers reach the locked apps through
  `apim-northwind`, so those calls pass both gateways ([ai-gateway.md](../ai-gateway.md#two-gateways)).
- **Gateway guard.** `apps/api/app/gateway_guard.py` plus `NORTHWIND_GATEWAY_AUTH_REQUIRED=true`: the app
  rejects requests without a valid gateway token; `/health` and `/healthz` stay open
  ([api-management.md](../api-management.md#security-model),
  [ADR 0003](decisions/0003-gateways-opt-in-fail-closed-two-tiers.md)).
- **Agents product and key (`<stack>-agents`).** The API Management product whose subscription key MCP callers
  send as `Ocp-Apim-Subscription-Key`, one per stack (`northwind-agents`, `insurance-agents`); browser calls to
  the REST API need no key. Keys never appear in the repository
  ([api-management.md](../api-management.md#security-model), [gotchas](gotchas/api-and-gateways.md)).

## Evals and delivery

Triggers below come from the workflow files; [genaiops.md](../genaiops.md#the-four-eval-tiers) is the
narrative.

| Tier | Workflow | Runs on | Purpose |
|---|---|---|---|
| 1 Smoke | [`smoke-eval.yml`](../../.github/workflows/smoke-eval.yml) | PRs touching `agents/`, `apps/api/`, `evals/`, `data/golden/`, `apps/mcp-work-order/` | Free GitHub Models check; no Azure credentials |
| 2 Golden set | [`evals-on-pr.yml`](../../.github/workflows/evals-on-pr.yml) | Same paths, plus `apps/web-tech/src/harness*` | Foundry evaluators against the golden sets; blocks regressions vs baseline |
| 3 Red team | [`red-team.yml`](../../.github/workflows/red-team.yml) | Weekly (Mon 09:00 UTC), pushes to `main` touching agent or harness paths, manual | PyRIT-backed jailbreak, harm, PII and tool-abuse attacks |
| 4 Continuous | [`continuous-eval.yml`](../../.github/workflows/continuous-eval.yml) | Nightly 07:00 UTC, manual | Re-scores a 5% sample of production traces; drift alerting |

- **Golden set.** JSONL eval rows per department at `data/northwind/departments/<slug>/golden/dataset.jsonl`
  (at least 10 rows for a new department); the flagship uses `data/golden/dataset.jsonl`
  ([departments.md](../departments.md#adding-a-new-department)).
- **`evals/baseline.json`.** The regression reference (schema v2), keyed by **department** so one department's
  regression cannot hide in another's numbers. Changes only with explicit reviewer approval
  ([architecture.md](../architecture.md#evaluation-baselines), [ADR 0002](decisions/0002-four-eval-tiers-and-department-baselines.md)).
- **Prompt registry.** On push to `main` touching `agents/**`, [`prompt-registry.yml`](../../.github/workflows/prompt-registry.yml)
  publishes prompts to the Foundry Prompt Store tagged with the git SHA.
- **Drift detector, red team and optimizer.** [`drift-detector.yml`](../../.github/workflows/drift-detector.yml)
  and the red-team job open GitHub issues; [`optimizer.yml`](../../.github/workflows/optimizer.yml) opens PRs
  labelled `optimizer, needs-review` ([genaiops.md](../genaiops.md#the-loop)).
- **Reuse mode.** `NORTHWIND_REUSE_EXISTING_RESOURCES=true` makes `postprovision` skip all bootstrap;
  `scripts/validate-reuse.py` validates reference-only provisioning inputs in azd's `preprovision` and
  `predeploy` hooks ([`azure.yaml`](../../azure.yaml),
  [README](../../README.md#existing-deployment-split-group-reuse-opt-in)).
