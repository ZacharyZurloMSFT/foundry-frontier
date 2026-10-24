# Names and retired vocabulary

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
touching names, projects, agents or resources, or when an earlier demo name turns up in code or docs. Keep each
entry short and cite a source.

- **Retired vocabulary and paths fail CI.** `scripts/tests/test_no_legacy_vocabulary.py` (run by
  `.github/workflows/ci.yml`) scans every tracked or untracked, non-ignored text file, Markdown included, for the
  retired demo vocabulary and for references to deleted paths. Allowed spots are listed in its `ALLOWED` map with a
  reason, and a stale entry also fails. Use the canonical name; add an entry only for a deprecated alias, a live
  identifier or a test of one. Run `python -m pytest scripts/tests/test_no_legacy_vocabulary.py -q` after doc edits.
- **`docs/legacy-decommission.md` is a runbook that nothing executes.** The cleanup changed code, configuration and
  documentation only. Every Azure name in the runbook comes from earlier documentation (recorded 2026-10-22) and
  removed code, so re-verify it with the runbook's read-only commands before deleting anything. CI never runs it:
  do not copy its delete commands into scripts or workflows. Source:
  [legacy-decommission.md](../../legacy-decommission.md).
- **Old names are still read or still live: do not remove them as drive-by cleanup.** Deprecated aliases (the
  canonical name wins, an alias is never written) keep deployed settings, clients and persisted state working and are
  pinned by `apps/api/tests/test_legacy_aliases.py` and the guard's allow-list. Live identifiers (Foundry project
  names, APIM API ids and gateway paths, resource groups, the Cosmos database) are bound to the running deployment.
  The earlier name for each is in the [glossary](../glossary.md#naming-lineage). Source:
  [README](../../../README.md#configuration-reference).
- **The flagship department's files are not under its slug folder.** `claims-concierge` points at the top-level
  `data/seed/`, `data/golden/` and `agents/` so `azd up` never breaks; the other three departments live under
  `data/northwind/departments/<slug>/` (real paths: [`company.yaml`](../../../data/northwind/company.yaml)). Source:
  [data/northwind/README.md](../../../data/northwind/README.md#why-claims-concierge-points-at-top-level-paths).
- **The repository uses the live Foundry project names and still accepts the original flagship name.**
  `company.yaml`, the Bicep defaults, the reuse-mode check and the API default use `nw-insurance-claims` (and
  `nw-insurance-support`, `nw-insurance-documents`, `nw-insurance-contact-center`). The API,
  `scripts/deploy-hosted-agents.py` and `scripts/validate-reuse.py` also accept the original native project
  `northwind-orders`, and the hosted policy-wording agent accepts both knowledge connection names
  (`nw-insurance-claims-kb-mcp`, `northwind-insurance-kb-mcp`). The repository cannot verify which names exist in a
  tenant (see [ai-gateway.md](../../ai-gateway.md#live-inventory)), so do not drop an accepted alias as a drive-by
  cleanup. Sources: [glossary](../glossary.md#naming-lineage),
  [departments.md](../../departments.md#the-four-departments).
