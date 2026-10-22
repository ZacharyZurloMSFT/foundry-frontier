# Northwind Copilot — Evaluation

We use **Foundry AI Evaluation** to score every change against a golden dataset
and a subset of live production traffic. The evaluators run in two places:

1. **Pull-request gate** — `.github/workflows/evals-on-pr.yml` runs the full
   golden set on every PR that touches `apps/api`, `agents/`, or `evals/`, and
   blocks merge if any evaluator regresses vs the current `main` baseline.
2. **Continuous evaluation** — `.github/workflows/continuous-eval.yml` runs
   nightly, samples ~5% of production traces from App Insights (`AI-conversation`
   custom dimension), and re-scores them with the same evaluators. Trends land
   in the Foundry Observability dashboard.

> **AI Gateway:** the evaluator judge model and Foundry safety/red-team
> services are **not** routed through the AI Gateway (out of scope, see
> [`docs/ai-gateway.md`](../docs/ai-gateway.md)). Only the deployed app and
> agents under test are, so PR evals measure the real gateway path.

## Evaluators used

| Evaluator                 | Kind      | Scope                                           |
| ------------------------- | --------- | ----------------------------------------------- |
| `TaskAdherence`           | built-in  | All workflows                                    |
| `Groundedness`            | built-in  | `manual_retrieval`                               |
| `Coherence`               | built-in  | All workflows                                    |
| `HateUnfairness`          | built-in  | Safety — customer voice front door               |
| `Violence` / `SelfHarm`   | built-in  | Safety                                           |
| `WorkOrderCompleteness`   | **custom** (deterministic) | `work_order_draft` / `resolution_draft` — checks persisted resolution fields |
| `TriageRubric`            | **custom** (LLM-as-judge) | `triage` — `.prompty` rubric, 1–5 score |

### Custom rubric evaluators (LLM-as-judge)

`evals/evaluators/rubric_evaluator.py` defines `RubricEvaluator`, a reusable
class that runs any `.prompty` rubric file against a judge model
(`FOUNDRY_JUDGE_DEPLOYMENT`, falling back to compatible configured
`FOUNDRY_PROMPT_AGENT_DEPLOYMENT` / `FOUNDRY_WORKER_DEPLOYMENT`) and
returns a normalized `{<name>_score: 1–5, <name>_reason: str, <name>_pass: bool}`
triple. It plugs into `azure.ai.evaluation.evaluate()` batch runs the same way
built-in evaluators do (implements `_aggregate_results`).

Per-agent rubrics live in `evals/evaluators/rubrics/{agent}_rubric.prompty`.
Today only `triage_rubric.prompty` exists; add a rubric for a new agent by:

1. Copy `triage_rubric.prompty` to `{agent}_rubric.prompty` and rewrite the
   system prompt + scoring criteria for that agent's job.
2. Add the workflow name to the `for wf in {"triage"}:` set in
   `run_evals.py::main`.
3. Refresh `baseline.json` on the first run so the regression gate has a
   reference for `{agent}_rubric_score` / `{agent}_rubric_pass`.

### Deterministic custom evaluator

The custom `WorkOrderCompleteness` evaluator retains its legacy module/metric
names in `evals/evaluators/work_order_completeness.py`. It validates the seven
resolution fields (`order_id`, `customer_id`, `resolution_type`, `amount_usd`,
`reason`, `policy_ref`, `urgency`), allowed type/urgency vocabularies and finite
numeric amount (0 ≤ x ≤ 10,000). Zero is valid; booleans are not amounts.

## Copilot SDK harness

`data\golden\harness.jsonl` covers order lookup, policy retrieval, a real saved
resolution, missing data, denied coding tools, forged approval, and cross-run
references. Run it against an enabled, deployed candidate:

```powershell
python evals\run_evals.py --target copilot-sdk --dataset data\golden\harness.jsonl --output evals\harness-results.json --baseline evals\baseline.json
```

CI additionally supplies `--expected-build` to reject stale deployments. The
harness contract grades structured persisted draft fields and actual events,
not a sentence claiming a refund succeeded. Judge errors fail the run.

The current baseline has empty department metrics. A reviewer must approve
establishing `aggregates.order-concierge.copilot-sdk` metrics before regression
comparison can be certified. Until then the harness report explicitly says
`regression_status=unavailable` and the golden runner exits nonzero. No numbers
are fabricated and this feature does not modify `baseline.json`.

