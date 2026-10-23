# Second brain

> Status: current · Last reviewed: 2026-10-23

Project memory for contributors and coding agents: the context that is hard to get from reading code alone.
It is small on purpose. Read only the row you need; do not read everything.

## Start here

| If you are... | Read first | Then, if needed |
|---|---|---|
| New to the repo | [README](../../README.md#repository-layout), [glossary](glossary.md) | [architecture](../architecture.md) |
| Unsure which name is current (Field Services, Order Concierge, Insurance) | [glossary: naming lineage](glossary.md#naming-lineage) | [departments](../departments.md) |
| Changing evals, baselines, prompts or CI gates | [ADR 0001](decisions/0001-prompt-files-are-source-of-truth.md), [ADR 0002](decisions/0002-four-eval-tiers-and-department-baselines.md), [gotchas: evals](gotchas.md#evals-and-prompts) | [genaiops](../genaiops.md), [evals/README](../../evals/README.md) |
| Changing `infra/`, `azure.yaml` or `scripts/postprovision.*` | [gotchas: azd up](gotchas.md#azd-up-and-deployment) | [architecture](../architecture.md) (Networking, Regions, Identity) |
| Changing the API surface or gateway routing | [gotchas: API and gateways](gotchas.md#api-and-gateways), [ADR 0003](decisions/0003-gateways-opt-in-fail-closed-two-tiers.md), [ADR 0005](decisions/0005-mcp-servers-are-plain-http-passthrough-apis.md) | [api-management](../api-management.md), [ai-gateway](../ai-gateway.md) |
| Picking up the gateway cut-over (what is live, what is left, how to roll back) | [api-management](../api-management.md) (Status block at the top), [Remaining steps](../api-management.md#remaining-steps), [Rollback](../api-management.md#rollback) | [Insurance stack cut-over](../api-management.md#insurance-stack-cut-over-2026-10-23), [gotchas: hosted agent versions](gotchas.md#hosted-agent-versions) |
| Setting up GitHub OIDC for the pipelines, or the repository goes private or public | [ADR 0004](decisions/0004-oidc-trust-on-a-public-repository-is-an-explicit-opt-in.md) | [api-management: repository visibility](../api-management.md#repository-visibility) |
| Merging a PR or releasing apps | [gotchas: releases and merging](gotchas.md#releases-and-merging) | [`deploy-apps.yml`](../../.github/workflows/deploy-apps.yml) |
| Adding or changing a department | [departments: adding a department](../departments.md#adding-a-new-department) | [glossary](glossary.md) |
| Working on agents, HITL or the Copilot SDK harness | [gotchas: approvals](gotchas.md#approvals-and-agent-tools), [architecture: harness](../architecture.md#copilot-sdk-agent-harness) | [evals/README: harness](../../evals/README.md#copilot-sdk-harness) |
| Running locally | [local-dev](../local-dev.md) | [gotchas: local development](gotchas.md#local-development) |
| Presenting or defending the story | [differentiators](../differentiators.md), [demo-script](../demo-script.md) | [genaiops](../genaiops.md) |
| Asking "why was this decided?" | [decisions](decisions/README.md) | |

## Which file wins

When a doc and a file disagree, trust the file and fix the doc in the same PR.

| Question | Source of truth |
|---|---|
| When does a workflow run? | `.github/workflows/*.yml` |
| Which paths, project and index does a department use? | [`data/northwind/company.yaml`](../../data/northwind/company.yaml) |
| What do the azd services and hooks do? | [`azure.yaml`](../../azure.yaml), `scripts/postprovision.*` |
| What is the gateway-visible API surface? | `infra/live/apim/specs/northwind-api.openapi.json` (generated from the app) |
| What does a prompt say? | The prompt file itself, never a copy ([ADR 0001](decisions/0001-prompt-files-are-source-of-truth.md)) |
| What is live right now (images, guard state, hosted agent versions)? | Azure itself: `az containerapp list -g rg-northwind-apps` and `python scripts/apim-smoke.py --gateway-url https://apim-northwind.azure-api.net --stack <stack> --expect-lockdown`. [api-management.md](../api-management.md) is a dated snapshot, so re-check it before relying on it. |

## Conventions

- One topic per page, short (about 150 lines at most).
- Start with a status line: `> Status: current · Last reviewed: YYYY-MM-DD · Sources: ...`. Update the date
  when you re-verify a page.
- Standard relative Markdown links only; no Obsidian `[[wikilinks]]` or plugin syntax.
- Link to code and docs instead of copying them, and cite a source for every fact. Never invent rationale.
- Record durable facts, not live status ("what is deployed today"); link to the doc that tracks status.
- Learned something non-obvious? Add it to [gotchas](gotchas.md) or a [decision record](decisions/README.md)
  rather than creating a new top-level note.

## What does not belong here

- Secrets, credentials, tokens or keys.
- Customer or sales data, other internal-only information, and personal notes. This repository is public
  (see [api-management: repository visibility](../api-management.md#repository-visibility)), so treat
  everything here as world-readable. Personal notes belong in `~/.copilot/` instructions or skills, or a
  private repository.
- Anything you can read straight from the code (file trees, function lists).
- Pasted web or issue content.

## Pages

- [glossary.md](glossary.md): naming lineage and terms that are easy to misread.
- [gotchas.md](gotchas.md): non-obvious traps, as trap, why, what to do.
- [decisions/](decisions/README.md): why we chose what we chose.
