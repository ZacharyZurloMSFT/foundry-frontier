# Gotchas

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Non-obvious traps, written as **trap, why, what to do**. Add new ones here instead of creating new
top-level notes. Keep each entry short and cite a source.

## Evals and prompts

- **Missing evidence is a failure, not a pass.** The golden runner reports `regression_status=unavailable` or a
  `baseline_error` and exits nonzero when the baseline is empty or malformed or no judge is configured; a gateway
  failure (401/403/404/429), disabled target or unscored attack is likewise an evaluation failure, never a skip.
  Never fabricate numbers, skip invalid entries, or edit `evals/baseline.json` to turn it green; a reviewer
  approves measured values (golden rule 2). The judge, Foundry safety evaluators, red team and the GitHub Models
  smoke tier call their services directly, not through the AI Gateway. Sources:
  [evals/README.md](../../evals/README.md#copilot-sdk-harness),
  [evals/README.md](../../evals/README.md#existing-golden-runner-corrections-and-offline-validation),
  [genaiops.md](../genaiops.md#gateway-routing-and-the-tiers),
  [genaiops.md](../genaiops.md#copilot-sdk-harness-evidence).
- **Judge model compatibility.** The golden runner's judge SDK is pinned (`azure-ai-evaluation==1.1.0`) and uses
  `max_tokens`; gpt-5/o-series names are rejected as judge. Set `FOUNDRY_JUDGE_DEPLOYMENT` to a verified
  chat-completions deployment. Source:
  [evals/README.md](../../evals/README.md#existing-golden-runner-corrections-and-offline-validation).
- **`python evals/smoke_eval.py --self-check` needs `GITHUB_TOKEN`** and does not validate Azure readiness.
  Source: [evals/README.md](../../evals/README.md#existing-golden-runner-corrections-and-offline-validation).
- **Offline tests are not live evidence.** The real-SDK harness contract tests make no Azure requests. Live eval
  jobs need private-network access to Foundry: use a connected runner, do not open private resources to make CI
  pass. Sources: [genaiops.md](../genaiops.md#copilot-sdk-harness-evidence),
  [evals/README.md](../../evals/README.md#copilot-sdk-harness).
- **Do not run cloud-write or billable commands unprompted.** Commands marked `CLOUD WRITE`, `MODEL CALLS` or
  `CLOUD UPLOAD + BILLABLE TRAINING` in `evals/README.md` need explicit operator approval and never belong in an
  API or UI request path. Source:
  [evals/README.md](../../evals/README.md#orders-triage-learning-approvals-before-uploads-or-training).
- **Trace content capture stays off.** Enable full prompt, response or tool payload capture only for a controlled
  debugging session; those fields can contain customer data. Source:
  [genaiops.md](../genaiops.md#generating-agent-traces-on-demand).

## Approvals and agent tools

- **Approval and business execution are never agent tools.** Agents only enqueue *pending* items; humans decide
  in HITL. Model-generated text is never proof of approval or persistence, and `workflow_id` comes from
  application-owned metadata ("prompt prefixes are not authority"). Exposing HITL decide or delete operations as
  tools breaks this. Sources: [architecture.md](../architecture.md#order-concierge-hosted-runtime),
  [architecture.md](../architecture.md#copilot-sdk-agent-harness), [ai-gateway.md](../ai-gateway.md#findings-to-act-on).

## `azd up` and deployment

- **A fresh `azd up` runs the legacy retail bootstrap.** `businessDomain` defaults to `retail`
  ([`infra/main.bicep`](../../infra/main.bicep)), so `postprovision` enables public access, fetches keys, seeds
  data and creates agents. `NORTHWIND_BUSINESS_DOMAIN=insurance` skips all of that (publish knowledge explicitly
  with `scripts/publish-insurance-knowledge.py`); `NORTHWIND_REUSE_EXISTING_RESOURCES=true` skips everything.
  Values must be exactly `retail` / `insurance` and `true` / `false` or the hook fails. Keep `postprovision.sh` and
  `postprovision.ps1` in `scripts/` equivalent: azd runs the `.ps1` on Windows, the `.sh` elsewhere (`azure.yaml`).
- **Hooks block by design.** `scripts/validate-reuse.py` runs in azd `preprovision` and `predeploy`, and each
  hosted agent's `prepackage` / `predeploy` runs `validate-reuse.py --agent-service <name>` with
  `continueOnError: false`. A failure there is a real stop, not noise. Source: [`azure.yaml`](../../azure.yaml).
- **Generated copies are git-ignored; edit the source.** Bundled at azd package time: `apps/api/app/_northwind/`
  (from `data/northwind/` plus insurance assets, `scripts/bundle-insurance-assets.py`),
  `apps/api/app/_agent_prompts/` (from `agents/copilot-harness/`, `scripts/bundle-harness-assets.py`) and
  `agents/*/agents/` (`scripts/orders_agent_package.py`); `data/seed/generated/` is regenerated at deploy.
  `.release-state/` holds the grant and enforce receipts that rollback needs; keep it. The MCP apps'
  `gateway_guard.py` copies also come from `scripts/bundle-insurance-assets.py`: edit
  [`apps/api/app/gateway_guard.py`](../../apps/api/app/gateway_guard.py). Sources: [`.gitignore`](../../.gitignore),
  [`azure.yaml`](../../azure.yaml), [api-management.md](../api-management.md#repository-map).
- **`AZURE_RESOURCE_GROUP` is the Foundry AI resource group, not Apps.** App services use
  `AZURE_APPS_RESOURCE_GROUP` (`resourceGroup` in [`azure.yaml`](../../azure.yaml)). Source:
  [architecture.md](../architecture.md#replacement-stack-placement).
- **Container Apps environment attachment is immutable.** To move an app to another environment, recreate it
  there and then move the recreated resource; a resource-group move alone cannot change the attachment. Source:
  [architecture.md](../architecture.md#replacement-stack-placement).
- **Hosted agents: do not redeclare platform variables.** The platform injects `FOUNDRY_PROJECT_ENDPOINT`, agent
  name and version, so do not redeclare reserved `FOUNDRY_*` variables in deployment environment maps. Install
  `agents/hosted/requirements.txt` in the agent image or a dedicated environment (not the shared API one), and never
  package virtual environments, tests or secrets. Source:
  [architecture.md](../architecture.md#order-concierge-hosted-runtime).
- **The legacy orchestrator needs its own azd environment.** `agents/legacyorchestrator` uses the isolated
  `northwind-legacy-gateway` environment, not the root `northwind-dev` one, and its local environment must bind
  both `AZURE_AI_PROJECT_ENDPOINT` and `FOUNDRY_PROJECT_ENDPOINT` to the legacy project so azd cannot pick an
  unrelated default. Source: [architecture.md](../architecture.md#order-concierge-hosted-runtime).
- **Config is intent, not evidence.** Manifests, agent YAML and settings describe intended targets, not proof that
  a resource exists or is current. Readiness gates are real: insurance chat, harness execution, voice and handoff
  stay unavailable until `/insurance/fixtures` reports `agent_runtime_ready: true`, and harness dependencies are
  not "ready" until a real SDK run completes. Source:
  [departments.md](../departments.md#active-insurance-surfaces-and-rollout-boundary).

## Retail vs insurance

- **Do not mix retail and insurance resources.** Insurance chat uses only `POST /insurance/chat` (after a fresh
  readiness check) and `northwind-insurance-*` agents in the flagship project (`northwind-orders` in
  `company.yaml`); the legacy `northwind` project and the old retail agent names are incompatible. The retail
  `northwind-knowledge` index and `northwind-kb` knowledge base are unchanged and are not insurer corpora; the
  legacy `data/seed/index.py` and `upload_blobs.py` reject insurance manifests on purpose. Source:
  [departments.md](../departments.md#active-insurance-surfaces-and-rollout-boundary).
- **The flagship department's files are not under its slug folder.** `order-concierge` points at the top-level
  `data/seed/`, `data/golden/` and `agents/` so `azd up` never breaks; the other three departments live under
  `data/northwind/departments/<slug>/` (real paths: [`company.yaml`](../../data/northwind/company.yaml)).
- **Old names are often still the live identifiers.** `mcp-tech-lookup`, `mcp-work-order`, `ca-mcp-*`,
  `tech_lookup`, `work_order_draft`, the `order-concierge` slug and the `northwind` project name are legacy but
  stable (`company.yaml` calls them "stable legacy resource identifiers"). Do not rename them as a drive-by
  cleanup, and check the [glossary](glossary.md#naming-lineage) before assuming a name reflects today's domain.
- **Live insurance project names differ from `company.yaml` on `main`.** Live: `nw-insurance-claims`,
  `nw-insurance-support`, `nw-insurance-documents`, `nw-insurance-contact-center`; `company.yaml` still has
  `northwind-orders`, `nw-employee-help-desk`, `nw-claims-warranty`, `nw-contact-center` (same order). The rename is
  unmerged work (`insurance-resource-cleanup`): the live API image carries a `company.yaml` overlay and the code
  accepts both flagship names (`INSURANCE_PROJECTS` in [`insurance.py`](../../apps/api/app/insurance.py)). Do not
  align one side as a drive-by fix; re-check this entry when that work lands. Sources:
  [ai-gateway.md](../ai-gateway.md#live-inventory),
  [api-management.md](../api-management.md#insurance-stack-cut-over-2026-10-23).

## API and gateways

- **Changing the API has two follow-ups.** After changing the FastAPI surface, run
  `python scripts/export-openapi.py` and commit the outputs under `infra/live/apim/specs/` (CI's
  `test_export_openapi.py` fails on drift; `--check` is the strict comparison). Adding an APIM stack or resource
  type is a deliberate boundary change: update `infra/live/apim-apis.bicep`, the allow-list and role in
  `scripts/apim-release.py` and `scripts/configure-github-oidc.py`, and the tests together. Source:
  [api-management.md](../api-management.md#changing-the-api).
- **APIM plans are noisy.** Every API, policy and the logger show as *Modify* and product links as *Create* even
  when nothing changed. The real change signal is the diff of
  `infra/live/apim/specs/northwind-api.openapi.json`, and every entry must still fall inside the
  `apim-release.py` allow-list. Source: [api-management.md](../api-management.md#repository-visibility).
- **A missing required query parameter returns 404 through APIM**, not the backend's 422, because OpenAPI import
  puts required query parameters into the URL template. Source:
  [api-management.md](../api-management.md#findings-that-shaped-the-design-verified-live-on-scratch-apis).
- **The work-order MCP cannot go through the AI Gateway.** The gateway drops client headers, and the work-order
  server binds `x-orders-workflow` / `x-harness-workflow` to the workflow. Route it through `apim-northwind`;
  it can stay direct only while its app is not locked behind the gateway guard, and a locked app rejects direct
  calls with 401 (the live apps are locked). For one hosted role, `MCP_GATEWAY_CONNECTION` and an
  `ORDERS_MCP_GATEWAY` entry are mutually exclusive (startup error). Sources:
  [ai-gateway.md](../ai-gateway.md#two-gateways), [ai-gateway.md](../ai-gateway.md#findings-to-act-on).
- **AI Gateway model names are `<provider>/<registration>`.** The registration is the deployment name lowercased
  with dots replaced by dashes (`gpt-5.4` becomes `gpt-5-4`); bare deployment names return 404, and
  `ORDERS_MODEL_GATEWAY_DEPLOYMENT` must match the selected deployment. Prompt agents cannot use these aliases
  (Foundry rejects them as model ids), so their model calls stay direct and `AI_GATEWAY_MODEL_CONNECTION` should
  stay unset. Sources: [architecture.md](../architecture.md#order-concierge-hosted-runtime),
  [ai-gateway.md](../ai-gateway.md#findings-to-act-on) (finding 9).

## Local development

- **Use port 8001 and `127.0.0.1`.** On Windows wslrelay grabs `:8000` (`ERR_CONNECTION_RESET` on every
  endpoint), and `localhost` can fail where `127.0.0.1` works (IPv6 vs IPv4). `scripts/dev-api.*` already uses
  8001. Source: [local-dev.md](../local-dev.md#common-gotchas).
- **gpt-5.x and o-series need `max_completion_tokens`**, not `max_tokens`; this only affects custom model code.
  Source: [local-dev.md](../local-dev.md#common-gotchas).
- **`DeploymentNotFound` in the bake-off, or `DeploymentModelNotSupported` when adding a model.** Set
  `FOUNDRY_BAKEOFF_DEPLOYMENTS` to deployments that exist in your project; catalog availability varies by
  subscription and region. Sources: [local-dev.md](../local-dev.md#common-gotchas),
  [departments.md](../departments.md#adding-partner-models-to-the-bake-off-tab).
