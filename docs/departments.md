# Northwind Insurance — one fictional insurer, four departments, one Foundry account

The Northwind demo is organized around **one fictional mid-market property-and-casualty
insurer** (Northwind Insurance, Washington and Colorado auto) with **four departments**.
Each department is backed by its own **Foundry project** inside a shared AI Services
account, so an SE can walk a buyer to the department that matches their interest: same
VNet, same managed identity and same model quota, but each department has its own agents,
connections, evaluation rows and Foundry Observability lens. An authorised operator
verifies each target project and deployment scope before rollout. All records are
synthetic, and no agent approves, denies or pays a claim.

## The four departments

| Department (slug) | Foundry pillar | Foundry project | Search index | Portal tab (`enabled_tabs`) | Agents |
| ----------------- | -------------- | --------------- | ------------ | --------------------------- | ------ |
| **Claims Concierge** (`claims-concierge`, flagship) | Multi-agent + human approval | `northwind-orders` | `northwind-insurance-knowledge` | Claims / Policies (`insurance_records`), Claims Concierge chat (`agent_chat`), Reviewed drafts (`reviewed_drafts`), Approvals (`hitl`) | Five **hosted** agents: `northwind-insurance-{orchestrator,triage,policy-claim-lookup,policy-wording,claim-action-draft}` |
| **Adjuster Support** (`adjuster-support`) | Grounded retrieval (RAG) | `nw-employee-help-desk` | `nw-insurance-adjuster-support` | Adjuster support (`adjuster_support`) | Prompt `northwind-insurance-adjuster-support`, read by the API (extractive, no tools) |
| **Claims Document Intelligence** (`claims-documents`) | Content Understanding | `nw-claims-warranty` | `nw-claims` | Document Intelligence (`claims_inbox`) | **Prompt** agent `northwind-claims-triage`; analyzer `northwindInsuranceClaimsV1` |
| **Policyholder Contact Center** (`contact-center`) | Voice Live | `nw-contact-center` | `nw-contact-center` | Policyholder Contact Center (`voice_console`) | **Prompt** agent `northwind-intent-classifier`; Voice Live app (`apps/web-voice`) |

The Foundry project names are **bound to the live deployment** and keep their original
spelling. They are configuration values (`foundry_project` in
`data/northwind/company.yaml`), not product names. The code accepts `northwind-orders` and
`nw-insurance-claims` for the flagship project (`INSURANCE_PROJECTS` in
`apps/api/app/insurance.py`); which project names exist in a given tenant is an operator
fact this repository cannot verify, so check before you rely on one.

