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

- Four tiers run cheapest first (fail fast): smoke, golden set, red team, continuous sampling. Triggers are in
  the [glossary](../glossary.md#evals-and-delivery) and the workflow files.
- `evals/baseline.json` (schema v2) is keyed by department, each with its own golden dataset.
- If it is not in a workflow, it does not count: no hand-run evals as evidence
  ([genaiops.md](../../genaiops.md#where-this-diverges-from-typical-langchain-in-a-notebook-demos)).
- Missing evidence is a failure, not a pass. A gateway failure is an evaluation failure, and missing judge
  access, missing baselines, disabled targets and unscored attacks are not successful eval evidence
  ([genaiops.md](../../genaiops.md#gateway-routing-and-the-tiers)).
- Baseline numbers change only with explicit reviewer approval, in the same PR as the legitimate regression.

## Consequences

- Never disable, weaken or bypass an eval to get CI green.
- A new department needs a golden dataset (at least 10 rows) and its own baseline entry
  ([departments.md](../../departments.md#adding-a-new-department)).
- A new agent rubric needs a `{agent}_rubric.prompty`, an entry in the workflow set in `evals/run_evals.py`,
  and a first-run baseline refresh ([evals/README.md](../../../evals/README.md#custom-rubric-evaluators-llm-as-judge)).
- See [gotchas](../gotchas.md#evals-and-prompts) for the traps that follow from this.
