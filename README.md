# Northwind Insurance Copilot

An end-to-end, production-shaped demo of **Azure AI Foundry** built on the
fictional Northwind Insurance, a property-and-casualty insurer with
**four departments**, each showcasing a different Foundry pillar. One
shared Foundry account supports the retained retail demo and the separate
insurance release. All insurance records and policy wordings are synthetic.

- **Claims Concierge** (multi-agent + HITL): intake triage, policy/claim
  lookup, cited policy-wording guidance and proposed adjuster actions.
  Human approval reviews a draft only; it does not approve coverage, deny
  a claim, execute a settlement or issue a payment.
- **Employee & Adjuster Support** (RAG): retains HR/IT material and adds
  fictional claims-handling procedures.
- **Claims Document Intelligence**: live Content Understanding over actual
  allowlisted synthetic PDFs or bounded PDF/JPEG/PNG uploads. Results retain
  typed fields, confidence and source spans; no authored extraction fallback.
- **Policyholder Contact Center** (Voice Live): insurance intake and
  claim-status examples, with readiness-gated handoff.

Insurance activation is explicit: `NORTHWIND_BUSINESS_DOMAIN=insurance`.
Records and pending review drafts work independently of agent deployment.
`NORTHWIND_INSURANCE_AGENTS_READY` stays `false` until the dedicated
`northwind-insurance-*` fleet, isolated knowledge base and native
`northwind-orders` endpoint have been deployed and verified. Unavailable
insurance inference returns a surfaced error instead of calling retail agents.
The converted Copilot SDK harness uses insurance tools and pending-only
human-review drafts. SDK/runtime availability is verified by real execution,
not inferred from an enabled flag.

The first insurance release is deployed: the portal/API/MCP services are in
Apps, the isolated 13-document knowledge base is in Data, and the five
dedicated hosted agents are in the existing Orders project in AI. The claims
manager uses supported named-agent endpoints for specialist delegation.
The insurance experience release is now deployed: grounded employee/adjuster
support, the real Copilot SDK harness, native Voice Live through a managed-
identity WebSocket proxy, confirmed claims handoff, and live CU extraction.
The voice proxy never sends a broad Azure credential to the browser. Its
single-use, origin-bound tickets are atomically consumed in Cosmos and work
across API replicas. Physical microphone interaction is not certified by
synthetic service-level smoke checks.

`data/northwind/insurance-fixtures.json` drives shared record validation and
versioned, effective-date/jurisdiction-aware wording citations. Publish the
isolated corpus with `scripts/publish-insurance-knowledge.py`; the retail
corpus is not overwritten. The corrected `insurance-2026-10-14-v2` package
was accepted by the user for evaluation-only upload and a conditional
verified $1 smoke budget. Acceptance is not individual human annotation,
qualified insurance review, training authorization or production approval.
Manual upload remains blocked by Scout's file-upload permission; no native
paid batch is admitted while all-call cost/token bounds remain unverified.

The isolated `nw-insurance-adjuster-support` index contains 19 fictional
HR/IT/adjuster documents. The API uses managed identity; Search retains its
existing key authentication alongside Azure AD. The CU analyzer
`northwindInsuranceClaimsV1` uses existing `gpt-4o` and
`text-embedding-3-small` deployments. CU model-role mappings were configured;
the Foundry account default project was not changed.

Capability switches are `ADJUSTER_SUPPORT_ENABLED`, `COPILOT_HARNESS_ENABLED`,
`NORTHWIND_CU_ENABLED`, `VOICE_LIVE_ENABLED` and `VOICE_LIVE_READY`. Keep
them disabled until their real dependencies and supported transport are
verified. CU upload limit is 10 MiB; extracted facts still require human
review, especially missing or low-confidence fields.

### Connected manual business journey (deployed, activation gated)

`apps/api/app/business` starts the persistent insurance business layer, separate
from AI adapters. It provides typed, run-scoped policy and claim reads,
form-based loss reporting, durable request-hash idempotency, pinned policy and
wording snapshots, explicit workflow transitions, and versioned case history.
The versioned `/api/v1` adapter now connects these services to an opt-in
policyholder portal and adjuster workbench, with manual evidence tasks, notes,
permitted adjuster assignment, and customer-filtered case history.
Actor permissions come from server-owned run mappings; caller-supplied roles or
customer IDs are not part of the loss-report contract. Date and jurisdiction
mismatches remain recorded human-review flags, not automatic denials.

The proposed bounded-demo storage layout uses one run aggregate in the existing
`workflow_state` container, partitioned by `workflowId`. An ETag-protected
replacement atomically saves the claim, original submission receipt, and history.
Runs are capped at 100 claims, 200 history events per claim, and 1.5 MB per
aggregate; contention is intentionally coarse-grained. Seeding is an explicit,
create-only administrative operation, never a fallback after a storage failure.
Retention/reset policy is not implemented or approved by this increment.

Both new surfaces are **disabled by default**. `NORTHWIND_BUSINESS_API_ENABLED`
gates the API; `VITE_INSURANCE_BUSINESS_ENABLED` selects the new business-first
frontend at build time. When disabled, the existing fixture-based demos and
draft-only approvals are unchanged. The application code is deployed through
scoped CI/CD, but insurance identity registrations, participant mappings, seeded
business runs and audience access still require separate approval.

