# Decommissioning the legacy retail resources (operator runbook)

> **Status: not executed.** The repository cleanup that made Northwind an insurance-only
> application changed code, configuration and documentation only. It did not query or
> change Azure, Entra ID or GitHub. Every resource name below comes from this repository's
> earlier documentation (recorded 2026-10-22) and from the removed code. **Re-verify each
> name with the read-only commands before deleting anything.** The live insurance stack
> (`ca-insurance-*`, the `insurance` APIM stack, `northwind-insurance-*` agents and the
> `nw-insurance-*` / `northwind-insurance-*` Search objects) must never appear in a delete
> command.

## Why this is a separate step

The repository no longer deploys, releases, tests or documents the retail "Order
Concierge", HR/IT, bake-off or safety features. Azure keeps whatever was created earlier:
Bicep and APIM deployments are incremental and never delete, and agents, indexes and role
assignments are created by scripts that never remove them. Leaving those objects costs
money, keeps public unauthenticated endpoints reachable and makes the live inventory
disagree with the repository.

## Ground rules

1. **Order:** stop callers → prove there is no traffic → export or back up → dry run →
   delete → verify. Do one numbered section at a time and stop if anything is unexpected.
2. **Read-only first.** Every section starts with list/query commands. Run them, compare the
   result with the table, and only then run the delete commands.
3. **Keep everything insurance.** If a name contains `insurance`, it is not on this list.
4. **Backups:** export Cosmos documents, APIM definitions and agent definitions before
   deleting them (sections 2, 3 and 6). Keep the exports until the next release is verified.
5. Run as an operator with Contributor on the resource groups concerned. Role grants are not
   changed by the repository's pipelines.

## 0. Inventory (read-only)

Replace the placeholders with the values recorded in your azd environment.

```powershell
az account show --query "{subscription:id, tenant:tenantId}" -o table
az containerapp list -g rg-northwind-apps --query "[].{name:name, fqdn:properties.configuration.ingress.fqdn, created:systemData.createdAt}" -o table
az apim list --query "[?name=='apim-northwind'].{name:name, rg:resourceGroup, sku:sku.name}" -o table
az apim api list -g <apim-resource-group> --service-name apim-northwind --query "[].{id:name, path:path}" -o table
az apim product list -g <apim-resource-group> --service-name apim-northwind --query "[].{id:name, state:state}" -o table
```

Expected legacy items (names as recorded; yours may differ):

| Area | Legacy item | Replacement (keep) |
|---|---|---|
| Container Apps (`rg-northwind-apps`) | `ca-api`, `ca-mcp-tech-lookup`, `ca-mcp-work-order`, `ca-web-tech`, `ca-web-voice` (and `ca-cu-ingest` if it exists) | `ca-insurance-api`, `-lookup`, `-review`, `-portal`, `-voice` |
| APIM `apim-northwind` APIs | `northwind-api`, `northwind-voice`, `northwind-mcp-tech-lookup`, `northwind-mcp-work-order` | `insurance-api`, `insurance-voice`, `insurance-mcp-tech-lookup`, `insurance-mcp-work-order` |
| APIM products / subscription | `northwind-web`, `northwind-agents`, subscription `northwind-agents` | `insurance-web`, `insurance-agents` |
| AI Gateway `ai-gateway-northwind` | tool servers `northwind-orders-knowledge`, `northwind-hr-knowledge`; the OpenAPI facade `northwind-api` | `northwind-insurance-lookup`, `-knowledge`, `-review` |
| Foundry | legacy `northwind` project; prompt agents `northwind-triage`, `northwind-order-lookup`, `northwind-policy-lookup`, `northwind-resolution-draft`, `northwind-triage-review`, `northwind-triage-ft-demo`; hosted agents `northwind-orchestrator` and its four specialists; HR prompt agents `northwind-hr-assistant`, `-gpt-5-4`, `-gpt-5-4-mini`, `-kimi`; connection `northwind-orders-kb-mcp` | `northwind-insurance-*` agents, `northwind-claims-triage`, `northwind-intent-classifier` |
| AI Search | indexes `northwind-knowledge`, `nw-hr` (+ its `-ds`, `-skillset`, `-indexer`), knowledge sources/bases `northwind-knowledge-ks`, `northwind-kb`, `nw-hr-ks`, `nw-hr-kb`; HR/IT documents inside `nw-insurance-adjuster-support` | `northwind-insurance-knowledge`, `northwind-insurance-kb`, `nw-insurance-adjuster-support` (adjuster documents only) |
| Cosmos DB `northwind` | documents in `hitl_queue`, `workflow_state`, `conversations` that are not insurance claim actions or insurance sessions | insurance documents (`kind = insurance_claim_action`) |
| Foundry evaluation catalog | rubrics registered for `tech_lookup`, `manual_retrieval`, `work_order_draft` | the insurance evaluators registered by `evals/register_foundry_evaluator.py` |
| GitHub / Entra | role assignments of `id-gh-northwind-apps` (and any other pipeline identity) scoped to the retired apps; repository variables that name them | roles scoped to `ca-insurance-*` |

