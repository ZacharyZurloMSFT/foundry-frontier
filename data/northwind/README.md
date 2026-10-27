# `data/northwind/` — one company, four departments

This tree is the manifest for the **one company, four Foundry projects**
story of the Northwind Insurance demo. It is the single source of truth that
the API, the bootstrap scripts and the portal web app all read.

## Layout

```
data/northwind/
  company.yaml                    ← top-level manifest (departments + platform tabs)
  insurance-fixtures.json         ← synthetic policyholders, policies, claims and wording
  departments/
    claims-concierge/
      department.yaml             ← paths point back to the top-level
                                    data/seed, data/golden and agents/ assets
    adjuster-support/
      department.yaml
      seed/                       ← versioned adjuster procedures (published to
                                    the isolated nw-insurance-adjuster-support index)
      golden/dataset.jsonl        ← RAG goldens
      prompts/                    ← adjuster-support evidence-selection prompt
    claims-documents/
      department.yaml
      seed/                       ← FNOL, repair estimate and policy wording sources
      golden/dataset.jsonl        ← Content Understanding extraction goldens
      prompts/                    ← claims-triage prompt agent
      cu-analyzers/               ← per-department Content Understanding analyzers
    contact-center/
      department.yaml
      seed/                       ← call scripts, IVR flow, escalation playbook
      golden/dataset.jsonl        ← intent-classification goldens
      prompts/                    ← intent classifier + Voice Live system prompt
```

## Contract

Every department must ship, at minimum:

- `department.yaml` with slug + name + tagline + pillar +
  `foundry_project` + `search_index` + `enabled_tabs`
- a `seed/` corpus (or a pointer to one)
- a `golden/dataset.jsonl` with ≥10 rows keyed by that department's slug
- a `prompts/` folder that either declares a full agent definition or
  overlays the insurance agents under `agents/insurance/*.yaml`

## Loading

Use `data.northwind.load_company()` from Python. The loader resolves paths
relative to the repo root, so callers don't have to think about layout.

## Department slugs

Canonical slugs are `claims-concierge`, `adjuster-support`, `claims-documents`
and `contact-center`. The API still accepts the deprecated slugs
`order-concierge`, `employee-help-desk` and `claims-warranty` (for example in
the `X-Department` header and in `FOUNDRY_PROJECT_ENDPOINTS`) so callers
deployed before the rename keep working. Foundry project names in
`company.yaml` are bound to deployed resources and are not renamed.

## Why Claims Concierge points at top-level paths

The flagship department intentionally reuses the top-level `data/seed`,
`data/golden` and `agents/` directories so every script
(`scripts/`, `.github/workflows/`, `evals/`) keeps resolving the same files.