The API verifies delegated **Entra v2 RS256 access tokens**, including signature,
issuer, tenant, audience, expiry, required scope, and permitted client application.
It derives identity as `tenant-UUID:object-UUID`; persisted run mappings determine
roles and customer access. Browser persona switches, JWT role labels and
`X-MS-CLIENT-PRINCIPAL-*` headers cannot grant business permissions. `X-Demo-Run`
selects a run but does not grant access. No client secret or broad Azure token
belongs in the browser; the portal uses MSAL with an in-memory token cache.

Configure the following only after the identity design and participants are
approved. These are application registration IDs, **not** the app's Azure managed
identity ID. The API expects the v2 token's GUID audience, while the browser
requests the full exposed delegated scope URI.

| API configuration | Meaning |
| --- | --- |
| `NORTHWIND_BUSINESS_DOMAIN=insurance` | Explicit insurance domain |
| `NORTHWIND_BUSINESS_API_ENABLED=true` | Enable the new protected routes |
| `INSURANCE_AUTH_TENANT_ID` | Approved audience's Entra tenant GUID |
| `INSURANCE_AUTH_AUDIENCE` | Insurance API application/client GUID |
| `INSURANCE_AUTH_REQUIRED_SCOPE` | Delegated scope name; default `access_as_user` |
| `INSURANCE_AUTH_CLIENT_IDS` | Comma-separated permitted SPA client GUIDs |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | Existing private canonical storage |

Frontend configuration is `VITE_INSURANCE_BUSINESS_ENABLED=true`,
`VITE_INSURANCE_AUTH_TENANT_ID`, `VITE_INSURANCE_AUTH_CLIENT_ID`,
`VITE_INSURANCE_AUTH_SCOPE` (for example `api://<API-GUID>/access_as_user`),
`VITE_INSURANCE_RUN_ID`, and `VITE_API_BASE_URL`. Register the exact frontend
origin/path as an SPA redirect URI and approve the delegated API permission.
Vite settings are build-time values: changing container runtime variables does
not alter an already-built frontend. Docker builds accept the same names as
build arguments. The existing azd prepackage hook inherits these values and the
default Docker build preserves that prepared `dist`. An explicit Docker business
opt-in rebuilds from source: supply the API URL and all identity/run arguments
together, rather than relying on blank build defaults. Identity registration and
consent are separate operator actions.

Seed an isolated run using `scripts/seed-insurance-run.py`. Supply a private JSON
mapping from canonical `tenant-UUID:object-UUID` keys to
`{"role":"policyholder","policyholder_id":"PH-1001"}`, `{"role":"adjuster"}`, or
`{"role":"presenter"}`. Presenter access is read-only in this increment.

```powershell
python scripts\seed-insurance-run.py --run-id northwind-demo --actors-json C:\secure\actors.json
```

The command previews counts only. A separately authorised `--apply` creates the
run in existing Cosmos and refuses to overwrite one; all mappings must match
the configured tenant. The operator must have private-network connectivity and
the appropriate data-plane permission. A failed read never auto-seeds a run.

`scripts/configure-insurance-identity.py` prepares the real API/SPA registrations
without changing existing GitHub deployment identities. Its default preview is
fully offline and requires an explicit tenant, stable deployment UUID, exact SPA
redirect origin, run ID and the same private participant mapping:

```powershell
python scripts\configure-insurance-identity.py --preview `
  --tenant-id "<approved-tenant-uuid>" --deployment-id "<stable-deployment-uuid>" `
  --redirect-uri "https://ca-web-tech.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/" `
  --run-id northwind-demo --actors-json C:\secure\actors.json