Tier 3 uses `--agent copilot-sdk` with `API_URI` to call the HTTP target. Unknown
or missing attack scores fail. Tool-context injection is explicitly unsupported
by this HTTP adapter and is rejected rather than discarded. Direct denial and
workflow-binding cases also have deterministic contract coverage.

For offline coverage, install the API and resolution-MCP requirements, stage
the pinned runtime with `python -m copilot download-runtime`, then run:

```powershell
$env:TEST_COPILOT_RUNTIME = "1"
python -m pytest apps\api\tests evals\tests\test_harness_eval.py -q
```

The real-SDK contract uses a local deterministic model server, exercises the
actual JSON-RPC/model/tool loop and token callbacks, and makes no Azure model
requests. It is not a substitute for live managed-identity, networking, and
deployment checks.

## Running locally

```bash
cd evals
pip install -r requirements.txt
python run_evals.py --dataset ../data/golden/dataset.jsonl --output ./results.json
```

Set `FOUNDRY_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, and `FOUNDRY_WORKER_DEPLOYMENT`
in your environment first (they're already in `.azure/<env>/.env` after `azd up`).

## Orders triage learning: approvals before uploads or training

### Manual-upload dataset pack

`pipelines\orders-learning\build_upload_pack.py` prepares a local, synthetic
manual-upload pack without calling Azure, approving labels, or starting jobs:

```powershell
.\.venv\Scripts\python.exe pipelines\orders-learning\build_upload_pack.py --output "<new-local-output-folder>"
```

The pack contains 200 triage training examples, 50 separate validation examples,
100 frozen-test inputs/references, and input/reference evaluations for order
lookup (50), policy lookup (60), resolution drafting (30), and orchestration
(40), plus 250 training/validation suggestions for native annotation review.
That review file excludes the frozen test. The fine-tuning files use
conversational JSONL with UTF-8 BOM. Evaluation
files have `query` and `ground_truth`; they intentionally have no `response`
because no model was run. Generated agent output must be mapped separately.

All labels remain **suggested and unreviewed**. Five paraphrases of each scenario
stay in one split; each split uses separate scenario families and identifiers.
The `review-only` folder is a compatible curation workspace with original
examples and an empty review ledger. The pack is not an approved release, and
must not be substituted for the immutable reviewed release used by the training
CLI. `UPLOAD-GUIDE.txt` and `manifest.json` describe project placement, source
lineage and checksums. Uploading the pack does not resolve the account-default
constraint or authorise training charges.

Resolution evaluations and some orchestration examples can create pending
drafts. Do not run those against operational tools/queues: use isolated test
tools/storage and supply a unique trusted workflow ID outside model text.
Fixture lookup expectations require refreshing if demo data changes. Policy
expectations come from the authored seed contract and require comparison with
the indexed knowledge before approval.

`scripts\orders-learning.py` is a **local operator CLI**, not an HTTP training
endpoint. Its offline commands neither load `.env` files nor acquire credentials.
The intended destination is the existing account `aisvc-zurlo-northwind`, project
`northwind-orders`; creating the project and making it the account default are
separate infrastructure operations. This pipeline does not perform them.

The installed API contract was checked against `azure-ai-projects==2.3.0`,
`azure-identity==1.19.0`, and `openai==2.46.0`. Live commands exclusively use
`AzureCliCredential`, so the operator must deliberately sign in with their own
Azure identity. There is no key-based or backend managed-identity fallback.

### Data contract and actual state

The 44 authored starter examples cover seven intents, entity extraction/missing
entities, four urgency levels, angry escalation, fraud, safety, legal risk and
instruction injection. **Every starter row is unreviewed.** Suggested labels are
not human approvals. This small corpus is useful for testing the process, not
evidence that a fine-tune will outperform the base model. Grow reviewed coverage
before paying for a training experiment.

Each source JSONL row contains:

| Field | Meaning |
|---|---|
| `example_id` | Immutable unique identifier; changed source content gets a new ID |
| `family` | Shared scenario/paraphrase group, never split across train/test |
| `input` | Synthetic or privacy-reviewed customer request |
| `expected` | `order_id`, `customer_hint`, `intent`, `sentiment`, `urgency`, `problem_summary` |
| `review_status`, `reviewer` | Source is always `unreviewed`, `null`; reviewed state comes from the ledger |
| `provenance` | `kind`, `source`, source/trace/conversation group; explicit `contains_dynamic_order_truth:false`, `contains_policy_text:false` |
| `split` | `train`, `validation`, or `test`, assigned at scenario-group level |

Non-synthetic imports also require `provenance.pii_reviewed:true`. Redact actual
identifiers before import; this boolean is a human attestation, not a PII scanner.
Do not train current delivery state, order amounts, refund eligibility, policies,
or retrieved documents into weights. Those remain live tools/RAG. Only stable
triage extraction belongs here. Training instructions are read from the existing
`agents\triage.yaml`, not a forked prompt.

The default workspace is `pipelines\orders-learning\workspace`. All commands also
accept `--workspace <path>` **before** the subcommand:

```powershell
$py = ".\.venv\Scripts\python.exe"
# First use only; init refuses to replace an existing examples.jsonl.
& $py scripts\orders-learning.py init
& $py scripts\orders-learning.py status
& $py scripts\orders-learning.py status --output pipelines\orders-learning\workspace\status.json
```

`status` is the read-only UI integration contract (`schema_version:1`):
`account`, `project`, `project_endpoint`, `counts` (unreviewed/accepted/rejected
and accepted rows per split), `review_revisions`, `dataset`,
`evaluation`, `training`, `training_placement`, `staging`, `native_annotations`, `blocked_reasons`.
It reports `evaluation.state=not_run`, `metrics=null`, and
`training.state=not_submitted` until actual artifacts are supplied. It never
invents a deployment, human annotation, baseline, metric, or job. Use
`--manifest`, `--evaluation`, and `--training` to select matching artifacts;
mismatched manifest hashes fail. `status --output` updates only a local snapshot.
Parent API/UI integrations should serve this snapshot read-only, never shell out
to a mutation command on behalf of a public caller.

`training_placement` exposes `state`, `requested_project`,
`actual_default_project` (null until a live read), and the routing requirement.
Before training it remains `not_verified`; a blocked submission receipt exposes
`state=blocked` and the actual ARM default project. Pass that receipt with
`status --manifest <manifest> --training <receipt>` to surface the reason in the
UI. Status itself performs no cloud calls and never treats an accepted ARM PATCH
as proof that the default actually changed.

The checked-in workspace also contains `default-promotion.json`: an explicitly
**operator-reported**, blocked promotion after accepted PATCH requests using
`2025-06-01` and `2026-09-01` did not change bounded readback. `status` includes it
as `default_project_promotion`; it is not live verification or training consent.
Preserve the existing Claims default/account. A later operator must update that
report only after verified resolution; the training CLI still performs its own
mandatory live routing checks.

### Native human review and append-only annotations

In [Foundry](https://ai.azure.com), select **northwind-orders** under
**aisvc-zurlo-northwind**, not an older project. Use native Human Evaluation for
review batches and native trace annotation controls when reviewing a trace.
The installed Projects 2.3.0 SDK has no verified `annotations` operation group;
this implementation deliberately does **not** invent one or claim that local
JSON appeared in the Foundry dashboard.

Manual operator steps:

1. Open the relevant evaluation row or trace/span and record its native
   evaluation/trace/span identifiers. Assign a human reviewer.
2. Use the native rating/comment controls offered in that project. Record the
   accept/reject decision, corrected six-field triage JSON, reviewer and reason.
   If custom label controls are unavailable, put corrections in the comment.
3. Add a new annotation/revision rather than overwriting history. Preserve the
   prior native annotation reference and correction rationale.
4. Export/download the reviewed rows where the portal supports it; otherwise
   manually transcribe a bounded set into the canonical contract above. Keep
   their native identifiers in provenance, perform PII review, and set source
   rows to **unreviewed**. There is no automatic conversion of a rating into
   training approval.
5. Import and explicitly accept/reject each example locally, preserving the
   native reference. The local ledger is distinct from native storage:

```powershell
& $py scripts\orders-learning.py import --source .\reviewed-source-export.jsonl
& $py scripts\orders-learning.py review --example-id synthetic-tracking-01 `
  --decision accepted --reviewer "<human identity>" --reason "<review rationale>" `
  --confirm-reviewed
