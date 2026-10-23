# 0001 — Prompt files are the source of truth

> Status: Accepted · Recorded: 2026-10-22 (the decision pre-dates this record) · Sources:
> [`.github/copilot-instructions.md`](../../../.github/copilot-instructions.md) (golden rule 3),
> [genaiops.md](../../genaiops.md#prompt-as-code),
> [`prompt-registry.yml`](../../../.github/workflows/prompt-registry.yml)

## Context

Prompts, tools, retrieval configs, model routes and safety policies ship, evolve and regress together
([genaiops.md](../../genaiops.md)). Production traces need a trace-back link from a running prompt to the
commit that shipped it.

## Decision

- Every `.prompty` file and `agent.yaml` under `agents/` is the source of truth. Prompt text is not forked
  into application code.
- On a push to `main` that touches `agents/**` or `evals/evaluators/rubrics/**`, `prompt-registry.yml` runs
  `scripts/publish-prompts.py`, which uploads each prompt to the Foundry Prompt Store tagged with the git SHA.
- Prompt changes go through a pull request and the optimizer loop, like any other change.

## Consequences

- Prefer prompt fixes over code fixes when handling an `eval-regression` or `redteam-failure` issue. If a code
  fix is needed, add a golden-set row that would catch the same failure next time.
- Runtime code loads the packaged canonical YAML (the hosted manager loads
  `agents/orchestrator/instructions.yaml`) and reports the canonical prompt SHA-256 in response metadata
  ([architecture.md](../../architecture.md#order-concierge-hosted-runtime)).
- Link to prompt files from the brain; never paste their text here.