```

A separately approved `--apply --journal C:\secure\insurance-identity.json`
reconciles only registrations carrying the exact deployment ownership marker.
It checkpoints confirmed IDs and uncertain writes so an interrupted create is
query-reconciled, not blindly submitted again. Keep that journal outside source
control. The API is single-tenant/v2 and exposes only `access_as_user`; the SPA
uses authorization-code/PKCE, the exact redirect and the API's delegated scope.
No client secret or implicit flow is configured.

Both service principals require participant assignment. `--assign-participants`
is a separate explicit opt-in to assign only the existing tenant users in the
approved map; it never invites users or guesses accounts from display names.
Registration management and participant assignment require an already authorised
operator and appropriate delegated Graph permissions. The tool neither grants
those permissions nor performs administrative consent. It emits proposed API
and frontend settings with both business enablement flags still **false**.
Assignments/consent, authenticated run seeding, real sign-in and negative-access
rehearsal remain prerequisites for activation. Each participant currently has one
server-mapped role per run; there is no client-side persona privilege switch.

Protected routes include `/me`, `/me/policies`, `/me/claims`, `/policies/{id}`,
and `/claims/{id}` under `/api/v1`. Loss reporting requires `Idempotency-Key`;
identical retries return the original result, and changed input conflicts.
Case transitions, tasks, notes and assignment require `expected_version`;
ETags and correlation IDs accompany responses. Errors have the typed shape
`{"error":{"code":"...","message":"...","correlation_id":"..."}}`.
Completing an evidence task is an attributable manual action, not a document
extraction, confirmed fact or automatic workflow transition. No external
communication, coverage adjudication, settlement or payment is implemented.

### Durable evidence increment (deployed, activation gated)

The initial gated code release is `60c8fb7` (16 October 2026). Source and worker
capabilities are shipped, but evidence/business access, uploads, extraction and
worker execution remain disabled pending their separate operational approvals.

The connected journey adds protected claim documents, processing jobs and
individual-field review. Sources are bounded PDF/JPEG/PNG documents, at most
10 MiB, selected from the synthetic allowlist or an independently enabled upload
path. Every submission requires synthetic-data acknowledgement and an
idempotency key. Original bytes and raw extraction/provenance live in immutable
private Blob objects; the existing run aggregate atomically stores references,
job state, review metadata and audit through ETag-protected updates.
No new public container, shared-access URL or general Azure token is exposed.
This deliberately bounded increment permits up to eight documents per run and
four per claim, subject to shared capacity. Admission reserves audit/byte space
for worker reconciliation and each document's first six-field review; unrelated
claim commands cannot consume that reserved space. Capacity rejection happens
before source writes, rather than after a paid submission has started.

The portal downloads source bytes through the authorised API and verifies
MIME, length and SHA-256 before offering the original. Available field confidence
and source spans remain untrusted suggestions. Accepting, correcting or clearing
a field is an explicit adjuster decision with source/review version checks and
attribution; the original extraction is not overwritten. Manual correction
works without CU, and confirmation changes no claim workflow or financial state.
Customers see only their permitted case and human-reviewed facts; presenter
accounts have no evidence mutation controls.

Protected evidence routes under `/api/v1` are `/evidence/status`,
`/evidence/samples`, `/claims/{id}/documents` (list/raw upload),
`/claims/{id}/documents/samples`, `/documents/{id}` and
`/documents/{id}/content`, `/jobs/{id}` with explicit `/retry` or `/cancel`,
and `/documents/{id}/reviews`. Uploads are raw request bodies, not document URLs.
Evidence responses omit ETags and use explicit document, review and job versions.
`/me.actor_key` is an opaque, actor/run-specific fingerprint used to isolate
pending browser command keys across sign-in changes; it confers no access.

| Evidence configuration | Default and boundary |
| --- | --- |
| `NORTHWIND_EVIDENCE_ENABLED` | `false`; gates new source submission |
| `NORTHWIND_EVIDENCE_UPLOADS_ENABLED` | `false`; separate operator opt-in for bounded synthetic uploads |
| `NORTHWIND_EVIDENCE_EXTRACTION_ENABLED` | `false`; extraction admission only, not proof of live CU success |
| `NORTHWIND_EVIDENCE_WORKER_ENABLED` | `false`; does not create or schedule a worker |
| `NORTHWIND_EVIDENCE_WORKER_RUN_IDS` | Empty; explicit comma-separated run allowlist, no run discovery |
| `NORTHWIND_EVIDENCE_WORKER_LEASE_SECONDS` | `60`, bounded 30-300 |
| `NORTHWIND_EVIDENCE_WORKER_BATCH_SIZE` | `5`, bounded 1-20 |
| `NORTHWIND_EVIDENCE_WORKER_MAX_ATTEMPTS` | `3`, bounded 1-5 |

Source/job records survive the browser connection. Processing is a separate
bounded process, not an API background task:

```powershell
# Only after approving identities, private connectivity and the CU budget:
Set-Location apps\api
python -m app.business.evidence_worker --run-id northwind-demo --once
# Or explicitly bound repeated work with --iterations <1-60>.
```

The worker additionally requires existing `NORTHWIND_CU_ENABLED` and valid CU
configuration. It uses durable leases to prevent competing workers from
processing the same job, resumes polling a saved provider operation after
restart, and refuses a blind retry if submission outcome is uncertain.
Cancellation stops further application processing; it does not promise to cancel
provider compute already submitted. Closing a browser never cancels the job.
Raw results and confirmed fields remain separate on completion.

**Operational gates remain:** approve/apply and rehearse the private Storage
path; approve participant mappings and activate sign-in; deploy/schedule the
bounded worker with its run allowlist; approve inference budgets; then exercise
actual Blob, identity and CU dependencies. No worker resource or schedule is
created by this increment. Orphan blobs are retained safely, not silently
deleted; scoped cleanup, retention and reset are still separate work. Status
responses describe configuration, not certified connectivity or production
readiness.

The greenfield Bicep parameters carry the API settings with disabled/blank
defaults and permit PATCH/preflight for task completion. The split-group reuse
profile remains read-only and does not update the existing API's environment or
ingress policy; any eventual activation requires an explicitly scoped deployment.

**Still pending:** private Storage/DNS activation, real sign-in and durable
evidence/worker activation, assistant/MCP/voice migration to dynamic scoped claims,
review/application commands, presenter reset, retention and release acceptance.
The new manual portal deliberately does not call legacy fixture-based agents,
employee knowledge or technical labs for new claims. Do not present it as a full
PRD release or as certified live identity/voice/evidence acceptance.

Do not run an unscoped `azd up` against the migrated deployment. Use the
split-group reuse profile and scoped app deployments below. Insurance agents
use the dedicated SDK deployment script, not the retail azd agent services.

### Scoped GitHub Actions delivery

The private source repository is `ZacharyZurloMSFT/foundry-frontier`. Its new
delivery lanes are deliberately separate:

| Workflow | Trigger and boundary |
| --- | --- |
| `CI` | PRs/main/manual; application contracts, deployment contracts, TypeScript, web tests/builds, and Bicep compilation. No Azure login or paid inference. |
| `Scoped App Release` (`deploy-apps.yml`) | Successful trusted CI for current `main`, or explicit main dispatch after successful CI; build and release selected existing apps only. |
| `Scoped Infrastructure` (`deploy-infra.yml`) | Successful trusted main CI or manual plan. Apply is manual and requires the exact reviewed main SHA and a successful same-source plan-run artifact. |
| `Scoped API Management` (`deploy-apim.yml`) | Opt-in (`NORTHWIND_APIM_ENABLED`). Plans the `apim-northwind` API definition after trusted main CI; apply is manual with the reviewed SHA, a same-source plan run and an allow-listed resource boundary. See [API Management](./docs/api-management.md). |

`deploy-progressive.yml` is retired: it can no longer call `azd up` or `azd down`.
Legacy evaluation/training/prompt workflows remain disabled until separately
configured and budgeted; enabling CI does not authorise those jobs.

App releases use immutable image digests and preserve existing environment,
secret references, managed identity, service endpoints and capability flags.
API build metadata reports the actual source SHA. Rollback artifacts contain
previous image/revision and build metadata only, never full app configuration.
Changing the deployment profile does not approve new insurance identities or
enable business-portal flags. Hosted Foundry agents are not deployed by this lane.

Infrastructure is **not** the greenfield stack. The original `infra/main.bicep`
reuse lane remains reference-only; applying it would not manage updates. The
new `infra/live/storage-networking.bicep` manages just four resources in
`rg-northwind-networking`: the Blob private endpoint, its DNS zone/group and
the existing VNet's private-DNS link. It references, but never redeclares, the
existing Storage account, VNet or subnets. It does not enable public networking,
change application images/settings, provision another AI service, or implement
evidence processing. Other infrastructure changes still receive static CI,
but need an explicitly reviewed live management boundary before automated apply.

Plan artifacts bind the complete what-if result, compiled template hash and
reviewed source SHA. Apply re-plans and rejects drift, deletes, out-of-scope
resources or unrecognised changes. It uses incremental deployment and requires
private-endpoint approval/readiness. A private endpoint is not proof of
end-to-end blob reachability; rehearse data-plane access separately.
App rollout and infra apply share a non-cancelling mutation concurrency group.

Run `python scripts\configure-github-oidc.py` to preview the one-time setup.
After explicit operator approval, `--apply` creates three dedicated managed
identities, environment-bound OIDC trust, scoped role definitions/assignments,
main-only GitHub environments, and nonsecret repository variables. No client
secret is created. App permissions are confined to five named Container Apps
plus ACR push; infra planning has read/what-if access, and infra apply has only
the reviewed networking writes and Storage private-endpoint approval.
The setup command deliberately leaves repository Actions disabled.

Required variables are `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_APPS_CLIENT_ID`, `AZURE_INFRA_PLAN_CLIENT_ID`,
`AZURE_INFRA_APPLY_CLIENT_ID`, `AZURE_APPS_RESOURCE_GROUP`, `ACR_NAME`,
`NORTHWIND_DEPLOY_ENABLED` and `NORTHWIND_INFRA_ENABLED`. The identities trust
only the `northwind-apps`, `northwind-infra-plan` and `northwind-infra-apply`
environments for this repository using GitHub's immutable owner/repository-ID
subject prefix; setup verifies that the current OIDC policy matches rather than
changing GitHub's subject policy. Environments allow `main` only. Infra apply
always needs an explicit dispatch/full-SHA confirmation even if GitHub's plan
does not support required-reviewer environment protection. This is not a claim
of enforced two-person approval.

Keep Actions disabled until the new workflow commit and OIDC setup are reviewed.
Then disable the legacy workflows individually, enable only `CI`,
`deploy-apps.yml` and `deploy-infra.yml`, enable repository Actions, and dispatch
`CI` on `main`. Never enable the old paid/greenfield workflows as a shortcut.

Each department is its own **Foundry project** inside a shared AI Services
account. Model deployments, UAMI, VNet, and observability are shared;
agents, connections, evaluators, and red-team runs are scoped per project.
See [`docs/departments.md`](./docs/departments.md) for the full layout.

- **Voice Live** — browser-based customer voice front door + hands-free
  back-office assistant
- **Azure AI Content Understanding** — classifies + extracts data from
  warranty PDFs, damage photos, and structured intake forms
- **Foundry Agent Service** — 5 independently hosted agents in the
  `northwind-orders` project: triage, order-lookup, policy-lookup,
  resolution-draft, and `northwind-orchestrator`.
  Tool calls on the specialists are served by two
  tiny MCP Container Apps (`ca-mcp-tech-lookup`, `ca-mcp-work-order` — legacy
  names from an earlier scaffold; semantically they serve `lookup_order` and
  `propose_resolution`) plus the Search KB's MCP endpoint, so every tool
  invocation surfaces as a first-class MCP tool call in Foundry
  Observability + App Insights. The Policy Lookup tab hits the same hosted
  orchestrator in single-turn mode.
- **Microsoft Agent Framework** — powers the hosted orchestrator agent and
  is what makes the manager loop tool-callable
- **Azure AI Search** — **four indexes** (one per department) plus the shared
  `northwind-knowledge` flagship, all served by one Search service
- **Foundry Observability + Application Insights** — full OpenTelemetry traces
  across every agent / tool / model call, stamped with `department` +
  `foundry_project` so the platform view slices cleanly by project
- **Foundry Evaluators + custom evaluator** — golden-dataset PR gate + 5% continuous
  eval sampling of production traffic, **keyed by department** in
  `evals/baseline.json`

The API, web front-ends, and MCP services run on **Azure Container Apps**;
the five Orders agents run in **Foundry hosted-agent sandboxes**. Cosmos and
Storage have public access disabled in the current deployment. Foundry,
Search, and the demo front doors are publicly reachable with their respective
access controls; do not describe this as an entirely private-network solution.

The in-app trace API uses `azure-monitor-query`, the Application Insights
workspace ID (`APPINSIGHTS_WORKSPACE_ID`, or `LOG_ANALYTICS_WORKSPACE_ID`),
and the app identity's **Log Analytics Reader** role scoped to that workspace.
It queries workspace `App*` tables and correlates child spans by operation ID.
Insurance shows real ingested spans or explicit unavailability, never the
retail compatibility stub. Reported token/cost metrics are not a complete
Azure billing total. Failed, incomplete or empty hosted responses are errors,
not successful turns.

## Existing project and learning lifecycle

The native Foundry demo uses `northwind-orders` in the existing Foundry account.
The existing web application remains on its original images and `northwind`
project; it is not the surface for the new learning capabilities. Creating the
new project does not migrate old response IDs, histories, or the account's
default-project designation.

The account's `defaultProject` update was accepted but not applied in this
environment, including with API `2026-09-01`. The current default remains
`nw-claims-warranty`. `scripts/migrate-orders-project.py` checks effective
readback and fails rather than reporting a successful switch. Do not delete
Claims or provision another account as an automatic workaround.

Use native Foundry **Agents**, **Data**, **Evaluations**, and model fine-tuning
surfaces. No custom lifecycle screens or training/review mutation endpoints
are added to the Northwind web application. The CLI below prepares reviewed,
versioned artifacts for Foundry rather than replacing its UI.

```powershell
python scripts\orders-learning.py --help
python scripts\orders-learning.py status --output pipelines\orders-learning\workspace\status.json
```

The initial 44 synthetic examples are **unreviewed**. Dataset exports require
human decisions and preserve source/review hashes and grouped splits.
Candidate evaluations bind the actual deployment, prompt, release, and run.
Training is dry-run by default and additionally requires dataset approval,
model admission, a bounded budget, and verified default-project placement.
No fine-tuned model or passing evaluation is implied by a configured name.
`TRIAGE_MODEL_DEPLOYMENT` selects a real custom deployment only after it exists
and passes promotion gates; otherwise triage uses the explicitly reported baseline.

For the separately authorised synthetic demo, the user manually uploaded the
200-example training and 50-example validation datasets into the existing
default `nw-claims-warranty` project, and evaluation data into `northwind-orders`.
Native Data dataset registrations are not automatically fine-tuning file IDs.
`scripts/import-orders-native-training.py` verifies and server-imports those
already uploaded Azure blobs; it does not upload local files or change Storage
network access. Demo acceptance is recorded separately from individual human
annotation or the production curation ledger.

The approved experiment uses GPT-4.1-mini (2025-04-14), supervised Developer
training, one epoch, seed 106, and no automatic model deployment. Training,
evaluation, and temporary Developer inference share a $10 submission budget,
not an Azure billing hard cap. Original agents remain active; any candidate is
a separate demo companion until explicitly promoted. Developer custom model
deployments expire after 24 hours and are not suitable as unattended production
dependencies. Native portal submission is the confirmed path in this
environment; the OpenAI v1 creation request was rejected before creating a job.
Do not repeat an uncertain submission: reconcile the journal and native job
inventory first.

Native Human Evaluation rule attachment currently rejects hosted agents in
this environment. A clearly labelled prompt companion, `northwind-triage-review`,
uses the canonical triage instructions and has an enabled native human-review
rule. The five business agents remain hosted (five of six agents overall).
`scripts/setup-orders-foundry-demo.py` configures this companion and a native
precomputed-response F1 evaluation definition. Token F1 is diagnostic only,
not a substitute for the structured triage release gates.
The template does not constitute completed annotations, and an evaluation
definition does not constitute an executed evaluation run.
See [evaluation and learning commands](./evals/README.md) for the review,
export, evaluation, and approval steps.

All five hosted services use `scripts/orders_agent_package.py` to package an
explicit 14-file allowlist before `azd` packaging. SDK deployments can use
`scripts/deploy-orders-hosted.py`. Prefer this scoped command for Foundry-only
work; a full `azd up` also redeploys the separate legacy web application.
The shared development Python environment is not upgraded to the hosted
runtime's dependency set; use an isolated environment for `agents/hosted/requirements.txt`.

## Insurance portal with Entra sign-in

The insurer-branded portal is now the default web experience (set `VITE_INSURANCE_BUSINESS_ENABLED=false` to fall back to the legacy department-tile shell). Configure the Entra IDs produced by `scripts/configure-insurance-identity.py` to enable sign-in. Public pages are branded for Northwind Insurance; after Microsoft Entra sign-in the server-assigned role decides what you see: policyholders get policies, claims and loss reporting; adjusters and presenters additionally get an **AI workspace** (claims concierge, document intelligence, approvals, bake-off, safety). The Agent Harness UI has been removed from the web app for now. The portal passes its Entra access token to the AI workspace's API calls. Backend enforcement of that token on the legacy AI endpoints (`/chat`, `/hitl`, `/voice`, …) is not yet implemented.

## Agent Harness (opt-in)

The **Agent Harness** workspace is accessible from the landing page and each
department's sidebar. It runs the Python **GitHub Copilot SDK 1.0.14** with its
matching runtime inside FastAPI. In insurance mode the SDK chooses among
triage, canonical policy/claim lookup, authoritative policy lookup and
pending-only claim-action drafting. Retail mode retains its separate prompt
and tool compatibility. The harness does **not** replace the hosted Agent
Framework manager. Azure Foundry supplies inference through a refreshing
managed-identity bearer-token callback. No GitHub subscription or new model
API key is needed for this provider mode.

Enable it explicitly before provisioning/updating the deployment:

```powershell
azd env set COPILOT_HARNESS_ENABLED true
azd deploy api
```

The default is disabled. The workspace remains visible and reports its actual
configuration; it never substitutes simulated output. API packaging stages the
canonical `agents\copilot-harness\instructions.prompty` and pre-downloads the
pinned SDK runtime during the API image build. Roll back by setting the flag
to `false` and applying the infrastructure configuration.

For local development, use a **clean API virtual environment** rather than an
environment containing the Agent Framework umbrella package (which can pin
an incompatible Copilot SDK). Install `apps\api\requirements.txt`, run
`python -m copilot download-runtime`, and configure
`FOUNDRY_PROJECT_ENDPOINT`, `COSMOS_ENDPOINT`, and `MCP_WORK_ORDER_URL`.
Set `COPILOT_HARNESS_ENABLED=true` in the API process. Local Azure Identity
credentials and connectivity to private Azure resources are required;
Compose does not inherit the host's Azure CLI login.

Runs are self-contained: Stop/refresh cancels the live request, while actual
results and events remain in Cosmos for seven days. The existing approval
queue has its own unchanged retention. A draft is **pending human approval**,
not an executed claim decision or payment. A cancelled request can already have saved a draft;
inspect its approval status before starting another run. Runtime access is
limited to four application tools, with no shell/filesystem tools or personal
Copilot configuration. This remains an unauthenticated synthetic demo, not a
production tenant-isolation solution.

See [architecture](./docs/architecture.md#copilot-sdk-agent-harness) and
[evaluation requirements](./evals/README.md#copilot-sdk-harness).

## Regions

The demo splits into two regions by default:

| Region              | What lives there                                                                             |
| ------------------- | -------------------------------------------------------------------------------------------- |
| **Canada Central**  | Compute + data plane: VNet, Container Apps, FastAPI + web front-ends, Cosmos, Storage, Key Vault, Azure AI Search, App Insights, UAMI |
| **East US 2**       | AI plane: Foundry AI Services (Voice Live, Agent Service, `gpt-5.4`, `gpt-5.4-mini`) |

Cross-region traffic to the AI plane stays on the Microsoft backbone via
**cross-region Private Endpoints** — the PE is provisioned in the Canada Central
VNet even though the target Foundry / Search resource lives in East US 2.
Public network access is disabled on every data-plane resource.

Override with `AZURE_LOCATION` and `AZURE_AI_LOCATION` before `azd up` if you
want different regions.

## Quickstart

```bash
azd auth login
azd up
```

For a fresh retail environment, `azd up` provisions infrastructure with Bicep, deploys the five
container services, generates ~20 synthetic Northwind policy + product
documents, indexes them into AI Search, and prints the URLs for the two
web front-ends.

### Existing deployment: split-group reuse (opt-in)

The original **fresh/greenfield** provisioning path remains the default
(`NORTHWIND_REUSE_EXISTING_RESOURCES=false`, or unset). Do not use that path
against the migrated environment: it reconciles the original single-group
infrastructure and runs the data/agent bootstrap.

**2026-10-14: replacement-stack cutover and legacy retirement are complete.**
In subscription `137c905b-7734-479e-acc5-ab8222462936`, the live placement is:

| Resource group | Final placement |
| --- | --- |
| `rg-northwind-apps` | `ca-api`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`, `ca-web-tech`, `ca-web-voice`, all attached to `cae-zurlo-northwind`; retain the selected app UAMI, ACR, App Insights and Log Analytics |
| `rg-northwind-networking` | Replacement `vnet-zurlo-northwind`, NSGs, approved Cosmos private endpoint `pe-zz-cosmos-northwind` and `privatelink.documents.azure.com` VNet link `northwind-networking` |
| `rg-northwind-data` | Existing Storage, Search, Cosmos (East US 2), Storage Event Grid topic |
| `rg-northwind-ai` | `aisvc-zurlo-northwind` and all five retained projects, `ai-gateway-northwind` and its connector-gateway companion |

