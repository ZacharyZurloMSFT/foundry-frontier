# Decision records

> Status: current · Last reviewed: 2026-10-23

Short records of decisions whose "why" is hard to infer from code. One numbered file per decision. Do
not rewrite an accepted record: add a new one that supersedes it and update the status of the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-prompt-files-are-source-of-truth.md) | Prompt files are the source of truth | Accepted |
| [0002](0002-four-eval-tiers-and-department-baselines.md) | Four eval tiers, cheapest first, with per-department baselines | Accepted |
| [0003](0003-gateways-opt-in-fail-closed-two-tiers.md) | Gateways are opt-in, fail closed, and split by header handling | Accepted |

## When to write one

Write a record when a choice has a reason that code and the narrative docs do not capture, and a later
change could undo it by accident. Do not record routine implementation details.

## Template

```markdown
# NNNN — Title

> Status: Proposed | Accepted | Superseded by NNNN · Recorded: YYYY-MM-DD · Sources: where the decision
> was originally stated (doc, PR, issue)

## Context
What forces made this a decision.

## Decision
What we do, in a few bullets.

## Consequences
What this means for future changes, including what not to do.
```

Rules: cite where the decision was stated, never invent rationale (write "reason not recorded" if it is
unknown), and link to files instead of copying them.
