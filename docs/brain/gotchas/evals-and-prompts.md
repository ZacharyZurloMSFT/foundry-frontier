# Evals and prompts

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
changing evals, baselines, prompts or CI gates. Keep each entry short and cite a source.

- **Missing evidence is a failure, not a pass.** The golden runner reports `regression_status=unavailable` or a
  `baseline_error` and exits nonzero when the baseline is empty or malformed or no judge is configured; a gateway
  failure (401/403/404/429), disabled target or unscored attack is likewise an evaluation failure, never a skip.
  Never fabricate numbers, skip invalid entries, or edit `evals/baseline.json` to turn it green; a reviewer
  approves measured values (golden rule 2). The judge, Foundry safety evaluators, red team and the GitHub Models
  smoke tier call their services directly, not through the AI Gateway. Sources:
  [evals/README.md](../../../evals/README.md#copilot-sdk-harness),
  [evals/README.md](../../../evals/README.md#existing-golden-runner-corrections-and-offline-validation),
  [genaiops.md](../../genaiops.md#gateway-routing-and-the-tiers),
  [genaiops.md](../../genaiops.md#copilot-sdk-harness-evidence).
- **Judge model compatibility.** The golden runner's judge SDK is pinned (`azure-ai-evaluation==1.1.0`) and uses
  `max_tokens`; gpt-5/o-series names are rejected as judge. Set `FOUNDRY_JUDGE_DEPLOYMENT` to a verified
  chat-completions deployment. Source:
  [evals/README.md](../../../evals/README.md#existing-golden-runner-corrections-and-offline-validation).
- **`python evals/smoke_eval.py --self-check` needs `GITHUB_TOKEN`** and does not validate Azure readiness.
  Source: [evals/README.md](../../../evals/README.md#existing-golden-runner-corrections-and-offline-validation).
- **Offline tests are not live evidence.** The real-SDK harness contract tests make no Azure requests. Live eval
  jobs need private-network access to Foundry: use a connected runner, do not open private resources to make CI
  pass. Sources: [genaiops.md](../../genaiops.md#copilot-sdk-harness-evidence),
  [evals/README.md](../../../evals/README.md#copilot-sdk-harness).
- **Do not run cloud-write or billable commands unprompted.** Commands marked `CLOUD WRITE`, `MODEL CALLS` or
  `CLOUD UPLOAD + BILLABLE TRAINING` in `evals/README.md` need explicit operator approval and never belong in an
  API or UI request path. Source:
  [evals/README.md](../../../evals/README.md#orders-triage-learning-approvals-before-uploads-or-training).
- **Trace content capture stays off.** Enable full prompt, response or tool payload capture only for a controlled
  debugging session; those fields can contain customer data. Source:
  [genaiops.md](../../genaiops.md#generating-agent-traces-on-demand).