The final managed environment ID is
`/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-apps/providers/Microsoft.App/managedEnvironments/cae-zurlo-northwind`.
Container App environment attachment is immutable: migration requires
recreating the five apps against the replacement environment and moving the
recreated resources into Apps while restoring the stable names. Selecting
groups or moving an existing app alone does not change its environment.
The original apps, Dev environment/VNet, monitoring component/web test,
unused identity and its grants, `pe-cosmos-zurlo-northwind`, and
`cosmos-ng5k6nn2f2ddy` DNS link were retired. Both `rg-northwind-dev` and
`rg-northwind-rebuild` are deleted. Azure also removed the service-managed
groups owned by the old environment and monitoring component.
Replacement Cosmos DNS ownership was reconciled after retiring the old
endpoint; public Cosmos access remains disabled.
`ca-cu-ingest` was explicitly retired and is not an azd deployment service.
Content Understanding schemas and the API remain; a worker is not required.
Azure-managed environment and OpenTelemetry resources remain service-owned.

The final reuse profile is below. The operator must keep selectors aligned
with actual cloud placement during cutover; configuration is not migration
evidence. Routine provisioning/deployment must not recreate Dev, Rebuild or
the retired ingestion worker.

| Key | Value |
| --- | --- |
| `NORTHWIND_REUSE_EXISTING_RESOURCES` | `true` (literal lowercase string) |
| `AZURE_SUBSCRIPTION_ID` | `137c905b-7734-479e-acc5-ab8222462936` |
| `AZURE_RESOURCE_GROUP` | `rg-northwind-ai` (azd agent target) |
| `AZURE_APPS_RESOURCE_GROUP` | `rg-northwind-apps` |
| `AZURE_DATA_RESOURCE_GROUP` | `rg-northwind-data` |
| `AZURE_NETWORKING_RESOURCE_GROUP` | `rg-northwind-networking` |
| `AZURE_AI_RESOURCE_GROUP` | `rg-northwind-ai` |
| `AZURE_FOUNDRY_RESOURCE_GROUP` | `rg-northwind-ai` |
| `AZURE_OBSERVABILITY_RESOURCE_GROUP` | `rg-northwind-apps` |
| `AZURE_LOG_ANALYTICS_RESOURCE_GROUP` | `rg-northwind-apps` for this migrated environment |
| `AZURE_LEGACY_RESOURCE_GROUP` | Omit or leave empty; deprecated compatibility label, never a resource selector |
| `AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP` | `rg-northwind-apps` (defaults to `AZURE_APPS_RESOURCE_GROUP` when omitted) |
| `AZURE_VNET_RESOURCE_GROUP` | `rg-northwind-networking` (defaults to `AZURE_NETWORKING_RESOURCE_GROUP` when omitted) |
| `AZURE_COSMOS_PRIVATE_ENDPOINT_NAME` | `pe-zz-cosmos-northwind` (default) |
| `ORDER_PROJECT_NAME` | `northwind-orders` |
| `ORDER_AGENTS_HOSTED` | Preserve the current value (`true` for the hosted demo) |
| `NORTHWIND_BUSINESS_DOMAIN` | `insurance`; `retail` remains the compatibility default |
| `NORTHWIND_INSURANCE_AGENTS_READY` | Preserve explicit verified readiness; `false` until native insurance rollout is verified |
| `FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID` | Explicit deployed `northwind-insurance-*` manager ID/version; required when readiness is `true` |
| `NORTHWIND_ENABLE_CU_INGEST` | `false`; reuse never recreates the retired worker |