# corrected-labels.json must contain the complete six-field expected object.
& $py scripts\orders-learning.py review --example-id synthetic-tracking-01 `
  --decision accepted --reviewer "<human identity>" --reason "<correction rationale>" `
  --corrected-labels .\corrected-labels.json --annotation-ref "<native trace/annotation reference>" `
  --confirm-reviewed
```

`reviews.jsonl` is append-only with per-example version, timestamp, corrected
labels, reviewer, reason, native annotation reference, source hash, previous
record hash and record hash. Hash checks detect accidental modification but are
**not digital signatures** or a replacement for repository/access controls.
Concurrent local writes are locked. If a process dies with `.curation.lock`,
inspect the last complete ledger record before manually removing that lock.

### Versioned accepted-only exports and frozen test

Review the intended holdout fully before the first export. Only accepted rows
are exported. Shared family, normalized duplicate input, order/customer
identifiers and trace/conversation/source groups cannot cross splits. A
persistent registry rejects historical split moves. Human reviewers must still
identify semantic near-duplicates that string/entity matching cannot detect.
The first exported test set is frozen by SHA256. Changed test labels, membership
or review revisions block later exports; a genuinely new benchmark requires a
separate explicitly reviewed workspace and new baseline, not silently unfreezing
the old test.

```powershell
& $py scripts\orders-learning.py export --version v1
$manifest = "pipelines\orders-learning\workspace\releases\v1\manifest.json"
$manifestHash = (Get-FileHash $manifest -Algorithm SHA256).Hash.ToLower()
# Separate dataset-level human sign-off after inspecting the release:
& $py scripts\orders-learning.py approve-dataset --manifest $manifest `
  --reviewer "<dataset owner>" --confirm $manifestHash
