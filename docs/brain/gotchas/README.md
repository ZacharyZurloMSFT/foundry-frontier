# Gotchas

> Status: current · Last reviewed: 2026-10-23 · Sources: cited on each page · Code wins: if an entry is
> wrong, fix it in the same PR.

Non-obvious traps, written as **trap, why, what to do**. They are split into one page per topic so each stays
short: read only the page that matches the work you are about to do.

| Page | Read it when you are... | Sections |
|---|---|---|
| [Evals and prompts](evals-and-prompts.md) | changing evals, baselines, prompts or CI gates | Evals and prompts |
| [Agents and approvals](agents-and-approvals.md) | working on agent tools, human approvals, the Copilot SDK harness or hosted agent versions | Approvals and agent tools; Hosted agent versions |
| [Deployment and releases](deployment-and-releases.md) | changing `infra/`, `azure.yaml`, the azd hooks or the release lane, merging a PR or releasing apps | `azd up` and deployment; Releases and merging |
| [Names and retired vocabulary](names-and-retired-vocabulary.md) | touching names, projects, agents or resources, or meeting an earlier demo name | Names and retired vocabulary |
| [API and gateways](api-and-gateways.md) | changing the API surface, gateway routing, the lock-down, keys or Foundry connections | API and gateways |
| [Local development](local-development.md) | running the stack locally or committing from Windows | Local development |

## Adding a gotcha

- Put it on the page whose task would have avoided the trap, under its section. Start a new page, and add a row
  above, only when none fits. Do not create new top-level notes.
- Keep it to a few lines and cite a source. Never include secrets or customer data: this repository is public.
- Keep each page at about 150 lines or fewer. When one outgrows that, split it by section, keep the section
  headings (other pages link to their anchors), and update this table and the inbound links.