## 1. Prove there is no traffic

Do this for at least one full business cycle (the Container Apps keep their request history
for the retention window of the workspace).

- API Management: use the instance's gateway analytics or the Log Analytics tables its
  diagnostic settings write to. Filter on the legacy API ids (`northwind-api`,
  `northwind-voice`, `northwind-mcp-*`); the last call time of each must be older than your
  retention requirement.
- Container Apps: check request counts and last activity of the five legacy apps (portal
  metrics, or `ContainerAppConsoleLogs_CL` in the workspace).
- Foundry: check each legacy agent's recent runs and traces in the Foundry portal.
- AI Gateway: `GatewayMCPLogs` for the retired tool servers.

Anything with recent traffic is not decommissioned yet: find the caller first.

## 2. Cosmos DB: retail-era documents (optional hygiene)

The API only lists and decides `insurance_claim_action` drafts, so retail-era documents are
ignored at read time. Removing them is hygiene, not a requirement. Export before deleting.

```sql
-- Cosmos SQL (Data Explorer, container hitl_queue)
SELECT c.id, c.workflowId, c.kind, c.status FROM c WHERE c.kind != 'insurance_claim_action'
```

1. Run the query in `hitl_queue`, export the rows (Data Explorer → Download results, or a
   one-off script using `azure-cosmos` with `DefaultAzureCredential`), and store the file.
2. Delete each exported item with its partition key (`workflowId`). Do the same review for
   `workflow_state` (workflow ids without the `wf-insurance-` prefix) and `conversations`
   (sessions that predate the insurance release).

Rollback: re-import the exported JSON with `create_item`.

## 3. API Management (`apim-northwind`)

`infra/live/apim-apis.bicep` now manages only the `insurance` stack. Deleting the legacy
`northwind` objects is a manual step.

1. Back up first: export each legacy API (portal → API → Export → OpenAPI, or
   `az apim api export`) and note the products and the subscription.
2. Delete in this order: the `northwind-agents` subscription, then the APIs
   `northwind-mcp-tech-lookup`, `northwind-mcp-work-order`, `northwind-voice`, `northwind-api`,
   then the products `northwind-agents` and `northwind-web`.

```powershell
az apim api delete -g <rg> --service-name apim-northwind --api-id northwind-api --delete-revisions true --yes
az apim product delete -g <rg> --service-name apim-northwind --product-id northwind-agents --delete-subscriptions true --yes
```

Keep the shared logger `northwind-appinsights` and everything prefixed `insurance`. The next
`scripts/apim-release.py` plan should show no changes to the insurance objects.

Rollback: re-import the exported definitions, or re-create the stack by adding it back to
the `stacks` parameter of `apim-apis.bicep`.

## 4. Container Apps (`rg-northwind-apps`)

The five legacy apps have public ingress; the MCP apps have no authentication.

```powershell
az containerapp show -g rg-northwind-apps -n ca-mcp-work-order --query "properties.template.containers[0].image"   # note the image for rollback
az containerapp delete -g rg-northwind-apps -n ca-api --yes
# repeat for ca-mcp-tech-lookup, ca-mcp-work-order, ca-web-tech, ca-web-voice (and ca-cu-ingest if present)
```

Do not delete the Container Apps environment, the registry or the managed identity: the
insurance apps share them. Remove the legacy images from the registry only after the apps
are gone.

**Registry repositories after the service rename.** The release lane pushes each app's image
to a repository named after its azd service. The services are now `api`, `mcp-policy-lookup`,
`mcp-claim-review`, `web-portal` and `web-voice`, so the first release creates the
repositories `mcp-policy-lookup`, `mcp-claim-review` and `web-portal`. Until that release,
`ca-insurance-lookup`, `ca-insurance-review` and `ca-insurance-portal` still run images from
the old repositories `mcp-tech-lookup`, `mcp-work-order` and `web-tech`. **Do not delete those
three repositories until each of the three apps runs an image from its new repository**
(`az containerapp show -g rg-northwind-apps -n <app> --query "properties.template.containers[0].image"`),
otherwise a restart or scale-out cannot pull the image. The `api` and `web-voice`
repositories are shared by both stacks and are not renamed.

Rollback: `az containerapp create` from the recorded image reference, or redeploy a tagged
release from git history (the pre-cleanup commit still contains the `northwind` lanes).

## 5. AI Gateway (`ai-gateway-northwind`)

`infra/live/ai-gateway.json` no longer lists the retail knowledge tool servers, so the next
`scripts/ai-gateway-release.py plan` reports `northwind-orders-knowledge` and
`northwind-hr-knowledge` as `unlisted` rather than deleting them.

1. Run the plan and confirm those two (and only those) are `unlisted`.
2. Delete them with the gateway control plane (`workspaces/default/toolServers`, ARM
   `2025-09-01-preview`, or the preview `az aigateway` CLI described in
   [`ai-gateway.md`](./ai-gateway.md)).
