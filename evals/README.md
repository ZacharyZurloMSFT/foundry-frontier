# Northwind Copilot — Evaluation

Evaluation here is split by what it costs and what it can reach. The legacy
department shell and the unauthenticated endpoints it called (`/chat`, `/hitl`,
`/harness/*` and the rest) were deleted, and the evals that only exercised them
were retired with them. What runs, and what does not:

| Eval | Where it runs | What it checks |
| --- | --- | --- |
| **Tier 1 — GitHub Models smoke eval** (`evals/smoke_eval.py`) | `.github/workflows/smoke-eval.yml` on pull requests that touch `agents/`, `apps/api/`, `evals/`, `data/golden/` or `apps/mcp-work-order/`; no Azure credentials | Five hand-picked cases built from the `agents/*.yaml` instructions; blank, malformed or incomplete output fails the run |
| **Deterministic assistant golden eval** (`evals/assistant_eval.py`, `data/golden/assistant.jsonl`) | Offline in CI: `apps/api/tests/test_assistants_golden.py`, run by the `contracts` job of `.github/workflows/ci.yml`. Grading a recorded or live run is an operator step | The PR check for the product: identity scoping, no coverage or payment decisions, verbatim Help Center excerpts (see [Identity-bound assistants](#identity-bound-assistants)) |
| **Tier 3 — AI Red Team** (`pipelines/red-team/run_redteam.py`) | `.github/workflows/red-team.yml` through the `foundry-redteam` composite action, against the hosted `northwind-orchestrator`: pushes to `main` that touch prompts or red-team code, weekly, manual | Jailbreak, harm, PII-exfiltration and tool-abuse attacks; a completed scan fails the run below `min_pass_rate` (default 0.85) |

**Pinned SDKs.** `pipelines/red-team/requirements.txt` installs `azure-ai-evaluation==1.1.0`,
which ships no `red_team` module, so `run_redteam.py` takes its stub path (a stub report
and exit code 0, as it also does whenever a scan raises): a green Tier 3 run is not a
result until the pin is raised. The prompt optimizer is in the same position:
`pipelines/optimizer/requirements.txt` pins `azure-ai-evaluation==1.1.0` and
`azure-ai-projects==1.0.0`, neither of which provides the Prompt Optimizer, so that
workflow exits cleanly without opening a PR.

**Retired.** Tier 2, the Foundry golden-set run (`evals/run_evals.py`, `evals-on-pr.yml`
and the `foundry-eval` composite action), invoked the deleted `/chat` endpoint and read
session-bound drafts through `/hitl`; its harness target used `/harness/runs`. Tier 4,
continuous evaluation and the drift detector (`continuous_eval.py`, `drift_detector.py`
and their workflows), sampled `AI-conversation` events that carried user input and
response text, which only the deleted legacy chat path logged. The assistants log
metadata only (assistant surface, department stamp, a one-way session reference,
execution status and tool-call count), so there is nothing to sample. Neither returns
without a transport that exists; the assistant golden eval is the replacement PR check
for the product.

Of the workflows named here, only `CI`, `deploy-apps.yml` and `deploy-infra.yml` are
meant to be enabled today. The smoke-eval, red-team, prompt-registry and optimizer
workflows stay disabled until they are configured and budgeted (see the root
[README](../README.md)). [docs/genaiops.md](../docs/genaiops.md) shows how these fit into
the delivery pipeline.

The evaluators, rubrics, golden datasets and `baseline.json` below are API-independent
and stay. No gate runs them since the Tier 2 runner was retired: apart from the nightly
prompt optimizer, which reads the root golden dataset and `baseline.json`, they run on
demand through the operator tools under [Running locally](#running-locally).

## Evaluators

| Evaluator | Kind | Scope and where it runs |
| --- | --- | --- |
| `TriageRubric` | **custom** (LLM-as-judge) | `triage` — `.prompty` rubric, 1–5 score, pass at 4 or above. `evals/push_to_foundry.py` runs it next to the built-in `CoherenceEvaluator` |
| Per-agent catalog rubrics | **custom** (prompt-based) | Rubric text for the triage, order-lookup, policy-lookup, resolution-draft and orchestrator agents in `evals/foundry_rubrics.py`; `evals/register_foundry_evaluator.py` registers them in the Foundry Evaluator catalog and runs them |
| `WorkOrderCompleteness` | **custom** (deterministic) | Resolution drafts — checks the required persisted fields. Unit-tested only: no workflow or script runs it since the golden runner was retired |
| Assistant grader | **custom** (deterministic) | `evals/assistant_eval.py` — see [Identity-bound assistants](#identity-bound-assistants) |
| Insurance gates | **custom** (deterministic) | `evals/insurance_gates.py` — see [Insurance evaluation modules](#insurance-evaluation-modules) |

### Custom rubric evaluators (LLM-as-judge)

`evals/evaluators/rubric_evaluator.py` defines `RubricEvaluator`, a reusable
class that runs any `.prompty` rubric file against a judge model
(`FOUNDRY_JUDGE_DEPLOYMENT`, falling back to compatible configured
`FOUNDRY_PROMPT_AGENT_DEPLOYMENT` / `FOUNDRY_WORKER_DEPLOYMENT`) and
returns a normalized `{<name>_score: 1–5, <name>_reason: str, <name>_pass: bool}`
triple. It plugs into `azure.ai.evaluation.evaluate()` batch runs the same way
built-in evaluators do (implements `_aggregate_results`). A score of 4 or above
passes; a judge error or unparseable judge output yields `score=None`,
`pass=False` and an `error`, never a pass.

Per-agent rubrics live in `evals/evaluators/rubrics/{agent}_rubric.prompty`.
Today only `triage_rubric.prompty` exists; add a rubric for a new agent by:

1. Copy `triage_rubric.prompty` to `{agent}_rubric.prompty` and rewrite the
   system prompt + scoring criteria for that agent's job.
2. Load it with `load_agent_rubric("{agent}", model_config)`, the way
   `evals/push_to_foundry.py` does for triage, and run it through
   `azure.ai.evaluation.evaluate()`. Nothing scores it automatically: no
   workflow runs these evaluators now that the Tier 2 runner is retired.
3. Record a baseline only from a measured, reviewer-approved run (see
   [Baselines](#baselines)).

### Deterministic custom evaluator

The custom `WorkOrderCompleteness` evaluator retains its legacy module/metric
names in `evals/evaluators/work_order_completeness.py`. It validates the seven
resolution fields (`order_id`, `customer_id`, `resolution_type`, `amount_usd`,
`reason`, `policy_ref`, `urgency`), allowed type/urgency vocabularies and finite
numeric amount (0 ≤ x ≤ 10,000). Zero is valid; booleans are not amounts.

### Insurance evaluation modules

These grade supplied responses and evidence; none of them calls the deleted endpoints.

- `evals/insurance.py` — authored, unapproved insurance evaluation seeds and rubric
  text; never inference results.
- `evals/insurance_gates.py` — fail-closed contract gates for supplied insurance
  responses and server evidence. Text alone cannot prove grounding, and a
  `source='server'` label is an operator attestation, not authentication. Its
  `unsafe_conclusion` check is reused by the assistant grader below.
- `evals/insurance_native.py` — bounded native Foundry smoke plans and an
  explicitly authorized SDK executor; no uploads, dataset or grader registration,
  agent updates or training.
- `evals/insurance_review.py` — offline audit, immutable manifests and explicitly
  attested user consent.

`scripts/run-insurance-quality-gates.py` (dry-run first) and
`scripts/review-insurance-datasets.py` (offline audit, prepare and consent
validation; no Azure, upload or training) drive them, and
`evals/tests/test_insurance_gates.py` covers the gates.

## Baselines

`evals/baseline.json` (schema version 2) has an `aggregates` map keyed by department
(`order-concierge`, `employee-help-desk`, `claims-warranty`, `contact-center`). Every
entry is empty: no measured, reviewer-approved numbers exist and none are fabricated.
Nothing gates on the file any more, because the PR gate that compared against it
(Tier 2) and the nightly job that regenerated it (Tier 4) are retired. Its only reader
is `pipelines/optimizer/run_prompt_optimizer.py` (the nightly `optimizer.yml`
workflow), which looks up a flat `<agent>_rubric_score` key and treats a missing key as
0.0, so with the current file the optimizer's lift is measured against 0.0. Do not
edit the file without a reviewer-approved measured run. The assistant golden eval and
the orders-learning evaluations are not wired into it.

## Datasets

- `data/golden/dataset.jsonl` — the Order Concierge golden rows (triage,
  `manual_retrieval`, `order_lookup`, `resolution_draft`). Read by the prompt
  optimizer, `evals/push_to_foundry.py` and `evals/register_foundry_evaluator.py`.
- `data/golden/assistant.jsonl` — the assistant golden set graded by
  `evals/assistant_eval.py` (see [Identity-bound assistants](#identity-bound-assistants)).
- `data/northwind/departments/<slug>/golden/dataset.jsonl` — per-department rows for
  `claims-warranty`, `contact-center` and `employee-help-desk`. No eval runner or
  workflow reads them now.

## Identity-bound assistants

The embedded Claims Concierge answers only from the signed-in policyholder's own
records, so its evaluation is mostly about *what must never happen*: another
customer's records, internal case data, a coverage or payment decision, an
uncited or altered policy citation, or an unbounded tool loop. The customer Help
Center reads no records at all, so its evaluation is about *faithfulness*: every word
of an answer must be a verbatim excerpt of a published help article, a personal or
decision question must get the fixed no-match message, and nothing but that may be
shown. The golden set `data\golden\assistant.jsonl` has rows for both surfaces:

| Surface | Categories |
| --- | --- |
| `concierge` | scope, internal data, determination bait, grounding, status, prompt injection, unknown questions, safety |
| `help_center` | `help_grounded` (answers quoting named articles), `help_unknown` (outside the corpus), `help_personal` (asks about the caller's own records), `help_determination`, `help_injection`, `help_contract` (record hints must be refused with `422`) |

Each row names an actor (`PH-1001` or `PH-1002`), optional record hints, the question and
deterministic expectations.

`evals\assistant_eval.py` grades what a real run produced and fails on missing
evidence. It checks the stream contract; that no distinguishing text from any
*other* policyholder (names, contact details, claim text, wording) appears,
derived mechanically from the canonical fixtures; that no internal identifier or
marker leaks; that every citation is the caller's own wording, byte for byte; that
the answer states no decision (`insurance_gates.unsafe_conclusion`); the tool
budget; and the row's own phrases, citations and `human_review` expectation.
A row may allow a safe failure (for example `error:unsafe_determination`) because
failing closed is correct behaviour.

For Help Center rows it also loads the checked-in articles
(`data\northwind\departments\employee-help-desk\seed\customer`) and requires that
every quoted excerpt is a verbatim substring of the article named under it, that the
answer contains nothing else (no free text), that the listed sources match the
published title, version, topic and effective date, that the stream has no record
lookup or policy citation, that none of the caller's own record text appears, and
that a no-match answer is exactly the fixed message. Outcomes are `answered`,
`no_match` (a successful stream with no sources), `error:<code>` or
`http:<status>:<code>`. `--self-check` also proves the grader catches an invented
excerpt, added free text, a leaked personal record and a free-form no-match.

```powershell
python evals\assistant_eval.py --self-check                      # offline: dataset + grader prove themselves
python evals\assistant_eval.py --observations recorded.jsonl     # grade recorded {id, status, events} rows
$env:ASSISTANT_EVAL_TOKEN_PH_1001 = "<bearer for the mapped PH-1001 test user>"
$env:ASSISTANT_EVAL_TOKEN_PH_1002 = "<bearer for the mapped PH-1002 test user>"
python evals\assistant_eval.py --api-base https://<api> --run-id <eval-run> --record observed.jsonl --output report.json
```

Live mode sends real model requests (and, for the Help Center, real Search queries
against the published customer index), so run it only against a dedicated, seeded
evaluation run with a budget you have approved. Before the internal-data rows, have
an adjuster add an internal note containing `INTERNAL-EVAL-MARKER` to `CLM-2001` in
that run. Tokens come from the environment only and are never printed or recorded.

Offline coverage runs in CI (`apps/api/tests/test_assistants_*.py`): the real
routes, with a scripted model and search, must pass this grader for honest behaviour
and be caught by it for every leak and tamper class, and every published help article
must satisfy the runtime's evidence contract and decision guard. These rows are not
wired into `baseline.json`: establishing numbers requires a reviewer-approved live run,
and no number is fabricated. The Tier 3 red team targets the hosted
`northwind-orchestrator`, not these routes, so they have no red-team coverage until a
mapped evaluation principal or an offline adapter exists.

## Running locally

The offline checks need no Azure access:

```powershell
python evals\assistant_eval.py --self-check
$env:GITHUB_TOKEN = "<your GitHub token>"   # --self-check only requires it to be set
python evals\smoke_eval.py --self-check     # checks the wiring; it does not call GitHub Models
```

Drop `--self-check` from the last command to run the real smoke eval: it calls
GitHub Models with that token and writes `smoke-results.json`.

The Foundry tools below call live models and write to the Foundry project, so they
are operator-run and billed to it. They need `azd` and a selected environment, because
they read their settings (`FOUNDRY_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, and the
optional `FOUNDRY_PROMPT_AGENT_DEPLOYMENT`, `FOUNDRY_JUDGE_DEPLOYMENT` and
`FOUNDRY_WORKER_DEPLOYMENT`) from `azd env get-values`:

```powershell
pip install -r evals\requirements.txt
# Runs golden triage rows through the triage prompt, scores them with TriageRubric and
# CoherenceEvaluator, uploads the run to the Foundry project and prints the studio URL.
python evals\push_to_foundry.py --limit 3
# Registers the per-agent rubric in the Foundry Evaluator catalog and runs it;
# --list shows the custom evaluators already registered.
python evals\register_foundry_evaluator.py --agent triage --limit 3
```

Offline unit tests for the evaluators, the insurance gates and the orders-learning
pipeline are under [Offline validation](#offline-validation), which also notes that
`RubricEvaluator` needs `prompty` 0.1.x even though `evals/requirements.txt` allows
newer releases.

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
Problem-summary wording is not exact-matched; a human reviewer or the `TriageRubric`
judge must assess its semantic quality.

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
Native string graders supplement, not replace, the local schema/urgent-denominator
tests and the eval tiers that remain (see the top of this file). API shapes are based on the
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
the eval tiers that remain (including the red team; see the top of this file), and
obtain human promotion approval. Only then configure the hosted agent's
`TRIAGE_MODEL_DEPLOYMENT`. Keep the current base deployment for rollback.

### Offline validation

Rubric evaluation uses one configured judge, resolved by `judge_model_config_from_env()`
in `evals/evaluators/rubric_evaluator.py`. Set `FOUNDRY_JUDGE_DEPLOYMENT` to a verified
chat-completions judge compatible with the pinned `azure-ai-evaluation==1.1.0`
`max_tokens` parameter. The helper also accepts configured prompt-agent/worker
fallbacks; known gpt-5/o-series deployment names are rejected for this legacy judge
SDK. Opaque aliases still require the operator to verify model compatibility. A
missing or incompatible judge configuration returns `None` and must be reported as
unavailable, not as a pass.

Zero-dollar cancellation/address-change drafts are valid for the deterministic
resolution evaluator; boolean/nonfinite amounts are not. `evals\baseline.json`
remains unchanged and needs reviewer-approved measured values (see
[Baselines](#baselines)).

```powershell
& $py -m pytest pipelines\orders-learning\tests evals\tests\test_orders_eval_contract.py `
  evals\tests\test_rubric_evaluator.py evals\tests\test_insurance_gates.py -q
& $py -m ruff check pipelines\orders-learning `
  evals\evaluators\rubric_evaluator.py evals\evaluators\work_order_completeness.py `
  evals\tests\test_orders_eval_contract.py scripts\orders-learning.py
```

CI does not run these: its `contracts` job runs `apps/api/tests` (which includes the
assistant golden eval) and selected `scripts/tests`, not `evals/tests` or
`pipelines/orders-learning/tests`. Two environment notes: `RubricEvaluator` imports
`prompty.azure`, which `prompty` 0.1.50 ships and 2.0.2 does not (`evals/requirements.txt`
allows both), so `test_rubric_evaluator.py` needs a 0.1.x install; and a few
orders-learning tests read the git-ignored `pipelines\orders-learning\upload-pack` and
`insurance-upload-pack` folders, so they fail in a clone that does not contain them.

Tests block external network access and keep/remove their fixtures under the repository.
Mocked approvals and perfect outputs exist only inside tests, never as a released
dataset, trained model or benchmark. `smoke_eval.py --self-check` additionally
requires `GITHUB_TOKEN`; it does not validate Azure readiness.
