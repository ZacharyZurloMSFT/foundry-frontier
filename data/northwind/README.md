# `data/northwind/` — one company, four departments

This tree holds the Northwind company manifest, the four departments' content packs
(seed corpus, prompt agents, golden set, Content Understanding assets) and the shared
synthetic insurance fixtures. The data/agent bootstrap scripts, a few API, evals and CI
consumers, and the API image read it. The portal does not: there is no department
picker or per-department tab.
[`docs/departments.md`](../../docs/departments.md) explains what a department is now,
which script reads which file, and how to add one.

## Layout

```
data/northwind/
  README.md                       this file
  __init__.py                     loader: load_company(), load_department(), list_departments()
  _smoke_test.py                  checks the manifest, seed dirs, golden JSONL and prompt YAML
  company.yaml                    the manifest: the company and its four departments
  insurance-fixtures.json         synthetic policyholders, policies, claims and policy wordings
  departments/
    order-concierge/
      department.yaml             no content of its own; paths point at data/seed,
                                  data/golden/dataset.jsonl, agents/ and
                                  pipelines/content-understanding
    employee-help-desk/
      department.yaml
      seed/*.md                   HR/IT corpus, indexed into `nw-hr`
      seed/customer/*.md          customer Help Center articles, published to their own index
      golden/dataset.jsonl
      prompts/*.yaml              northwind-hr-assistant and its -gpt-5-4, -gpt-5-4-mini
                                  and -kimi variants
    claims-warranty/
      department.yaml
      claims-documents.json       allowlist of synthetic claim PDFs (API claim-document review)
      cu-analyzers/claim-intake.json   Content Understanding analyzer definition
      seed/*.md                   FNOL, repair estimate, policy wording, evidence procedure
      golden/dataset.jsonl
      prompts/*.yaml              northwind-claims-triage
    contact-center/
      department.yaml
      seed/*.md                   call scripts, IVR flow, escalation playbook, intent
                                  taxonomy, deflection FAQ
      golden/dataset.jsonl
      prompts/                    northwind-intent-classifier.yaml; greeting.md and
                                  voice-live-system.md (Voice Live prompts, no consumer now)
```

Each `seed/` directory also holds a `README.md`; the department indexer skips it.

## Contract

`company.yaml` is authoritative: it is what `data.northwind` loads. Each
`departments/<slug>/department.yaml` is a descriptive copy of that entry plus authoring
notes (`agents:`, `corpus_theme:`); no code reads it, so keep the two in step by hand.

A department entry in `company.yaml` must define:

- `slug`, `name`, `tagline` and `pillar`
- `foundry_project` (its Foundry project) and `search_index` (its Search index)
- `seed_dir`: the corpus. For the three non-flagship departments its top-level `*.md` files
  are indexed into `search_index`; audience-specific content belongs in a subfolder (like
  `seed/customer`), which is never indexed there
- `prompts_dir`: `*.yaml` prompt agents, each with `name` and `instructions`. A
  `tools: [{type: knowledge, index: ...}]` entry becomes a knowledge tool over that
  index's knowledge base
- `golden_path`: the golden dataset (by convention at least 10 rows, each carrying the
  department slug)

`icon`, `brand_color`, `cu_inbox_prefix`, `cu_analyzer_dir` and `customer_archetype` are
optional and descriptive: the loader parses them, but no script or API code acts on them.

## Loading

Use `data.northwind.load_company()` from Python (`load_department(slug)` and
`list_departments()` are thin wrappers). The loader resolves every path in the manifest
against the repo root and returns absolute paths, so callers don't have to think about
layout or the working directory. `python data/northwind/_smoke_test.py` loads the manifest
and checks each department's seed directory, golden JSONL and prompt YAML.

The API image bundles this tree as `apps/api/app/_northwind/` (the `azure.yaml` prepackage
hook and `deploy-apps.yml` copy it), where `apps/api/app/company.py` reads `company.yaml`
directly. It uses the manifest for one thing: `resolve_department()` maps an assistant
profile's `department` slug to its `foundry_project`, which stamps telemetry.

## Who reads what

| Reader | Files |
| --- | --- |
| `scripts/create-department-indexes.py` | `company.yaml`; each department's top-level `seed/*.md` |
| `scripts/create-knowledge-base.py`, `scripts/connect-knowledge-base.py` | `company.yaml` |
| `scripts/create-prompt-agents.py` | `company.yaml`; each department's `prompts/*.yaml` |
| `scripts/publish-customer-help.py`, `evals/assistant_eval.py` | `employee-help-desk/seed/customer/*.md` |
| `scripts/publish-insurance-knowledge.py`, `data/seed/generate.py` | `insurance-fixtures.json` and the department seed files listed in `data/seed/insurance-corpus.json` |
| `scripts/configure-insurance-cu.py` | `claims-warranty/cu-analyzers/claim-intake.json` |
| `apps/api/app/company.py` | `company.yaml` |
| `apps/api/app/content_understanding.py` | `claims-warranty/claims-documents.json` |
| `apps/api/app/insurance_domain.py`, the two MCP servers, hosted agent packages, `evals/insurance.py`, `evals/insurance_review.py`, `evals/assistant_eval.py` | `insurance-fixtures.json` |
| `scripts/release-app.py` | any change under `data/northwind/` selects the `api`, `mcp-tech-lookup` and `mcp-work-order` services for release |

The three non-flagship departments' `golden/dataset.jsonl` files are read only by
`_smoke_test.py`; no eval runner or workflow reads them now.

## Why the odd Order Concierge paths

`order-concierge` is the flagship and has no content folder of its own. Its manifest entry
points at top-level assets that other tooling reads directly: `data/seed` (the seed
generator), `data/golden/dataset.jsonl` (the eval scripts and the nightly optimizer),
`agents/` (the hosted agents and the prompt registry) and `pipelines/content-understanding`.
`create-department-indexes.py` skips it, and `create-prompt-agents.py` skips it while
`ORDER_AGENTS_HOSTED` is `true`.
