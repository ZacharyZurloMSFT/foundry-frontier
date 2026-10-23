# GitHub Copilot workspace instructions for foundry-frontier
#
# When you (Copilot) are working in this repo, always follow these rules.

## What this repo is
End-to-end Azure AI Foundry demo. Story: Northwind Insurance, a fictional
property and casualty insurer. A policyholder reaches the voice front door, the
claims concierge orchestrator hands the work to four hosted agents (triage,
policy and claim lookup, policy wording, claim-action draft), and a person
approves the drafted claim action. Adjuster support, claim-document
understanding and the Copilot SDK harness sit beside it. All data is synthetic.
`azd` and the release workflows deploy five apps: `api`, `mcp-policy-lookup`,
`mcp-claim-review`, `web-portal` and `web-voice` (the live `ca-insurance-*`
Container Apps).

## Golden rules
1. **Do not break the deploy path.** Any change to `infra/`, `azure.yaml`,
   `scripts/validate-reuse.py` (the azd preprovision/predeploy hook),
   `scripts/release-app.py` or the `deploy-*.yml` workflows must leave a fresh
   `azd provision`/`azd deploy` and the app release lane working. Service names
   must stay in step across `azure.yaml`, `release-app.py` and the workflows.
   Never rename or delete live Azure resources from code.
2. **Preserve the four eval tiers.** Never disable, weaken, or bypass an eval.
   If a regression is legitimate, update `evals/baseline.json` in the same PR.
3. **Prompt files (`agents/insurance/*.yaml`, `data/northwind/departments/*/prompts/*.yaml`,
   `.prompty`) are source of truth.** Do not fork them into the code. Changes go
   through the PR + optimizer loop.
4. **Every new capability must be visible in the UI or a GitHub artifact.**
   No invisible plumbing.
5. **Insurance only.** Do not add retail, orders, field-service, HR/IT or model
   bake-off features, routes, data or wording. The retired names survive only as
   documented deprecated aliases and live Azure identifiers (see
   `docs/legacy-decommission.md`); `scripts/tests/test_no_legacy_vocabulary.py`
   enforces the allow-list.
6. **Agents draft, people decide.** Claim actions are drafts for human review.
   Never add a path that approves, denies or pays a claim without a person.

## When fixing an `eval-regression` or `redteam-failure` issue
- Read the trace payload attached to the issue.
- Compare the current prompt against the SHA stamped in the trace.
- Prefer *prompt* fixes over *code* fixes. If a code fix is needed, add a
  golden-set row that would catch the same failure next time.
- Do not touch `evals/baseline.json` unless the reviewer explicitly approves
  the new numbers.

## Testing before opening a PR
- `ruff check apps/api evals pipelines data/seed`  (`apps/api` has known E402/F401/F821 findings: add none)
- `python evals/smoke_eval.py --self-check`
- `python data/seed/generate.py --domain insurance`, then `python -m pytest apps/api/tests` and the
  `scripts/tests` files listed in `.github/workflows/ci.yml`
- `python -m pytest agents/tests evals/tests`  (after `pip install -r agents/hosted/requirements.txt -r evals/requirements.txt`)
- `python scripts/export-openapi.py --check`            (if API routes or docstrings changed)
- `bicep build infra/main.bicep --stdout > /dev/null`  (if infra touched)
- `(cd apps/web-portal && npm run build)`               (if web touched)

## Second brain (read on demand)
- Start at `docs/brain/README.md`; it maps each kind of task to the few pages worth reading (README,
  architecture, differentiators, genaiops and demo-script are routed from there). Do not read everything.
- Code wins: if a brain page or doc disagrees with the code, trust the code and fix the page in the same PR.
- Names changed over time and the earlier demo names survive only as deprecated aliases and live Azure
  identifiers; check `docs/brain/glossary.md` before assuming what a name means, and read
  `docs/legacy-decommission.md` for what was retired and how the old stack is wound down.
- Learned something non-obvious (a trap, a "why")? Add it to the matching page under `docs/brain/gotchas/`
  (index: `docs/brain/gotchas/README.md`) or to a `docs/brain/decisions/` record. Do not create new top-level
  notes.
- Never put secrets, customer data, or internal-only data in the brain; this repo is public.
