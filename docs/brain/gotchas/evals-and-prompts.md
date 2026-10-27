# Evals and prompts

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
changing evals, baselines, prompts or CI gates. Keep each entry short and cite a source.

- **Missing evidence is a failure, not a pass.** `evals/run_evals.py` exits nonzero (`2` for incomplete evidence)
  with `regression_status=unavailable` or a `baseline_error` when the baseline is empty or malformed or no judge is
  configured, and exits `1` for a failed row, gateway failures (401/403/404/429) included. Never fabricate numbers,
  skip invalid entries, or edit `evals/baseline.json` to turn it green; a reviewer approves measured values (golden
  rule 2). One known gap: if the hosted red-team target's SDK or target fails, `pipelines/red-team/run_redteam.py`
  writes a stub report and exits `0` (only the `copilot-sdk` target is strict). The judge, Foundry safety evaluators,
  red team and GitHub Models smoke tier bypass the AI Gateway. Sources:
  [evals/README.md](../../../evals/README.md#results-exit-codes-and-the-baseline),
  [genaiops.md](../../genaiops.md#gateway-routing-and-the-tiers),
  [genaiops.md](../../genaiops.md#what-is-and-is-not-enforced-today).
- **Judge model compatibility.** The judge SDK is pinned (`azure-ai-evaluation==1.1.0`) and sends `max_tokens`, which
  gpt-5.x deployments reject, so names starting `gpt-5`, `o1`, `o3` or `o4` leave the judge unavailable. Set
  `FOUNDRY_JUDGE_DEPLOYMENT` to a verified chat-completions deployment. Sources:
  [evals/README.md](../../../evals/README.md#results-exit-codes-and-the-baseline),
  [`rubric_evaluator.py`](../../../evals/evaluators/rubric_evaluator.py).
- **`python evals/smoke_eval.py --self-check` needs `GITHUB_TOKEN`** (or `GH_TOKEN`); it checks the token and that
  every case and prompt file loads, not Azure readiness. Source:
  [evals/README.md](../../../evals/README.md#tier-1--smoke-eval).
- **Offline tests are not live evidence.** The real-SDK harness contract tests make no Azure requests. Live eval jobs
  run on GitHub-hosted runners against the API's public ingress (no private-network access needed); Cosmos DB stays
  private, so Cosmos-backed routes only work against a deployed API. Sources:
  [genaiops.md](../../genaiops.md#copilot-sdk-harness-evidence),
  [evals/README.md](../../../evals/README.md#copilot-sdk-harness).
- **Paid or cloud-writing evaluation runs need an approval receipt.** `scripts/run-insurance-quality-gates.py` only
  prints a credential-free plan unless given `--execute` with `--approval-receipt`, `--confirm` and a new
  `--run-receipt` (an explicit paid native evaluation). Never generate a consent receipt, never run it unprompted, and
  keep it out of any API or UI request path. Source:
  [evals/README.md](../../../evals/README.md#insurance-evaluation-packs-gated).
- **Trace content capture stays off.** Enable full prompt, response or tool payload capture only for a controlled
  debugging session; those fields can contain policyholder data. Source:
  [genaiops.md](../../genaiops.md#generating-agent-traces-on-demand).
- **Keep prompt and source files LF.** [`.editorconfig`](../../../.editorconfig) sets `end_of_line = lf` and there is
  no `.gitattributes`. Hosted agents report the SHA-256 of a prompt file's raw bytes (`prompt_sha256`), and
  `scripts/deploy-hosted-agents.py` reuses a version only if the SHA-256 of the zipped bytes matches, so a CRLF
  checkout (for example `core.autocrlf=true` on Windows) breaks the trace-to-prompt comparison of the
  `eval-regression` flow. Sources: [`.github/copilot-instructions.md`](../../../.github/copilot-instructions.md),
  [genaiops.md](../../genaiops.md#prompt-as-code).
