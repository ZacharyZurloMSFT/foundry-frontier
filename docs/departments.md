# Northwind Insurance — four departments as content packs

Northwind Insurance is **one fictional property-and-casualty insurer** with **four
departments**. Each department is a **content pack** — a seed corpus, prompt agents, a
golden set and a Search index — that bootstrap scripts load into its own **Foundry
project** inside one shared AI Services account. Model deployments and account-scoped
RBAC are shared; each project keeps its own agents and connections. An authorised
operator verifies each target project and deployment scope before running any of the
scripts below.

## What a department is now

Departments are **not a portal concept**. The Northwind Insurance portal
(`apps/web-tech`, entered through `BusinessPortal`) has no department picker and no
per-department tabs, and the API's own routes are `/health`, `/build` and `/api/v1/*`: there
is no `/company` or `/departments` endpoint and no `X-Department` header (see
[Removed legacy surfaces](../README.md#removed-legacy-surfaces)). The API makes its model
calls through the single project in `FOUNDRY_PROJECT_ENDPOINT` and never calls a
department's prompt agents or its own Search index. Three things still tie departments to
the running system:

- **One content set reaches users.** The customer Help Center answers from the
  `employee-help-desk` pack's `seed/customer/*.md` articles
  ([Help Center customer articles](#help-center-customer-articles)).
- **Telemetry.** Each assistant profile names a department (`concierge` →
  `order-concierge`, `help_center` → `employee-help-desk`). `apps/api/app/company.py`
  loads the bundled manifest, and the assistant runtime calls `resolve_department()` to
  look up that slug's `foundry_project` and stamp `department` and `foundry_project` on
  the spans it creates for the turn.
- **Shared source files.** The `claims-warranty` pack supplies the Content Understanding
  analyzer definition and the allowlist of synthetic claim PDFs behind the portal's
  claim-document review, and selected seed files from three packs are the source of the
  isolated insurance knowledge base ([Source of truth](#source-of-truth)).

Everything else — the department projects, their `search_index` indexes, knowledge bases and
prompt agents — is platform content that the scripts create and the portal does not call.

## The four departments

| Department (slug) | Foundry pillar | Foundry project | Search index | Prompt agents created from `prompts/*.yaml` |
| --- | --- | --- | --- | --- |
| **Claims Concierge** (`order-concierge`, flagship) | `multi_agent` | `northwind-orders` | `northwind-insurance-knowledge` | None from this tree: its agents are the hosted agents under `agents/` |
| **Employee & Adjuster Support** (`employee-help-desk`) | `rag` | `nw-employee-help-desk` | `nw-hr` | `northwind-hr-assistant` and three model variants (`-gpt-5-4`, `-gpt-5-4-mini`, `-kimi`) |
| **Claims Document Intelligence** (`claims-warranty`) | `content_understanding` | `nw-claims-warranty` | `nw-claims` | `northwind-claims-triage` |
| **Policyholder Contact Center** (`contact-center`) | `voice_live` | `nw-contact-center` | `nw-contact-center` | `northwind-intent-classifier` |

Names, slugs, projects and indexes come from `data/northwind/company.yaml`; the slugs key
the scripts and the telemetry stamp. The flagship's project is the native Orders project
`northwind-orders`; the manifest does not reference the separate legacy `northwind`
project.

- **`order-concierge`** has no content folder of its own: its manifest entry points at the
  top-level `data/seed`, `data/golden/dataset.jsonl`, `agents/` and
  `pipelines/content-understanding`. The department indexer skips it,
  `scripts/publish-insurance-knowledge.py` publishes its index
  ([Bootstrapping the packs](#bootstrapping-the-packs)), and `create-prompt-agents.py`
  skips it while `ORDER_AGENTS_HOSTED` is `true` (the default).
- **`employee-help-desk`** keeps its name, but its adjuster-procedures surface (an
  endpoint, a prompt agent and a publisher script) was removed. Today it is the HR/IT
  corpus plus the customer help articles; its adjuster content survives only as the
  `## Adjuster procedures and claims-review boundaries` section of
  `seed/manager-approval-workflow.md`, which feeds the insurance knowledge base. The three
  model-variant agents fed a model dropdown in the removed UI (per the docstring in
  `create-prompt-agents.py`); the script still creates them and nothing calls them.
- **`claims-warranty`**'s Content Understanding assets are live:
  `cu-analyzers/claim-intake.json` defines analyzer `northwindInsuranceClaimsV1`
  (registered by `scripts/configure-insurance-cu.py`), and `claims-documents.json` lists
  the synthetic PDFs the API's claim-document review offers.
- **`contact-center`** was written for the removed Voice Live front door and its handoff
  to the Claims Concierge. Only `northwind-intent-classifier` is created from it;
  `prompts/greeting.md`, `prompts/voice-live-system.md` and the `agents.voice_live` block of
  its `department.yaml` have no consumer, while two of its seed files (`call-scripts.md`,
  `deflection-faq.md`) still feed the insurance knowledge base.

## Source of truth

Everything about a department lives under `data/northwind/departments/<slug>/`:

- `department.yaml` — a descriptive copy of the department's manifest entry plus authoring
  notes; no code reads it
- `seed/*.md` — synthetic corpus; the top-level files are indexed into the department's
  Search index (`README.md` is skipped)
- `prompts/*.yaml` — Foundry prompt-agent definitions
- `golden/dataset.jsonl` — golden rows
- `cu-analyzers/*.json` and `claims-documents.json` — (`claims-warranty` only) the Content
  Understanding analyzer definition and the allowlist of synthetic claim PDFs
- `seed/customer/*.md` — (`employee-help-desk` only) customer help articles, kept out of
  the department index and published to their own

`data/northwind/company.yaml` is the manifest that code loads
(`data.northwind.load_company()`); each `department.yaml` repeats its entry, so keep the two
in step by hand. [`data/northwind/README.md`](../data/northwind/README.md) has the full tree
and the folder contract.

Who reads the packs:

| Reader | What it takes |
| --- | --- |
| `scripts/create-department-indexes.py`, `create-knowledge-base.py`, `connect-knowledge-base.py`, `create-prompt-agents.py` | The manifest, plus top-level `seed/*.md` (indexer) and `prompts/*.yaml` (prompt agents); see [Bootstrapping the packs](#bootstrapping-the-packs) |
| `apps/api/app/company.py` | `company.yaml`, bundled into the API image as `app/_northwind/` |
| `scripts/publish-customer-help.py`, `evals/assistant_eval.py` | `employee-help-desk/seed/customer/*.md` |
| `scripts/publish-insurance-knowledge.py`, `data/seed/generate.py` | The seed files listed in `data/seed/insurance-corpus.json`: the four `claims-warranty` sources, the adjuster-procedures section of `employee-help-desk/seed/manager-approval-workflow.md`, and `contact-center/seed/call-scripts.md` and `deflection-faq.md` |
| `scripts/configure-insurance-cu.py`, `apps/api/app/content_understanding.py` | `claims-warranty/cu-analyzers/claim-intake.json` (analyzer definition); `claims-warranty/claims-documents.json` (PDF allowlist) |
| `data/northwind/_smoke_test.py` | Everything: the manifest loads, seed directories exist, golden files are valid JSONL and prompt YAML parses |

## Bootstrapping the packs

Four scripts turn the packs into platform content. Each iterates the departments in
`company.yaml`. Run them only against a project and deployment scope an authorised operator
has verified.

1. **`scripts/create-department-indexes.py`** uploads each department's top-level
   `seed/*.md` (not `README.md`) to blob container `dept-seed/<slug>/`, then upserts the
   Search index named by `search_index`, datasource `nw-<slug>-ds`, skillset
   `<search_index>-skillset` and indexer `nw-<slug>-indexer`, and resets and runs the
   indexer. It skips `order-concierge`, and it first deletes any existing
   `<search_index>-kb`, `<search_index>-ks` and index so the index can be recreated.
   Customer help articles sit in `seed/customer/` so they never reach `nw-hr`;
   `scripts/tests/test_create_department_indexes_isolation.py` pins that.
2. **`scripts/create-knowledge-base.py`** upserts a knowledge source `<search_index>-ks` and
   a knowledge base `<search_index>-kb` per department (the flagship's names come from
   `AZURE_SEARCH_KNOWLEDGE_SOURCE` and `AZURE_SEARCH_KNOWLEDGE_BASE`). Only the four slugs
   in the script's `_DEPT_INSTRUCTIONS` map get one; any other slug is skipped.
3. **`scripts/connect-knowledge-base.py`** creates a project connection
   `<search_index>-kb-mcp` in each department's project, pointing at that knowledge base's
   MCP endpoint (the flagship's connection name comes from `FOUNDRY_KB_CONNECTION_NAME`).
4. **`scripts/create-prompt-agents.py`** creates (or adds a version of) every
   `prompts/*.yaml` agent in the department's project. A YAML needs `name` and
   `instructions`; the model is `FOUNDRY_PROMPT_AGENT_DEPLOYMENT` unless the YAML sets
   `model:`, and a `tools: [{type: knowledge, index: ...}]` entry becomes an MCP tool over
   that index's knowledge base. It skips `order-concierge` while `ORDER_AGENTS_HOSTED` is
   `true`, and it exits non-zero when a department has no project endpoint.

**When they run.** The azd `postprovision` hook (`scripts/postprovision.ps1` and `.sh`) runs
`data/seed/generate.py`, `data/seed/upload_blobs.py` and `scripts/create-vector-indexer.py`,
then the four scripts above in that order, but only in the retail domain
(`NORTHWIND_BUSINESS_DOMAIN` unset or `retail`, the Bicep default). With
`NORTHWIND_BUSINESS_DOMAIN=insurance` the hook prints that it is skipping all legacy
bootstrap and exits, and with `NORTHWIND_REUSE_EXISTING_RESOURCES=true` it exits before any
bootstrap. In those cases run the scripts yourself with the azd outputs and keys the hook
would load (`scripts/load-azd-env.ps1`, `scripts/fetch-keys.*`);
`scripts/index-departments.ps1` does that for steps 1 and 2.

**The flagship's insurance knowledge is published separately.**
`scripts/publish-insurance-knowledge.py` is a dry run unless `--apply` is passed. It pushes
the policy wordings in `data/northwind/insurance-fixtures.json` and the department seed files
listed in `data/seed/insurance-corpus.json` to index `northwind-insurance-knowledge`,
knowledge source `northwind-insurance-knowledge-ks` and knowledge base
`northwind-insurance-kb`; with `--connect-project` it also creates the
`northwind-insurance-kb-mcp` connection in `northwind-orders`. A listed seed file must declare
synthetic provenance, a source reference, a version, an effective date and a fictional
jurisdiction, or the publisher refuses it. `data/seed/generate.py --domain insurance`
renders the same sources to PDFs locally, with no cloud or model calls. The names describe
intended targets, not proof that the cloud resources exist or hold current content. The
retail `northwind-knowledge` index and `northwind-kb` knowledge base are separate;
`data/seed/index.py` and `upload_blobs.py` reject insurance manifests, so they cannot write
insurance content to the retail index or the shared seed container.

## Help Center customer articles

The one department content set that an embedded assistant serves. The Help Center
(`help_center`, an `extractive_help` surface) appears in the portal as a Help center tab and a
collapsed panel beside Report a loss. It answers general questions from published help
articles and reads no personal record; the Claims Concierge is the assistant that reads a
caller's own policies and claims. Both run through the identity-bound API
(`/api/v1/assistants`; see [Embedded assistants (identity-bound)](../README.md#embedded-assistants-identity-bound)
and [architecture](./architecture.md#identity-bound-embedded-assistants)) and stay disabled
until an operator enables them.

- **Source.** `data/northwind/departments/employee-help-desk/seed/customer/*.md`: twelve
  fictional articles. Each starts with front matter of exactly `id`, `topic`, `version` and
  `effective_from`, then one H1 beginning `# Fictional Northwind help: `. HTML, links and
  images are not allowed, and each article stays under 12,000 characters.
- **Publish.** `python scripts/publish-customer-help.py` validates the articles and
  previews: it prints a summary and makes no network call or write. `--apply` is an operator
  action: it creates or refreshes only the publisher-owned index
  `nw-insurance-help-customer-v1`, authenticating with Entra by default, and refuses an index
  that holds foreign documents. The publisher requires exactly the twelve ids in its
  `EXPECTED_IDS`, so adding or removing an article also means changing that list and the
  assistant golden rows that cite article ids.
- **Isolation.** The articles live in a subfolder, so the department indexer (top-level
  `seed/*.md` only) never sends them to `nw-hr`, and the HR/IT corpus and its prompt agents
  have no portal surface. The API reads only `nw-insurance-help-customer-v1`, through a fixed
  publisher and audience filter.
- **Checks.** `python evals/assistant_eval.py --self-check` runs the deterministic assistant
  golden-set grader offline; it also reads these articles.

## Where each Foundry project lives in Bicep

`infra/modules/foundry.bicep` provisions the shared AI Services account, its model
deployments, the flagship project (`orderProjectName`, default `northwind-orders`) and one
project per entry of the `additionalProjects` param (defaulted in `infra/main.bicep`; each
entry is `{ slug, projectName, displayName, description }`):

```bicep
additionalProjects = [
  { slug: 'employee-help-desk', projectName: 'nw-employee-help-desk', ... }
  { slug: 'claims-warranty',    projectName: 'nw-claims-warranty',   ... }
  { slug: 'contact-center',     projectName: 'nw-contact-center',    ... }
]
```

The projects are created whenever the deployment is not in reuse mode, whatever the business
domain. The account-scoped RBAC roles (`Azure AI User`, `Cognitive Services OpenAI User`) are
granted to the app identity and the dev principal on the account, so every project inherits
them — no per-project role assignments.

The slug-to-endpoint map is the `FOUNDRY_PROJECT_ENDPOINTS` azd output: a JSON object that
`infra/main.bicep` formats by hand for the four slugs, in manifest order.
`create-prompt-agents.py` and `connect-knowledge-base.py` read it (and synthesize it from
`FOUNDRY_ENDPOINT` plus the manifest's `foundry_project` names when it is missing), and
`scripts/validate-reuse.py` requires all four slugs in reuse mode. The Container App still
receives the variable, but the API no longer parses it.

An optional `bakeoffDeployments` param (default `[]`) adds extra model deployments to the same
account. The bake-off tab and endpoint that raced them were removed, so nothing in the portal
or API uses them; the `FOUNDRY_BAKEOFF_DEPLOYMENTS` output remains for operators and the reuse
profile.

## Golden sets and evals

- **Flagship.** `order-concierge`'s golden set is the top-level `data/golden/dataset.jsonl`.
  `evals/push_to_foundry.py`, `evals/register_foundry_evaluator.py` and the nightly optimizer
  (`pipelines/optimizer/run_prompt_optimizer.py`) read it.
- **Assistants.** `data/golden/assistant.jsonl` covers both embedded assistants and is graded
  offline by `python evals/assistant_eval.py --self-check`.
- **Other departments.** The `golden/dataset.jsonl` files of `employee-help-desk`,
  `claims-warranty` and `contact-center` are authored content kept with their packs.
  `data/northwind/_smoke_test.py` checks that they parse as JSONL, and no eval runner or
  workflow reads them now: the Tier 2 golden-set runner and its PR workflow were retired with
  the endpoints they exercised.
- **Baseline.** `evals/baseline.json` keeps one (currently empty) entry per department slug.
  Do not edit it without reviewer approval.

## Adding an embedded assistant surface

A department's content reaches users only through an embedded assistant surface. The design
is in the README's
[Embedded assistants (identity-bound)](../README.md#embedded-assistants-identity-bound)
section and in [architecture](./architecture.md#identity-bound-embedded-assistants).
Surfaces are not departments, so they need no Foundry project or Bicep change:

1. Add a profile to `apps/api/app/assistants/profiles.py` (`surface`, `kind`, `label`,
   `agent`, `roles`, `department`, `prompt_file`, `tools`, `accepts` and `suggestions`) and a
   prompt in `agents/insurance/assistants/<surface>.yaml` that declares
   `business_domain: insurance` and a `surface:` matching the profile id (use `_` in the id
   and `-` in the file name). `kind` is `scoped_tools` for assistants that read a caller's
   records and `extractive_help` for assistants that quote a document corpus. `department`
   should be a manifest slug: it stamps telemetry, and an unknown slug falls back to
   `order-concierge`. `scripts/bundle-assistant-assets.py` packages the prompt into the API
   image and SHA-stamps it.
2. Give customer roles customer-safe tools only: ids as arguments, whitelisted projections,
   no identity, no writes. Extend `ToolBox` (`apps/api/app/assistants/tools.py`) rather than
   reading storage from a handler.
3. For a document corpus, publish an **audience-isolated** index (one index per audience,
   never a filter on a shared index) with a publisher script modelled on
   `scripts/publish-customer-help.py` (it previews by default, and `--apply` writes only the
   publisher-owned index). `extractive_help` is pinned to the one customer index today: its
   name, publisher and audience filter are constants in `profiles.py` and `extractive.py`, so
   a second corpus, such as a staff help desk, also means making that mapping per surface.
4. Mount `<AssistantPanel surface="..." context={...} />` in the portal section it helps
   with, or `<AssistantDisclosure>` beside a form (never inside one, because the panel has
   its own form). Both render nothing unless `/api/v1/assistants/status` lists the surface
   for the caller. Add the surface's wording to `VARIANTS` in
   `apps/web-tech/src/business/assistants/AssistantPanel.tsx`; a surface without an entry
   gets the records wording.
5. Add golden rows to `data/golden/assistant.jsonl` (including scope, injection and
   determination bait) and make `python evals/assistant_eval.py --self-check` and the API's
   offline tests pass before listing the surface in `NORTHWIND_ASSISTANT_SURFACES`. A surface
   stays off until `NORTHWIND_ASSISTANTS_ENABLED` is `true`.

## Adding a new department

A new department is data plus platform assets. It has **no portal surface** unless an
[assistant surface](#adding-an-embedded-assistant-surface) is added for it.

1. Add `data/northwind/departments/<slug>/` with a `department.yaml`, a `seed/` corpus
   (top-level `*.md` files), at least one `prompts/*.yaml` and a `golden/dataset.jsonl` (by
   convention at least 10 rows, each carrying the slug).
2. Add the department to `data/northwind/company.yaml` with the keys `data.northwind`
   requires (`slug`, `name`, `tagline`, `pillar`, `foundry_project`, `search_index`,
   `seed_dir`, `golden_path`, `prompts_dir`), and run `python data/northwind/_smoke_test.py`.
3. Provision its project: add a `{ slug, projectName, displayName, description }` entry to
   `additionalProjects` in `infra/main.bicep`, and add the slug to the hand-formatted
   `FOUNDRY_PROJECT_ENDPOINTS` output next to it. While that variable is set, a slug missing
   from it is skipped as "no project endpoint" and `create-prompt-agents.py` exits non-zero.
4. Add an entry for the slug to `_DEPT_INSTRUCTIONS` in `scripts/create-knowledge-base.py`;
   without one, no knowledge base is created for it.
5. Run the bootstrap ([Bootstrapping the packs](#bootstrapping-the-packs)): in the retail
   domain `azd provision` runs it through the postprovision hook; otherwise run the four
   scripts yourself against a verified target.
