# Deployment and releases

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
changing `infra/`, `azure.yaml` or the `postprovision` hooks, merging a PR or releasing apps. Keep each entry
short and cite a source.

## `azd up` and deployment

- **A fresh `azd up` runs the legacy retail bootstrap.** `businessDomain` defaults to `retail`
  ([`infra/main.bicep`](../../../infra/main.bicep)), so `postprovision` enables public access, fetches keys, seeds
  data and creates agents. `NORTHWIND_BUSINESS_DOMAIN=insurance` skips all of that (publish knowledge explicitly
  with `scripts/publish-insurance-knowledge.py`); `NORTHWIND_REUSE_EXISTING_RESOURCES=true` skips everything.
  Values must be exactly `retail` / `insurance` and `true` / `false` or the hook fails. Keep `postprovision.sh` and
  `postprovision.ps1` in `scripts/` equivalent: azd runs the `.ps1` on Windows, the `.sh` elsewhere (`azure.yaml`).
- **Hooks block by design.** `scripts/validate-reuse.py` runs in azd `preprovision` and `predeploy`, and each
  hosted agent's `prepackage` / `predeploy` runs `validate-reuse.py --agent-service <name>` with
  `continueOnError: false`. A failure there is a real stop, not noise. Source: [`azure.yaml`](../../../azure.yaml).
- **Generated copies are git-ignored; edit the source.** Bundled at azd package time: `apps/api/app/_northwind/`
  (from `data/northwind/` plus insurance assets, `scripts/bundle-insurance-assets.py`),
  `apps/api/app/_agent_prompts/` (from `agents/copilot-harness/`, `scripts/bundle-harness-assets.py`) and
  `agents/*/agents/` (`scripts/orders_agent_package.py`); `data/seed/generated/` is regenerated at deploy.
  `.release-state/` holds the grant and enforce receipts that rollback needs; keep it. The MCP apps'
  `gateway_guard.py` copies also come from `scripts/bundle-insurance-assets.py`: edit
  [`apps/api/app/gateway_guard.py`](../../../apps/api/app/gateway_guard.py). Sources:
  [`.gitignore`](../../../.gitignore), [`azure.yaml`](../../../azure.yaml),
  [api-management.md](../../api-management.md#repository-map).
- **`AZURE_RESOURCE_GROUP` is the Foundry AI resource group, not Apps.** App services use
  `AZURE_APPS_RESOURCE_GROUP` (`resourceGroup` in [`azure.yaml`](../../../azure.yaml)). Source:
  [architecture.md](../../architecture.md#replacement-stack-placement).
- **Container Apps environment attachment is immutable.** To move an app to another environment, recreate it
  there and then move the recreated resource; a resource-group move alone cannot change the attachment. Source:
  [architecture.md](../../architecture.md#replacement-stack-placement).
- **Hosted agents: do not redeclare platform variables.** The platform injects `FOUNDRY_PROJECT_ENDPOINT`, agent
  name and version, so do not redeclare reserved `FOUNDRY_*` variables in deployment environment maps. Install
  `agents/hosted/requirements.txt` in the agent image or a dedicated environment (not the shared API one), and never
  package virtual environments, tests or secrets. Source:
  [architecture.md](../../architecture.md#order-concierge-hosted-runtime).
- **The legacy orchestrator needs its own azd environment.** `agents/legacyorchestrator` uses the isolated
  `northwind-legacy-gateway` environment, not the root `northwind-dev` one, and its local environment must bind
  both `AZURE_AI_PROJECT_ENDPOINT` and `FOUNDRY_PROJECT_ENDPOINT` to the legacy project so azd cannot pick an
  unrelated default. Source: [architecture.md](../../architecture.md#order-concierge-hosted-runtime).
- **Config is intent, not evidence.** Manifests, agent YAML and settings describe intended targets, not proof that
  a resource exists or is current. Readiness gates are real: insurance chat, harness execution, voice and handoff
  stay unavailable until `/insurance/fixtures` reports `agent_runtime_ready: true`, and harness dependencies are
  not "ready" until a real SDK run completes. Source:
  [departments.md](../../departments.md#active-insurance-surfaces-and-rollout-boundary).

## Releases and merging

- **Some paths release every app, and a release aborts when `main` moves.** `scripts/`, `.github/workflows/`,
  `agents/copilot-harness/`, `azure.yaml` and `.dockerignore` release all five services; docs-only changes release
  none. The release re-checks that `main` still equals its source SHA, so merging while `Scoped App Release` runs
  aborts it part-way; an abort does not advance the baseline, so the next release covers it (check that it
  passed). It never touches `ca-insurance-api`, `-lookup` or `-review`. Sources:
  [`release-app.py`](../../../scripts/release-app.py),
  [`deploy-apps.yml`](../../../.github/workflows/deploy-apps.yml).