For an older transitional deployment only, explicitly select
`AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP=rg-northwind-dev`,
`AZURE_VNET_RESOURCE_GROUP=rg-northwind-dev` and
`AZURE_COSMOS_PRIVATE_ENDPOINT_NAME=pe-cosmos-zurlo-northwind` while those
dependencies still exist. A legacy label never activates these overrides.
`NORTHWIND_EXISTING_RESOURCE_IDS` reports actual app attachments, environment
subnet and private-endpoint subnet from live references, not inferred state
from source labels. `AZURE_AI_PROJECT_ID` must identify `northwind-orders`
under the existing account in AI and match the selected subscription.

#### Replacement endpoint change

Stable app names do **not** preserve default ingress FQDNs across environment
recreation. The replacement run URLs are:

| Output | Replacement URL |
| --- | --- |
| `API_URI` | `https://ca-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io` |
| `MCP_TECH_LOOKUP_URL` | `https://ca-mcp-tech-lookup.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/` |
| `MCP_WORK_ORDER_URL` | `https://ca-mcp-work-order.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/` |
| `WEB_TECH_URI` | `https://ca-web-tech.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io` |
| `WEB_VOICE_URI` | `https://ca-web-voice.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io` |

The old `bluecliff` FQDNs are retired; no custom domain masks this change.
Both frontend builds, API/MCP callers and all five active insurance-agent
configurations use the replacement URLs. Adoption outputs read actual
named app ingress instead of hardcoding these URLs. When
`NORTHWIND_GATEWAY_URL` (and `NORTHWIND_GATEWAY_STACK`) are set, `API_URI` and
`MCP_*_URL` become the API Management gateway URLs instead, so a re-provision
cannot silently revert a gateway cut-over; see
[API Management](./docs/api-management.md).