```

Each immutable release contains `manifest.json`, accepted examples and review
history, the original `sources.jsonl` snapshot, exact triage instructions, OpenAI conversational `train.jsonl` and
`validation.jsonl` (UTF-8 with BOM), and canonical `test.jsonl`. Hashes, sizes,
counts and source/prompt/review lineage are verified before subsequent use.
Verification reconstructs the accepted set from the complete, hash-chained
ledger and source snapshot. Its head, latest decisions, corrected labels,
reviewers, revisions and review hashes must match exactly; matching file hashes
alone cannot authorize a release.
With every starter row accepted, counts would be 24/8/12; currently that approval
has **not** happened. The training CLI requires at least ten training rows.
It never uploads the frozen test as a fine-tuning file.

Native versioned datasets use the verified
`AIProjectClient.datasets.get/upload_file(name, version, file_path)` APIs.
Publication plans are offline; an approved explicit upload is a separate step:

```powershell
& $py scripts\orders-learning.py publish --manifest $manifest --output .\dataset-publication-plan.json
# CLOUD WRITE: run only after the operator approves uploading this exact dataset.
& $py scripts\orders-learning.py publish --manifest $manifest --output .\dataset-publication-receipt.json `
  --execute --confirm $manifestHash
```

Three distinct native dataset names end in `-train`, `-validation`, and `-test`.
Native dataset publication and native evaluations are explicitly project-scoped
and can use nondefault `northwind-orders`; they do not require switching the
account default. Fine-tuning uses an account-scoped API and has a separate,
mandatory routing gate below.
Existing native versions are refused; use a new version rather than overwrite.
Only one publisher should operate at a time: the service upload itself is not an
atomic create-if-absent transaction. Interrupted receipts must be reconciled in
Foundry before retrying. Native datasets and fine-tuning `purpose=fine-tune`
files are different service resources; publishing a dataset does not train it.

### Evaluate genuine outputs, not fabricated baselines

Saved prediction JSONL is one `{"example_id":"...", "response":"<raw model text>"}`
per frozen-test row, **plus mandatory recorded provenance** on every row:
`deployment`, `prediction_run_id`, `manifest_sha256`, `dataset_version`,
`frozen_test_sha256`, and `prompt_sha256`. `predict_live` writes these fields
automatically and records provider response ID/model when available. Imported
predictions must retain their original capture provenance; `--deployment`
cannot relabel a baseline response as a never-invoked candidate. The prompt hash
here is the released instruction text, not the hosted runtime's raw-YAML hash.
Optional `usage` and `error` fields preserve real inference results.
Do not fill this file with expected labels. Missing/duplicate/unknown
IDs, malformed JSON, extra fields, errors and urgent misclassification cannot
silently pass. Reports contain row outputs, actual computed metrics, denominator
counts, hashes and gates. Metrics cover strict six-field JSON structure, intent,
entity exact match, sentiment, exact urgency, urgent recall and P1 recall.
Problem-summary wording is not exact-matched; a human/judge must assess its
semantic quality through the other eval tiers.

```powershell
& $py scripts\orders-learning.py evaluate --manifest $manifest `
  --predictions .\base-predictions.jsonl --deployment "<base deployment>" --output .\base-eval.json