Slugs from earlier builds (and `X-Department` values or `FOUNDRY_PROJECT_ENDPOINTS` keys
that still use them) are mapped to the canonical slugs above by `apps/api/app/company.py`
and `apps/web-portal/src/departments.ts`; the old-to-new table is in
[`data/northwind/README.md`](../data/northwind/README.md#department-slugs).

### What each department shows

- **Claims Concierge.** Synthetic records (policyholders `PH-1001` and `PH-1002`,
  policies `POL-1001` in WA and `POL-1002` in CO, claims `CLM-2001` and `CLM-2002`, versioned
  wording `NW-AUTO-WA-2026-v1` and `NW-AUTO-CO-2026-v1`) in Claims / Policies; a chat whose
  manager hands off to four hosted specialists with a live trace rail; saved claim-action
  drafts that wait in Approvals for a human, then move to Reviewed drafts. Chat needs the
  insurance runtime to be ready ([`docs/architecture.md`](./architecture.md#runtime-readiness-and-feature-switches)).
  The department also hosts the **Copilot SDK harness** (`platform_tabs: agent_harness` in
  the manifest); today it is API-only (`/harness/*`) and the portal renders no tab for it.
- **Adjuster Support.** Read-only, extractive answers about adjuster procedures with the
  excerpt, title, version and source URI shown, and a model picker (`gpt-5.4`,
  `gpt-5.4-mini`, `Kimi-K2.6` when those deployments exist). Questions that ask for a
  coverage, approval or payment decision get the fixed "I don't have sufficient evidence"
  answer.
- **Claims Document Intelligence.** Live Content Understanding on four allowlisted
  synthetic PDFs (FNOL, repair estimate and invoice, and the WA and CO claim snapshots) or
  an upload of at most 10 MiB, side by side with the original and the extracted fields,
  confidence and source spans. A reviewer can hand the extracted identifiers to Claims
  Concierge. Nothing is indexed or written back.
- **Policyholder Contact Center.** The portal tab is text intake plus a confirmed handoff;
  live audio is the separate Voice Live app (`ca-insurance-voice`). Either way a person
  reviews the intake and confirms before `POST /handoff` starts a Claims Concierge run.

## Source of truth

Everything about a department lives under `data/northwind/departments/<slug>/`, and the
top-level manifest `data/northwind/company.yaml` lists the departments, their projects, their
indexes and their enabled tabs. **The runtime reads only `company.yaml`**
(`data/northwind/__init__.py` exposes `load_company()`, `load_department(slug)` and
`list_departments()`); each `department.yaml` is the department's authoring slice and is kept
in step by hand. The manifest contract is in [`data/northwind/README.md`](../data/northwind/README.md).

| Department | Seed corpus | Golden rows | Prompts |
| ---------- | ----------- | ----------- | ------- |
| `claims-concierge` | `data/seed/` (`insurance-corpus.json`; the PDFs are generated locally by `data/seed/generate.py`) and `data/northwind/insurance-fixtures.json` | `data/golden/dataset.jsonl` (50 rows) and `data/golden/harness.jsonl` (17) | `agents/insurance/*.yaml` (hosted agents) |
| `adjuster-support` | `departments/adjuster-support/seed/` (four versioned procedures) | `departments/adjuster-support/golden/dataset.jsonl` (14) | `prompts/northwind-adjuster-support.yaml` |
| `claims-documents` | `departments/claims-documents/seed/`, the sample allowlist `claims-documents.json` and the analyzer `cu-analyzers/claim-intake.json` | `golden/dataset.jsonl` (10) | `prompts/northwind-claims-triage.yaml` |
| `contact-center` | `departments/contact-center/seed/` | `golden/dataset.jsonl` (12) | `prompts/northwind-intent-classifier.yaml`, `greeting.md`, `voice-live-system.md` |

Paths under `departments/` are relative to `data/northwind/`. The automated Tier-2
golden run uses the flagship sets only (see [`docs/genaiops.md`](./genaiops.md)); the other
departments' rows are authored regression rows that no workflow runs yet, and
`data/northwind/_smoke_test.py` checks that the manifest and those files parse.

`company.yaml` is read by:

- the **API** (`apps/api/app/company.py`): `GET /company`, `GET /departments`,
  `GET /departments/{slug}` and the `X-Department` header, which selects the project
  endpoint (an unknown or missing slug falls back to Claims Concierge);
- the **portal** (`apps/web-portal/src/CompanyContext.tsx`): the landing tiles and the
  sidebar;
- **`scripts/create-prompt-agents.py`** (`--dry-run` first): creates each department's
  prompt agents from `prompts_dir/*.yaml` in that department's own project. It skips
  `claims-concierge`, whose agents are the hosted fleet from
  `scripts/deploy-hosted-agents.py`;
- **`scripts/create-department-indexes.py`**, `scripts/create-knowledge-base.py` and
  `scripts/connect-knowledge-base.py`: build the Search index named by each manifest's
  `search_index`, plus the knowledge base
  and project connection for the departments that use them (`claims-documents` and
  `contact-center`).

### Search, knowledge bases and what is actually queried

| Index | Published by | Used by |
| ----- | ------------ | ------- |
| `northwind-insurance-knowledge` (knowledge source `northwind-insurance-knowledge-ks`, knowledge base `northwind-insurance-kb`, project connection `northwind-insurance-kb-mcp`) | `scripts/publish-insurance-knowledge.py` (dry run unless `--apply`) | the hosted `policy-wording` agent |
| `nw-insurance-adjuster-support` | `scripts/publish-adjuster-support.py` (dry run unless `--apply`; it never deletes) | `/adjuster-support/*` |
| `nw-claims`, `nw-contact-center` | `scripts/create-department-indexes.py` (drops and recreates its own index), then `create-knowledge-base.py` and `connect-knowledge-base.py` | no current API route queries them; the prompt agents in those departments have no tools |

The existing Search service may be reused. Manifest names describe intended targets, not
proof that cloud resources exist or contain current content, and an authorised operator
reviews each dry run before publishing. `northwind-claims-triage` and
`northwind-intent-classifier` are provisioned prompt agents that the current API and portal
do not invoke: the portal's Document Intelligence tab hands reviewed identifiers to Claims
Concierge, and the contact-center operator chooses the intake intent.

## Where each Foundry project lives in Bicep

`infra/modules/foundry.bicep` provisions the shared AI Services account and the flagship
project (`insuranceProjectName`, default `northwind-orders`). The other three come from
the `additionalProjects` parameter, defaulted in `infra/main.bicep`:

```bicep
param additionalProjects array = [
  { slug: 'adjuster-support', projectName: 'nw-employee-help-desk', displayName: 'Northwind Adjuster Support', ... }
  { slug: 'claims-documents', projectName: 'nw-claims-warranty',    displayName: 'Northwind Claims Documents', ... }
  { slug: 'contact-center',   projectName: 'nw-contact-center',     displayName: 'Northwind Policyholder Contact Center', ... }
]
```

The account-scoped roles (Azure AI User, Cognitive Services OpenAI User) are granted to the
application identity and the principal running `azd up` once, on the account, and inherit to
every project. The four endpoints are exported as the JSON app setting
`FOUNDRY_PROJECT_ENDPOINTS` (hand-formatted in `infra/main.bicep` so it is real JSON, in the
same order as `company.yaml`), which the API reads at startup.

## Adding a new department

1. Add `data/northwind/departments/<slug>/` with a `department.yaml`, a `seed/` corpus, at
   least one `prompts/*.yaml` and a `golden/dataset.jsonl` with at least 10 rows.
2. Add the department to `data/northwind/company.yaml` (`slug`, `name`, `tagline`,
   `pillar`, `icon`, `brand_color`, `foundry_project`, `search_index`, `cu_inbox_prefix`,
   `enabled_tabs`, `seed_dir`, `golden_path`, `prompts_dir`, `cu_analyzer_dir`,
   `customer_archetype`). This is the file the API and the portal read.
3. Add a matching entry to the `additionalProjects` array in `infra/main.bicep`, and extend
   the hand-formatted `FOUNDRY_PROJECT_ENDPOINTS` map in the same file (the `containerapps`
   module input and the output of that name each index `additionalProjectEndpoints` by
   position, in `company.yaml` order). Rebuild the committed `infra/main.json` with
   `az bicep build --file infra/main.bicep --outfile infra/main.json` (the form `ci.yml`
   uses).
4. Add the UI tab: create a component under `apps/web-portal/src/tabs/` (for example
   `<Name>Tab.tsx`), then in `apps/web-portal/src/App.tsx` add its id to the `TabId` union,
   map the manifest's `enabled_tabs` slug in `DEPT_TABS` (id and label), and mount it in
   `DepartmentShell` (`{department.slug === "<slug>" && tab === "<id>" && <NameTab />}`).
   Claims Concierge is special-cased through `CLAIMS_TABS` instead of `enabled_tabs`.
5. If the tab needs API routes, add a router under `apps/api/app/` and regenerate the route
   table with `python scripts/export-openapi.py` (the CI contract test fails if it drifts).
6. Provision and publish. There is no postprovision hook, so after `azd provision` and
   `azd deploy` run the data scripts yourself: `python scripts/create-department-indexes.py`
   (or the dedicated publish script), `python scripts/create-knowledge-base.py`,
   `python scripts/connect-knowledge-base.py` and `python scripts/create-prompt-agents.py`,
   each of which iterates over the manifest.
7. Add golden rows and, only with reviewer-approved measured numbers, a baseline slot in
   `evals/baseline.json`.

## Cross-department flow

The most compelling demo is the **cross-department finale**, with the same shared account,
identity and evaluation loop behind four different customer archetypes:

1. **Policyholder Contact Center**: a caller reports a loss in the Voice Live app (or an
   operator keys the intake in the portal tab); a person confirms the editable summary and
   `POST /handoff` starts a Claims Concierge run.
2. **Claims Concierge**: the manager triages, verifies the claim and policy records,
   retrieves the versioned wording and drafts a claim action.
3. **Approvals**: a human approves or rejects the draft; no external action or claim
   decision follows.
4. **Claims Document Intelligence**: extract an FNOL or estimate with provenance and
   uncertainty, review it and hand the identifiers to Claims Concierge.
5. **Adjuster Support**: look up the review procedure, with its version and source.

The walkthrough with click paths and prompts is [`docs/demo-script.md`](./demo-script.md).