`infra/main.bicepparam` uses compiler serialization for environment values,
including JSON-valued endpoint strings. This avoids raw JSON interpolation
errors in older azd versions without changing runtime values. The final
reference-only adoption deployment has refreshed its live resource IDs.

The insurance release uses `northwind-insurance-knowledge`,
`northwind-insurance-knowledge-ks`, `northwind-insurance-kb` and the dedicated
`northwind-insurance-kb-mcp` connection. Configured names do not prove
publishing or runtime verification has completed.

Preserve the current azd outputs used as adoption inputs:
`FOUNDRY_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_PROJECT_ENDPOINT`,
`FOUNDRY_PROJECT_ENDPOINTS`, `ORDERS_OPENAI_ENDPOINT`, `AZURE_OPENAI_ENDPOINT`,
`ORDERS_KB_CONNECTION_NAME`, `FOUNDRY_KB_CONNECTION_NAME`,
`FOUNDRY_PLANNER_DEPLOYMENT`, `FOUNDRY_WORKER_DEPLOYMENT`,
`FOUNDRY_PROMPT_AGENT_DEPLOYMENT`, `FOUNDRY_EMBEDDING_DEPLOYMENT`,
`AZURE_AI_MODEL_DEPLOYMENT_NAME`, `FOUNDRY_BAKEOFF_DEPLOYMENTS`,
`AZURE_SEARCH_INDEX`, `AZURE_SEARCH_KNOWLEDGE_BASE`,
`AZURE_SEARCH_KNOWLEDGE_SOURCE`, `COSMOS_DATABASE`,
`STORAGE_CU_CONTAINER`, and `STORAGE_SEED_CONTAINER`.
The preprovision validator rejects missing/blank values and inconsistent
Orders endpoints rather than silently replacing them with defaults.
Existing optional agent-ID outputs are preserved, including empty placeholders.