3. Review the `northwind-api` OpenAPI facade: it publishes the HITL decide/delete operations
   as agent tools, which the repository's rules forbid. Remove it or narrow it to read-only
   operations.
4. Remove model/tool role grants held by the orphaned identities of the removed projects
   (list with `az role assignment list --scope <gateway-resource-id>`).

## 6. Foundry

1. Export each legacy agent definition (portal "View code" or the SDK) and the connection
   list for the project.
2. Delete the retail and HR prompt agents, the retail hosted agents and the demo companions
   listed in the inventory table, then the connection `northwind-orders-kb-mcp`.
3. Remove the legacy `northwind` project only when it contains nothing else and nothing
   references its endpoint (`FOUNDRY_PROJECT_ENDPOINTS` keys, `INSURANCE_PROJECT_NAME`).
4. Delete the stale evaluation rubrics for `tech_lookup`, `manual_retrieval` and
   `work_order_draft` from the Foundry evaluator catalog.

Do not change the account's default project as part of this runbook.

## 7. AI Search

1. List what exists (single quotes keep PowerShell from expanding `$select`):
   `az rest --resource https://search.azure.com --method get --url 'https://<search>.search.windows.net/indexes?api-version=2025-09-01&$select=name'`.
2. Delete the retail objects in dependency order: indexer `nw-hr-indexer`, data source
   `nw-hr-ds`, skillset `nw-hr-skillset`, knowledge bases and sources, then the indexes
   `nw-hr` and `northwind-knowledge`.
3. In `nw-insurance-adjuster-support`, remove the HR/IT documents. The API already filters
   retrieval to `category eq 'adjuster'`, and `scripts/publish-adjuster-support.py` uploads
   only the four adjuster procedures and never deletes.

```powershell
# keys of the documents to remove (category hr or it) - export the result first
az rest --resource https://search.azure.com --method post `
  --url 'https://<search>.search.windows.net/indexes/nw-insurance-adjuster-support/docs/search?api-version=2025-09-01' `
  --body '{"search":"*","filter":"category eq ''hr'' or category eq ''it''","select":"id","top":100}'
# then post {"value":[{"@search.action":"delete","id":"<id>"}, ...]} to .../docs/index
```

Rollback: the HR/IT corpus is no longer in the repository; restore from the exported
documents or the pre-cleanup commit (`data/northwind/departments/adjuster-support/seed/`).

## 8. GitHub and Entra

- Role assignments: for each pipeline identity (`id-gh-northwind-apps`, `-plan`, `-apply`,
  `-apim-plan`, `-apim-apply`), list assignments (`az role assignment list --assignee <client-id> --all`)
  and delete the ones scoped to the retired Container Apps.
- Repository variables/secrets that name retired resources (for example the old gateway
  URL variable pointing at the `northwind` stack): update them to the `insurance` values.
- Existing azd environments: `NORTHWIND_GATEWAY_STACK` must be `insurance`; an environment
  that still says `northwind` fails preprovision with an explicit message.
- Entra application registrations are not touched by the repository. Remove redirect URIs
  that point at the retired web apps (`ca-web-tech`, `ca-web-voice`) from the sign-in app.

## 9. Developer machines

Git does not move ignored files, so after pulling the directory renames
(`apps/web-tech` → `apps/web-portal`, `apps/mcp-tech-lookup` → `apps/mcp-policy-lookup`,
`apps/mcp-work-order` → `apps/mcp-claim-review`, `pipelines/orders-learning` →
`pipelines/insurance-learning`):

- Move the local, git-ignored state of `pipelines/orders-learning/` (for example
  `insurance-upload-pack/` and `workspace/`) into `pipelines/insurance-learning/` and remove
  the empty old directory.
- Delete leftover `apps/web-tech`, `apps/mcp-tech-lookup` and `apps/mcp-work-order` folders
  (they hold only ignored `node_modules/`, `dist/` or `__pycache__/`), then run `npm ci` in
  `apps/web-portal`.
- The approved insurance evaluation packs pin the earlier prompt files and source hashes, and
  `evals/review-evidence/` pins the earlier source of the evaluation code. Re-prepare the pack
  (`python scripts/insurance-learning.py prepare --output <new pack directory>`) and repeat the
  execution-code review before any native evaluation; the fail-closed lineage checks refuse to
  run until then.

## Done when

- `az containerapp list -g rg-northwind-apps` shows only `ca-insurance-*`.
- `az apim api list` shows only `insurance-*` APIs and `scripts/apim-release.py` plans no
  insurance changes.
- The AI Gateway plan has no `unlisted` retail servers.
- Foundry shows only `northwind-insurance-*`, `northwind-claims-triage` and
  `northwind-intent-classifier` agents.
- `nw-insurance-adjuster-support` contains only `category = adjuster` documents.
- `/health`, `/insurance/fixtures` and the insurance smoke checks
  (`python scripts/apim-smoke.py --gateway-url <gateway-url> --stack insurance --public-checks`) still pass.
