# Retail vs insurance

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
touching names, projects, agents or resources that differ between the retail and insurance business domains. Keep
each entry short and cite a source.

- **Do not mix retail and insurance resources.** Insurance chat uses only `POST /insurance/chat` (after a fresh
  readiness check) and `northwind-insurance-*` agents in the flagship project (`northwind-orders` in
  `company.yaml`); the legacy `northwind` project and the old retail agent names are incompatible. The retail
  `northwind-knowledge` index and `northwind-kb` knowledge base are unchanged and are not insurer corpora; the
  legacy `data/seed/index.py` and `upload_blobs.py` reject insurance manifests on purpose. Source:
  [departments.md](../../departments.md#active-insurance-surfaces-and-rollout-boundary).
- **The flagship department's files are not under its slug folder.** `order-concierge` points at the top-level
  `data/seed/`, `data/golden/` and `agents/` so `azd up` never breaks; the other three departments live under
  `data/northwind/departments/<slug>/` (real paths: [`company.yaml`](../../../data/northwind/company.yaml)).
- **Old names are often still the live identifiers.** `mcp-tech-lookup`, `mcp-work-order`, `ca-mcp-*`,
  `tech_lookup`, `work_order_draft`, the `order-concierge` slug and the `northwind` project name are legacy but
  stable (`company.yaml` calls them "stable legacy resource identifiers"). Do not rename them as a drive-by
  cleanup, and check the [glossary](../glossary.md#naming-lineage) before assuming a name reflects today's domain.
- **Live insurance project names differ from `company.yaml` on `main`.** Live: `nw-insurance-claims`,
  `nw-insurance-support`, `nw-insurance-documents`, `nw-insurance-contact-center`; `company.yaml` still has
  `northwind-orders`, `nw-employee-help-desk`, `nw-claims-warranty`, `nw-contact-center` (same order). The rename is
  unmerged work (`insurance-resource-cleanup`): the live API image carries a `company.yaml` overlay and the code
  accepts both flagship names (`INSURANCE_PROJECTS` in
  [`insurance.py`](../../../apps/api/app/insurance.py)). Do not align one side as a drive-by fix; re-check this
  entry when that work lands. Sources: [ai-gateway.md](../../ai-gateway.md#live-inventory),
  [api-management.md](../../api-management.md#insurance-stack-cut-over-2026-10-23).
- **Insurance apps are released by hand, canary-first.** A CI `api` image once made `/health` report
  `insurance_agent_runtime_ready: false` (`main` hard-coded `northwind-orders`), and a CI MCP image exposes the
  retail tools unless `NORTHWIND_MCP_INSURANCE_ONLY=true`. That switch is not derived from the business domain:
  `ca-mcp-*` run in it too and must keep every tool. Canary on a throwaway copy of the live app and keep the
  previous digest. Source: [api-management.md](../../api-management.md#insurance-stack-cut-over-2026-10-23).