In reuse mode, `azd provision` deploys only a reference/adoption template.
It reads the replacement UAMI client ID and live app ingress, retains existing
Foundry/runtime outputs, and reports the effective account default project
(`nw-claims-warranty` currently), **without changing it**. It does not
reconcile existing infrastructure configuration: no foundational services,
RBAC, model deployments, connections, app settings/images, privacy/network
settings, observability links, or seeded data are created or updated.
The Windows and POSIX postprovision hooks exit before installing dependencies,
opening public access, fetching keys, or bootstrapping data/agents. They are
not a migration or repair mechanism.

All five `containerapp` services have explicit `resourceGroup` and
`resourceName` routing to Apps. The `azure.ai.agent` services are unchanged:
they remain retail and reject insurance-mode azd agent deployment. Use
`scripts/deploy-orders-hosted.py --business-domain insurance` with an explicit
credential source, per-role non-secret config and allowlisted source package
for the separate insurance fleet.
Its project remains `northwind-orders` in AI. Fresh provisioning emits `AZURE_APPS_RESOURCE_GROUP`
as the original single group, preserving greenfield deployment routing.
Provisioning adoption alone leaves existing app images/settings and verified
insurance readiness unchanged; a subsequent scoped `azd deploy` deliberately
updates the selected service images. Retain the selected Apps identity and
its registry/data access bindings; any required changes are a separate,
reviewed operation.

