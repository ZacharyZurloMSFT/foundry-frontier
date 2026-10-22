# GitHub Copilot workspace instructions for foundry-frontier
#
# When you (Copilot) are working in this repo, always follow these rules.

## What this repo is
End-to-end Azure AI Foundry demo. Story: Northwind Insurance — a customer portal
with Microsoft Entra sign-in where policyholders see their own policies and
claims, adjusters work cases with a human in control, and identity-bound AI
assistants (Claims Concierge, Help Center) are embedded in the sections where
the work happens. There is no separate "AI workspace" and no legacy department
shell; the API is `/api/v1/*` plus `/health` and `/build`. One `azd up`
deploys everything.

## Golden rules
1. **Do not break `azd up`.** Any change to `infra/`, `azure.yaml`, or the
   `scripts/postprovision.*` files must leave a fresh deploy working end-to-end.
2. **Preserve the eval tiers that exist.** Never disable, weaken, or bypass an
   eval. Tier 1 (GitHub Models smoke), Tier 3 (red team against the hosted
   orchestrator) and the deterministic assistant golden eval stay green. The
   Tier 2 golden-set runner and the Tier 4 continuous eval were retired with
   the legacy `/chat`, `/hitl` and `/harness` endpoints they exercised; do not
   bring them back without a transport that exists. If a regression is
   legitimate, update `evals/baseline.json` in the same PR.
3. **Prompt files (`agents/*.yaml`, `.prompty`) are source of truth.** Do
   not fork them into the code. Changes go through the PR + optimizer loop.
4. **Every new capability must be visible in the UI or a GitHub artifact.**
   No invisible plumbing.

## When fixing an `eval-regression` or `redteam-failure` issue
- Read the report or trace payload attached to the issue.
- Compare the current prompt against the SHA stamped in the trace (`/build`
  reports the prompt SHAs the running API shipped).
- Prefer *prompt* fixes over *code* fixes. If a code fix is needed, add a
  golden-set row that would catch the same failure next time.
- Do not touch `evals/baseline.json` unless the reviewer explicitly approves
  the new numbers.

## Testing before opening a PR
- `ruff check apps/api evals pipelines data/seed`
- `python evals/smoke_eval.py --self-check`
- `python evals/assistant_eval.py --self-check`          (if assistants, help articles or `data/golden/assistant.jsonl` touched)
- `bicep build infra/main.bicep --stdout > /dev/null`  (if infra touched)
- `(cd apps/web-tech && npm run build)`                  (if web touched)

## Docs to read first for context
- `README.md`
- `docs/architecture.md`
- `docs/differentiators.md`   ← the sales pitch this demo defends
- `docs/genaiops.md`          ← how CI/CD/eval/observability fit together
- `docs/demo-script.md`       ← the 10-minute story arc
