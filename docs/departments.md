# Northwind Insurance — one fictional insurer, four departments, one Foundry account

The Northwind demo is organized around **one fictional mid-market
property-and-casualty insurer** (Northwind Insurance) with **four departments**, each
backed by its own **Foundry project** inside a shared AI Services
account. An authorised operator verifies each target project and deployment
scope before rollout. An SE can navigate to the appropriate department — same VNet, same
UAMI, same model quota, but each department has its own agents,
connections, evaluators, red-team runs, and Foundry Observability lens.

## The four departments

| Department                    | Foundry pillar         | Foundry project            | Search index         | Prompt agents                                                              |
| ----------------------------- | ---------------------- | -------------------------- | -------------------- | -------------------------------------------------------------------------- |
| **Claims Concierge** (flagship) | Records + Multi-agent + HITL | `northwind` (legacy web), `northwind-orders` (native) | `northwind-insurance-knowledge` | Stable specialist names retained; insurance behavior requires authorised rollout |
| **Employee & Adjuster Support** | Read-only Search + model RAG | Department namespace retained; shared model client | `nw-insurance-adjuster-support` | Server-validated verbatim evidence with title/version/source citations; no retail HR-agent fallback |
| **Claims Document Intelligence** | Live Content Understanding | Shared AI Services account; department namespace retained | No automatic indexing | `northwindInsuranceClaimsV1`, actual document bytes and typed fields/confidence/source spans |
| **Policyholder Contact Center** | Native Voice Live WSS | Shared AI Services account; `nw-contact-center` namespace retained | No voice RAG claim | Insurance intake, completed transcripts and explicitly confirmed claims handoff |

Internal slugs `order-concierge`, `employee-help-desk`, `claims-warranty` and
`contact-center` remain unchanged. Legacy web `northwind` and native
`northwind-orders` are distinct projects, not renamed or merged.

## Active insurance surfaces and rollout boundary

Claims / Policies loads synthetic policyholders, auto policies, coverage
schedules, endorsements, loss dates and evidence from `GET /insurance/fixtures`.
It prepares `POST /insurance/actions/draft` payloads with linked claim,
policy and policyholder IDs, one of five allowed action types, summary,
source references and an optional proposed settlement amount.
Saved drafts enter the existing `/hitl` human-review queue. Approval is an
internal draft decision only: no insurance claim approval/denial, external
message, real adjuster assignment or payment execution.

Insurance chat uses **only** `POST /insurance/chat`, with a fresh readiness
check before sending. Chat, harness execution, voice and contact handoff are
unavailable until `/insurance/fixtures` reports `agent_runtime_ready: true`.
The server's `NORTHWIND_BUSINESS_DOMAIN=insurance` and authorised
`NORTHWIND_INSURANCE_AGENTS_READY` rollout remain the runtime boundary.
Readiness also requires separately configured `northwind-insurance-*` native
agents in `/api/projects/northwind-orders`; the legacy web
`/api/projects/northwind` and old retail agent names are not compatible.
The converted Copilot SDK harness preserves history and uses insurance
lookup/policy/draft tools. `COPILOT_HARNESS_ENABLED` controls activation;
configured dependencies are not labelled ready until a real SDK run completes.
No custom learning/training screen or automatic training is added to the web app.

Document extraction uses the API directly; `ca-cu-ingest` is not required.
Allowlisted synthetic PDF inputs and raw bounded PDF/JPEG/PNG uploads go to
live CU. Expected golden extractions are never returned as inference.
The UI preserves source provenance and confidence; no automatic indexing or
claim-record mutation is implied. Source bytes are not stored by the API.
FNOL, repair estimates, supporting invoices and damage evidence are source
facts only; photos cannot establish cause, fault, fraud or coverage.

Voice Live authenticates through the API's managed identity. Browsers receive
only a short-lived app connect ticket, never an Azure token or key. Ticket
digests, origin, expiry and atomic ETags live in the existing workflow store
with a 60-second TTL. Only completed transcripts populate the editable intake;
an operator confirms the typed summary before POST `/handoff`. Raw audio is not
persisted, and partial transcripts never trigger an action.

The user accepted the immutable v2 synthetic package for evaluation-only use,
not training or individual annotation. Five final review findings in SDK
metadata checks and scoring were fixed. Reviewed execution-code overlays keep
the prepared and executed source hashes separate without rewriting approved
data. File-upload permission and verifiable all-call spend bounds remain
independent mandatory gates before native evaluation.

Active source forms/procedures carry fictional, version, effective-date,
jurisdiction and citation context. `data/seed/generate.py --domain insurance`
builds the local flagship PDF manifest without cloud/model calls.
`--domain retail` retains explicit historical generation; old retail files
are not referenced by the active insurance manifest.

Insurance retrieval is isolated: index `northwind-insurance-knowledge`, knowledge
source `northwind-insurance-knowledge-ks`, knowledge base `northwind-insurance-kb`
and optional native `northwind-orders` connection `northwind-insurance-kb-mcp`.
The existing Search service may be reused, but the retail `northwind-knowledge`
index and `northwind-kb` knowledge base remain unchanged and are not insurer
corpora. Manifest names describe intended targets, not proof that cloud resources
exist or contain current content. An authorised operator reviews the dry-run
plan from `scripts/publish-insurance-knowledge.py` before explicitly publishing
the isolated corpus and verifying converted agents. The legacy
`data/seed/index.py` and `upload_blobs.py` reject insurance manifests so they
cannot populate the old retail index or shared seed container.

