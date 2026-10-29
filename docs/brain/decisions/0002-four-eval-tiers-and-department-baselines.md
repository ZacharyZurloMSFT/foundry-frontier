# 0002 — Four eval tiers, cheapest first, with per-department baselines

> Status: Accepted · Recorded: 2026-10-22 (the decision pre-dates this record) · Sources:
> [`.github/copilot-instructions.md`](../../../.github/copilot-instructions.md) (golden rule 2),
> [genaiops.md](../../genaiops.md#the-four-eval-tiers),
> [architecture.md](../../architecture.md#evaluation-baselines),
> [evals/README.md](../../../evals/README.md)

## Context

Agent behavior regresses in ways unit tests do not catch, and each Foundry evaluation spends Azure quota.
Departments share one account, so one department's numbers must not mask another's.

## Decision

- Four tiers, ordered by cost: smoke, golden set, red team, continuous sampling. Tiers 1 and 2 are independent
  workflows triggered by the same pull requests, so nothing makes tier 2 wait for tier 1. Triggers are in the
  [glossary](../glossary.md#evals-and-delivery) and the workflow files.
- `evals/baseline.json` (schema v2) is keyed by department slug, each department with its own golden dataset.
- Evals are code: evaluators, rubrics and golden rows live next to the agents they score, so a prompt change and its
  regression test ship together
  ([genaiops.md](../../genaiops.md#where-this-diverges-from-typical-langchain-in-a-notebook-demos)).
- Missing evidence is a failure, not a pass. In the eval runner and the harness paths a gateway failure is an
  evaluation failure, and missing judge access, missing baselines, disabled targets and unscored attacks are not
  successful eval evidence ([genaiops.md](../../genaiops.md#gateway-routing-and-the-tiers)). The hosted red-team
  lane now fails closed and remains blocked pending verified cloud agentic support
  ([gotchas](../gotchas/evals-and-prompts.md)).
- Baseline numbers change only with explicit reviewer approval, in the same PR as the legitimate regression.

## Consequences

- Never disable, weaken or bypass an eval to get CI green.
- A new department needs a golden dataset (at least 10 rows) and, once a reviewer approves measured numbers, its own
  baseline slot ([departments.md](../../departments.md#adding-a-new-department)).
- A new judge rubric needs `evals/evaluators/rubrics/{workflow}_rubric.prompty`, the workflow name added to
  `RUBRIC_WORKFLOWS` in `evals/run_evals.py`, and baseline metrics only after a reviewer approves the first measured
  numbers ([evals/README.md](../../../evals/README.md#evaluators)).
- See [gotchas](../gotchas/evals-and-prompts.md) for the traps that follow from this.
