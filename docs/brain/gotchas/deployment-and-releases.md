# Deployment and releases

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
changing `infra/`, `azure.yaml`, the azd hooks or the release lane, merging a PR or releasing apps. Keep each entry
short and cite a source.

## `azd up` and deployment

- **Nothing runs after `azd provision`.** The azd hooks are `preprovision` and `predeploy` (both run
  `scripts/validate-reuse.py`) plus each service's `prepackage`; there is no `postprovision` hook and no hosted-agent
  azd service. A fresh `azd up` gives running apps but no knowledge base, prompt agents, hosted agents or Content
  Understanding analyzer: those are explicit, dry-run-first scripts, and agent inference stays `503` until an
  operator sets `NORTHWIND_INSURANCE_AGENTS_READY`. Sources: [`azure.yaml`](../../../azure.yaml),
  [README](../../../README.md#fresh-environment).
- **`validate-reuse.py` blocks by design.** It checks the azd environment without Azure calls or writes, and a failure
  is a real stop, not noise: a flag that is not exactly `true` or `false`, readiness without a
  `northwind-insurance-` orchestrator id, a gateway URL that is not a bare `https://<name>.azure-api.net` origin, or
  an older azd environment whose `NORTHWIND_GATEWAY_STACK` is not `insurance`. Source:
  [`validate-reuse.py`](../../../scripts/validate-reuse.py).
- **Generated copies: edit the source.** `apps/api/app/_northwind/` (from `data/northwind/` plus the fixtures and four
  claim PDFs) and `apps/api/app/_agent_prompts/` (the harness and adjuster-support prompts, from
  `scripts/bundle-harness-assets.py`) are git-ignored build inputs, as are `data/seed/generated/` (run
  `python data/seed/generate.py --domain insurance` before packaging or testing the API) and `.release-state/` (the
  grant and enforce receipts that rollback needs; keep it). The MCP apps carry **checked-in** copies of
  `gateway_guard.py`, `insurance_domain.py` and the fixtures: edit
  [`apps/api/app/gateway_guard.py`](../../../apps/api/app/gateway_guard.py) and re-run
  `python scripts/bundle-insurance-assets.py --service mcp-policy-lookup` (and `mcp-claim-review`);
  `scripts/tests/test_deployment_insurance_bundle.py` fails when a guard copy drifts. Sources:
  [`.gitignore`](../../../.gitignore), [`azure.yaml`](../../../azure.yaml),
  [api-management.md](../../api-management.md#repository-map).
- **Five apps, one name map.** The azd service names and the Container App each maps to live in `azure.yaml`, in
  `SERVICE_APPS` in `scripts/release-app.py` and in `.github/workflows/deploy-apps.yml`; the Container App names also
  appear in the `insurance` stack of `infra/live/apim-apis.bicep`. Tests fail when they drift. Each image is tagged
  `<service>:<sha>-<run>-<attempt>`, so renaming a service starts a new registry repository: read
  [legacy-decommission.md](../../legacy-decommission.md#4-container-apps-rg-northwind-apps) before deleting an old one,
  and never rename live Azure resources from code (golden rule 1).
- **`AZURE_RESOURCE_GROUP` is the Foundry AI resource group, not Apps.** App services use
  `AZURE_APPS_RESOURCE_GROUP` (`resourceGroup` in [`azure.yaml`](../../../azure.yaml)), and reuse mode requires
  `AZURE_RESOURCE_GROUP` to agree with `AZURE_FOUNDRY_RESOURCE_GROUP`. Sources:
  [README](../../../README.md#existing-deployment-reuse-mode),
  [architecture.md](../../architecture.md#networking-regions-and-identity).
- **Hosted agents: platform and readiness settings stay out of the deploy config.** `FOUNDRY_*` variables are
  platform-reserved and `NORTHWIND_INSURANCE_AGENTS_READY=true` is refused: readiness is an API setting that an
  operator flips after verifying every specialist. Install `agents/hosted/requirements.txt` in a dedicated environment
  (not the shared API one) and never package virtual environments, tests or secrets. Sources:
  [architecture.md](../../architecture.md#hosted-agents), [README](../../../README.md#hosted-agents).
- **Config is intent, not evidence.** Manifests, agent YAML and settings describe intended targets, not proof that
  a resource exists or is current. Readiness gates are real: insurance chat, `/handoff`, voice and harness runs stay
  unavailable until `/insurance/fixtures` reports `agent_runtime_ready: true`, and the harness is not `ready` until a
  complete SDK run verifies it. Sources:
  [architecture.md](../../architecture.md#runtime-readiness-and-feature-switches),
  [departments.md](../../departments.md#search-knowledge-bases-and-what-is-actually-queried).

## Releases and merging

- **Some paths release every app, and a release aborts when `main` moves.** `scripts/`, `.github/workflows/`,
  `agents/copilot-harness/`, `azure.yaml` and `.dockerignore` release all five services; docs-only changes release
  none. The release re-checks that `main` still equals its source SHA, so merging while `Scoped App Release` runs
  aborts it part-way; an abort does not advance the baseline, so the next release covers it (check that it
  passed). Sources: [`release-app.py`](../../../scripts/release-app.py),
  [`deploy-apps.yml`](../../../.github/workflows/deploy-apps.yml).
- **Live apps were released by hand, canary-first; canary an `api` build from `main` before releasing it.** The
  workflow now covers all five `ca-insurance-*` apps, but the live API image was built from `main` plus a data
  overlay carrying the live department project names, and no workflow build has been canaried against it yet. The
  overlay's content is now in `data/northwind/company.yaml`, so a new build no longer needs it. A CI `api` image
  once made `/health` report `insurance_agent_runtime_ready: false` (`main` then hard-coded the original project
  name; `INSURANCE_PROJECTS` now accepts both). Canary on a throwaway copy of the live app and keep the previous
  digest. Sources: [insurance cut-over](../../api-management.md#insurance-stack-cut-over-2026-10-23),
  [Remaining steps](../../api-management.md#remaining-steps).