## Source of truth

Everything about a department lives under
`data/northwind/departments/<slug>/`:

- `department.yaml` — manifest slice
- `seed/*.md` — synthetic corpus indexed into the department's Search index
- `golden/dataset.jsonl` — golden eval rows for that department's agents
- `prompts/*.yaml` — Foundry prompt-agent definitions
- `cu-analyzers/*.json` — (Claims only) Content Understanding analyzer schemas

The top-level `data/northwind/company.yaml` composes these into a single
manifest read by:

- **API** (`apps/api/app/company.py`) → serves `GET /company` + resolves
  `X-Department` headers to project endpoints.
- **web-tech** (`CompanyContext`) → renders the landing tiles + wires
  the sidebar navigation.
- **`scripts/create-prompt-agents.py`** → iterates every department and
  creates its agents in the department's own Foundry project.
- **`scripts/create-department-indexes.py`** → seeds each new
  department's Search index from the department's `seed/` corpus.

## Where each Foundry project lives in Bicep

`infra/modules/foundry.bicep` provisions the shared AI Services account
and the stable flagship projects unchanged. Additional projects come in
via the `additionalProjects` param (defaulted in `infra/main.bicep`):

```bicep
additionalProjects = [
  { slug: 'employee-help-desk', projectName: 'nw-employee-help-desk', ... }
  { slug: 'claims-warranty',    projectName: 'nw-claims-warranty',   ... }
  { slug: 'contact-center',     projectName: 'nw-contact-center',    ... }
]
```

The account-scoped RBAC roles (`Azure AI User`,
`Cognitive Services OpenAI User`) grant the UAMI + dev principal
access on every project by inheritance — no per-project role
assignments needed.

Endpoints are exported as a JSON map on the `FOUNDRY_PROJECT_ENDPOINTS`
env var, which the API reads at startup.

## Adding partner models to the bake-off tab

The Bicep default for `bakeoffDeployments` is `[]` — the bake-off tab
runs a **2-way race** on the always-deployed planner + worker
deployments, which works on any Azure subscription without extra
catalog gymnastics.

To add a third (or fourth) model to the race — for example a
Moonshot Kimi, a Meta Llama, or a DeepSeek from the **Foundry Models
catalog** — set the array via `azd env set` **before** running
`azd provision`:

```powershell
# PowerShell
azd env set BAKEOFFDEPLOYMENTS '[{"name":"kimi-k2.6","version":"2026-07-01","capacity":50,"publisher":"moonshot","format":"Moonshot"}]'
```

```bash
# bash
azd env set BAKEOFFDEPLOYMENTS '[{"name":"kimi-k2.6","version":"2026-07-01","capacity":50,"publisher":"moonshot","format":"Moonshot"}]'
```

Then `azd provision`. Each entry becomes a
`Microsoft.CognitiveServices/accounts/deployments` resource on the
shared AI Services account, and the bake-off endpoint
(`FOUNDRY_BAKEOFF_DEPLOYMENTS`) picks up every deployment name.

**Catalog availability varies by subscription + region.** If ARM
rejects the model with `DeploymentModelNotSupported`, check the
Foundry portal → Deployments → **+ Deploy model** dialog for what's
actually listed in your AI region (defaults to `eastus2`), then update
the array to a name/version that appears there.

The eval Tier-4 pipeline still keys metrics per-department regardless
of how many bake-off models are configured.

## Agent Harness platform workspace

`agent_harness` in `company.yaml` describes a platform capability, not a fifth
department. Its explicit UI entry is available from the landing page and
department navigation. Entering it preserves the selected department's chat
state. Its insurance calls remain scoped to the stable `order-concierge`
department and `northwind-orders`, even when opened from another department.

The Python Copilot SDK runs in the API with four registered insurance tools
and workflow-bound, pending-only action persistence. No additional Foundry
project or Container App is
provisioned. `COPILOT_HARNESS_ENABLED` controls execution independently of
workspace visibility.

## Adding a new department

1. Add a `data/northwind/departments/<slug>/` directory with a
   `department.yaml`, a `seed/` corpus, at least one `prompts/*.yaml`,
   and a `golden/dataset.jsonl` with ≥10 rows.
2. Add the department to `data/northwind/company.yaml` (top-level).
3. Add a matching entry to the `additionalProjects` array in
   `infra/main.bicep`.
4. Add a `UI tab` entry: create a tab component under
   `apps/web-tech/src/tabs/` and wire it into `DEPT_TABS` in `App.tsx`.
5. Run `azd provision && azd deploy` — postprovision picks up every
   department automatically.

## Cross-department flow

The most compelling demo is the **cross-department finale**:

1. Browse **Claims Concierge** records and select CLM-2001 / POL-1001.
2. Inspect a synthetic FNOL, estimate or invoice in **Claims Document
   Intelligence**, noting provenance, source references and uncertainty.
3. Prepare an evidence request or review recommendation; save an internal draft.
4. A human reviews the draft in Approvals; no external action or claim
   adjudication occurs.
5. After authorised converted-agent rollout, **Policyholder Contact Center**
   can handle loss intake/status and send an insurance-safe summary to
   **Claims Concierge** for an adjuster handoff.
6. **Employee & Adjuster Support** preserves HR/IT retrieval and adds
   versioned adjuster procedures after the corpus is refreshed.

Same UAMI, same VNet, same account, same eval loop — four different
customer archetypes all served by one deployment.