& $py scripts\orders-learning.py evaluate --manifest $manifest `
  --predictions .\candidate-predictions.jsonl --deployment northwind-orders-triage-ft-v1 `
  --baseline .\base-eval.json --output .\candidate-eval.json
# MODEL CALLS: optional explicit inference, never run automatically.
& $py scripts\orders-learning.py evaluate --manifest $manifest --execute-live --confirm $manifestHash `
  --predictions .\new-live-predictions.jsonl --deployment "<deployed model>" --output .\new-live-eval.json
```

The first measured run exits **2** (`regression_status=unavailable`) if its
absolute gates pass but no comparison exists. A measured candidate exits **0**
only with matching frozen test, all gates and no metric regression >0.05.
Both runs must also bind the same release and prompt, identify distinct recorded
deployments, and have different prediction hashes and inference-run IDs.
Reusing baseline predictions does not constitute candidate evaluation. These
local provenance records detect mismatches/reuse, not deliberate forgery by an
operator who can rewrite all artifacts; they are not service-signed attestations.
It exits **1** for failed gates/incomplete responses. No baseline file is
auto-populated and no evaluation result automatically approves deployment.

Supplemental native evaluations use documented
`project.get_openai_client().evals.create` and `.evals.runs.create`, with `custom`
data schemas, `string_check` graders and inline JSONL saved predictions:

```powershell
& $py scripts\orders-learning.py native-evaluate --manifest $manifest `
  --predictions .\candidate-predictions.jsonl --output .\native-eval-plan.json
# CLOUD WRITE/JOB: explicit approved upload of actual predictions.
& $py scripts\orders-learning.py native-evaluate --manifest $manifest `
  --predictions .\candidate-predictions.jsonl --output .\native-eval-receipt.json `
  --execute --confirm $manifestHash
```

The receipt contains real eval/run IDs and the service report URL, not invented
success metrics. Inspect completion in Foundry; creation is not completion.
Native string graders supplement, not replace, local schema/urgent-denominator
tests or the four established eval tiers. API shapes are based on the
[official Projects evaluation samples](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/evaluations)
and [dataset samples](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/datasets).

### Budget, admission and actual training

`gpt-4.1-mini` version `2025-04-14` is the initial **candidate**, not a guaranteed
available training/deployment model. Verify current account region, supported
training tier, residency, quota, access and retirement dates using the
[fine-tuning guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/fine-tuning)
and [retirement guidance](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements).
The account may not support this candidate locally. Do not change the resource
region, silently use Global training, or silently select another model.

```powershell
& $py scripts\orders-learning.py budget-template --manifest $manifest --output .\budget-review.json
# A HUMAN fills in and signs off this JSON, including current admission evidence.
& $py scripts\orders-learning.py train --manifest $manifest --budget .\budget-review.json `
  --output .\training-plan.json
```

The template starts `approved:false`. Required approval binds the exact dataset
hash, approver/timestamps/expiry, base model/version, actual region, training tier
and residency, exact ARM `account_resource_id`, epochs/max epochs, training-token upper bound, quoted current
price/reference and maximum USD. Admission independently requires named review,
dated/expiring evidence, support, quota, training-not-retired and
deployment-not-retired attestations. `Standard` means `regional`; `GlobalStandard`
and `Developer` require explicit `global` residency consent.

The conservative token estimate is serialized UTF-8 bytes plus 1024 tokens per
training example, multiplied by explicit epochs. It is an intentionally generous
preflight estimate for text-only GPT training, **not** an exact tokenizer result
or Azure spending hard cap. Model-specific usage, validation, storage, inference
and deployment charges remain separate. A human must acknowledge that limitation
and monitor actual billing. A zero/NaN budget, token overrun, invalid epoch count,
expired approval or unsupported admission blocks submission.

Only after approval and `az login` by the operator:

```powershell
$budgetHash = (Get-FileHash .\budget-review.json -Algorithm SHA256).Hash.ToLower()
# CLOUD UPLOAD + BILLABLE TRAINING; never part of an API/UI request:
& $py scripts\orders-learning.py train --manifest $manifest --budget .\budget-review.json `
  --submit --training-project northwind-orders --confirm $budgetHash --output .\training-receipt.json
