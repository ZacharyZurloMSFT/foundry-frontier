# GitHub Copilot workspace instructions for foundry-frontier
#
# When you (Copilot) are working in this repo, always follow these rules.

## What this repo is
End-to-end Azure AI Foundry demo. Story: Northwind Field Services — customer
voice front door → agent orchestrator → four prompt agents → HITL approval →
work order dispatched. One `azd up` deploys everything.

## Golden rules
1. **Do not break `azd up`.** Any change to `infra/`, `azure.yaml`, or the
   `scripts/postprovision.*` files must leave a fresh deploy working end-to-end.
2. **Preserve the four eval tiers.** Never disable, weaken, or bypass an eval.
   If a regression is legitimate, update `evals/baseline.json` in the same PR.
3. **Prompt files (`agents/*.yaml`, `.prompty`) are source of truth.** Do
   not fork them into the code. Changes go through the PR + optimizer loop.
4. **Every new capability must be visible in the UI or a GitHub artifact.**
   No invisible plumbing.

## When fixing an `eval-regression` or `redteam-failure` issue
- Read the trace payload attached to the issue.
- Compare the current prompt against the SHA stamped in the trace.
- Prefer *prompt* fixes over *code* fixes. If a code fix is needed, add a
  golden-set row that would catch the same failure next time.
- Do not touch `evals/baseline.json` unless the reviewer explicitly approves
  the new numbers.

## Testing before opening a PR
- `ruff check apps/api evals pipelines data/seed`
- `python evals/smoke_eval.py --self-check`
- `bicep build infra/main.bicep --stdout > /dev/null`  (if infra touched)
- `(cd apps/web-tech && npm run build)`                  (if web touched)

## Second brain (read on demand)
- Start at `docs/brain/README.md`; it maps each kind of task to the few pages worth reading (README,
  architecture, differentiators, genaiops and demo-script are routed from there). Do not read everything.
- Code wins: if a brain page or doc disagrees with the code, trust the code and fix the page in the same PR.
- Names changed over time (Field Services → Order Concierge → Northwind Insurance); check
  `docs/brain/glossary.md` before assuming what a name means.
- Learned something non-obvious (a trap, a "why")? Add it to the matching page under `docs/brain/gotchas/`
  (index: `docs/brain/gotchas/README.md`) or to a `docs/brain/decisions/` record. Do not create new top-level
  notes.
- Never put secrets, customer data, or internal-only data in the brain; this repo is public.
