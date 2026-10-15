# `data/northwind/` — one company, four departments

This tree is the manifest for the **one company, four Foundry projects**
reshape of the demo. It is the single source of truth that the API,
postprovision scripts, and the web-tech landing page all read.

## Layout

```
data/northwind/
  company.yaml                    ← top-level manifest (departments + platform tabs)
  departments/
    order-concierge/
      department.yaml             ← paths point back to the pre-existing
                                    data/seed, data/golden, agents/ so `azd up`
                                    keeps working during the phased rollout
    employee-help-desk/
      department.yaml
      seed/                       ← HR/IT synthetic docs (Phase 4)
      golden/dataset.jsonl        ← RAG goldens (Phase 4)
      prompts/                    ← northwind-hr-assistant prompt agent overlay
    claims-warranty/
      department.yaml
      seed/                       ← warranty PDFs, damage photos, intake forms
      golden/dataset.jsonl        ← CU extraction goldens
      prompts/                    ← claims-triage prompt agent overlay
      cu-analyzers/               ← per-department CU analyzer schemas
    contact-center/
      department.yaml
      seed/                       ← call scripts, IVR flows, escalation playbook
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
  overlays existing top-level `agents/*.yaml`

## Loading

Use `data.northwind.load_company()` from Python. The loader resolves paths
relative to the repo root, so callers don't have to think about layout.

## Why the odd Order Concierge paths

During the phased rollout, Order Concierge intentionally points at the
pre-existing top-level `data/seed`, `data/golden`, and `agents/` so that
every current script (`scripts/postprovision.*`, `.github/workflows/`,
`evals/`) keeps working unchanged. A later phase physically moves those
files under `data/northwind/departments/order-concierge/` and drops the
top-level compat shims.