& $py scripts\orders-learning.py job-status --receipt .\training-receipt.json --output .\trained-model.json
```

The documented account OpenAI v1 API uploads only train/validation files, waits
for processing, and submits supervised fine-tuning with explicit `n_epochs` and
`extra_body.trainingType`. Automatic deployment is not requested. Receipts are
written before network calls and SDK request retries are disabled. An atomic
user-scoped journal claim at
`~\.foundry-frontier\orders-learning\training-journal` binds the approval digest,
account resource and project. It is outside movable dataset releases, so copying
or renaming a release cannot resubmit the same approval. The persistent journal
retains upload/job IDs and uncertain outcomes for reconciliation even if a local
receipt is moved. Tests redirect this journal inside their repository fixture.
On timeout/uncertain submission, inspect existing jobs/files before authorizing
a fresh budget; do not remove the marker and retry blindly.

**Account default-project gate:** `--submit` requires the explicit
`--training-project northwind-orders` selection. Before **each upload** and again
before job creation, the operator's Azure CLI identity reads the exact account
resource with ARM `GET`, API `2026-09-01`. The returned account identity, location,
and `properties.defaultProject` must match the budget and selected project.
Missing read permission, unknown/absent default, a region mismatch, or an actual
default such as `northwind-claims` blocks training. No alias, old project, Claims
fallback, default-project mutation, full-account PUT, or destructive workaround
is implemented.

A pending/accepted default-project PATCH is **not** sufficient: ARM readback must
actually report Orders. If switching the account default is unsupported or
stalled, keep training blocked pending platform support or a separately designed,
explicitly approved different-project workflow. Do not change this dataset's
project silently. The first routing failure writes a blocked receipt but uploads
nothing, creates no job and does not consume the budget's one-attempt marker.
After a later routing failure following an upload, the receipt is marked
uncertain and existing files require reconciliation; job creation is still
blocked. Coordinate account-default changes with other operators: these checks
are not an atomic service-side routing lock.

After a successful job, `trained-model.json` records the actual fine-tuned model,
trained tokens and result file IDs. Its deployment manifest targets only
`northwind-orders-triage-ft-v1`, with
`staging.candidate_environment.TRIAGE_MODEL_DEPLOYMENT` set to that name and
`staging.activation_environment={}`. This is a **candidate handoff**, not proof of
a deployed model. A separately authorized ARM/portal action must deploy to
staging, then evaluate the base and candidate on the same frozen test, complete
all four existing tiers (including red-team and harness), and obtain human
promotion approval. Only then configure the hosted agent's
`TRIAGE_MODEL_DEPLOYMENT`. Keep the current base deployment for rollback.

### Existing golden runner corrections and offline validation

The golden runner now uses the same configured judge as rubric evaluation.
Set `FOUNDRY_JUDGE_DEPLOYMENT` to a verified chat-completions judge compatible
with the pinned `azure-ai-evaluation==1.1.0` `max_tokens` parameter. The helper
also accepts configured prompt-agent/worker fallbacks; known gpt-5/o-series
deployment names are rejected for this legacy judge SDK. Opaque aliases still
require the operator to verify model compatibility. Missing judge configuration
and empty baseline are explicitly unavailable, not green gates.
Empty, nonnumeric, nonfinite, malformed or unsupported/unmeasured baseline
metrics are rejected with a nonzero exit and an explicit `baseline_error`;
invalid entries are never silently skipped.

Resolution completeness now fetches the pending persisted draft bound to the
specific `/chat` server workflow through `/hitl?workflow_id=<workflow>`, not the
global oldest-50 queue; an unrelated draft or assistant prose cannot pass.
Zero-dollar cancellation/address-change drafts are valid, boolean/nonfinite
amounts are not. Harness behavior and the four eval tiers remain intact.
`evals\baseline.json` remains unchanged and requires reviewer-approved measured
values before regression certification.

```powershell
& $py -m pytest pipelines\orders-learning\tests evals\tests\test_orders_eval_contract.py `
  evals\tests\test_harness_eval.py evals\tests\test_rubric_evaluator.py -q
& $py -m ruff check pipelines\orders-learning evals\run_evals.py `
  evals\evaluators\rubric_evaluator.py evals\evaluators\work_order_completeness.py `
  evals\tests\test_orders_eval_contract.py scripts\orders-learning.py
```

Tests block external network access and keep/remove their fixtures under the repository.
Mocked approvals and perfect outputs exist only inside tests, never as a released
dataset, trained model or benchmark. `smoke_eval.py --self-check` additionally
requires `GITHUB_TOKEN`; it does not validate Azure readiness.