## Repository layout

```
infra/                Bicep IaC (main + modules)
  live/               Scoped incremental boundaries applied by the guarded
                      workflows (storage networking, API Management)
apps/
  api/                FastAPI + Agent Framework orchestrator
  web-tech/           React (Vite) back-office / CX-agent app
  web-voice/          React (Vite) customer voice front door
  mcp-tech-lookup/    Tiny MCP server — serves lookup_order for the
                      Order Lookup agent. Directory + service name are
                      legacy from the Field Services scaffold.
  mcp-work-order/     Tiny MCP server — serves propose_resolution for the
                      Resolution Draft agent. Directory + service name are
                      legacy from the Field Services scaffold.
agents/               Order Concierge Foundry agent definitions (flagship)
data/
  seed/                        Order Concierge synthetic corpus generator
  golden/                      Order Concierge eval golden dataset
  northwind/                   Company manifest + non-flagship departments
    company.yaml               Top-level manifest (company + 4 departments)
    departments/
      order-concierge/         Points back at data/seed + data/golden + agents/
      employee-help-desk/      HR/IT corpus + hr-assistant prompt agent
      claims-warranty/         Claims corpus + CU analyzer + triage agent
      contact-center/          Call scripts + intent classifier + Voice Live prompts
pipelines/
  content-understanding/  CU analyzer schemas + ingestion job
evals/                Foundry evaluators config + custom evaluator
scripts/              azd hooks (postprovision indexing, per-project agents, etc.)
.github/workflows/    CI, PR evals, continuous evals
docs/                 Architecture, demo script
```

## Documentation

- [Architecture](./docs/architecture.md)
- [**API Management gateway (`apim-northwind`)**](./docs/api-management.md)
- [**One company, four departments (the reshape)**](./docs/departments.md)
- [Demo script (10 minutes)](./docs/demo-script.md)
- [**Why Foundry — vs OpenAI Assistants, Bedrock, Vertex, LangChain**](./docs/differentiators.md)
- [**GenAIOps through GitHub**](./docs/genaiops.md)
- [Evaluation strategy](./evals/README.md)

## What makes this demo hard to beat

The bake-off (`Model bake-off` tab), safety shields (`Try to jailbreak` tab),
red-team + optimizer pipelines, and 4-tier eval gate are all Foundry-only
capabilities that most competing stacks either don't offer or ask you to
build yourself. See [`docs/differentiators.md`](./docs/differentiators.md)
for the full checklist and [`docs/genaiops.md`](./docs/genaiops.md) for the
end-to-end lifecycle diagram.

## Non-goals

This demo intentionally omits Front Door, WAF, Azure Policy, PSTN/ACS phone
integration, fine-tuning, and multi-tenant isolation. The goal is a focused,
forkable pattern any Solution Engineer can adapt to a real customer — not a
turnkey product.

The two MCP servers (`ca-mcp-tech-lookup`, `ca-mcp-work-order`) run behind
**public Container Apps ingress with no auth by default** — same trade-off, kept
simple so Foundry can reach them without wiring bearer tokens or private
endpoints. They (and the API) can be put behind the opt-in API Management
gateway: MCP callers then present a subscription key (for Foundry agents,
through a project connection) and `NORTHWIND_GATEWAY_AUTH_REQUIRED=true` makes
the apps accept only calls relayed by the gateway. See
[API Management](./docs/api-management.md).
