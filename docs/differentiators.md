# Why Foundry — feature-by-feature vs the alternatives

A one-page checklist mapping the capabilities this repo actually demonstrates
to what each competing agent stack ships in 2026. "Native" means the SDK/portal
ships it; "DIY" means you can build it but you own the code, tests, ops, and
regressions.

The first table is what the 10-minute demo, the infrastructure and the GitHub
workflows exercise. The second is shipped in the repo but no longer wired into
the portal, so don't demo it. Anything marked *operator-enabled* is off by
default and needs your own Entra tenant, Foundry deployment, Search index or
budget: the repo proves its contracts offline, not a live deployment.

## What the demo exercises

| Capability                                                | **Azure AI Foundry** *(this repo)*                                                  | OpenAI Assistants / Responses | AWS Bedrock Agents           | GCP Vertex AI Agent Builder | LangChain / LlamaIndex on anything |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------- | ---------------------------- | --------------------------- | ---------------------------------- |
| Multimodal document understanding (classify + extract)    | ✅ Content Understanding *(claim evidence worker; operator-enabled)*                | ❌ DIY on vision              | ⚠️ Textract stitched         | ⚠️ Document AI stitched      | ❌ DIY                             |
| One SDK / one auth over many frontier models              | ✅ Foundry Models catalog *(the assistants call a configured deployment with managed identity; the answer panel names it)* | ❌ OpenAI-only                | ⚠️ Bedrock catalog            | ⚠️ Model Garden              | ⚠️ Adapters per provider           |
| First-class evaluators (built-in + custom)                | ✅ Rubric (`.prompty`) and deterministic custom evaluators *(`evals/`)*             | ⚠️ Evals API                  | ⚠️ Model Evaluation           | ⚠️ Gen AI Evaluation         | ⚠️ DIY on ragas / trulens          |
| AI Red Teaming Agent (PyRIT-backed, categorized attacks)  | ⚠️ Foundry AI Red Team *(Tier 3; aimed at the hosted orchestrator agent, not the portal's assistants; see the caveat under the eval tiers)* | ❌ DIY                        | ❌ DIY                        | ❌ DIY                       | ❌ DIY                             |
| Prompt Optimizer that opens PRs when it beats baseline    | ⚠️ *(`optimizer.yml`; operator-enabled, a person reviews any PR; see the same caveat)* | ❌                            | ❌                           | ⚠️ Prompt Tuning             | ❌                                 |
| Private networking + identity-based access                | ⚠️ *(infra/: UAMI, VNet-integrated apps, Cosmos private endpoint; see the note)*    | ❌ (public API only)          | ✅ VPC endpoints              | ✅ VPC-SC                    | N/A                                |
| One-command deploy from empty subscription                | ✅ `azd up` *(greenfield; Bicep is compiled and contract-tested in CI)*             | ❌ N/A                        | ⚠️ CDK                        | ⚠️ Terraform                 | ❌ N/A                             |

Legend: ✅ native/GA · ⚠️ preview or partial (needs stitching) · ❌ not offered / DIY.
In the Foundry column ⚠️ also flags a piece this repo pins to a preview or beta
API, or cannot prove today (see the caveats).

**Network posture, honestly.** The template gives every app a user-assigned
managed identity and passes no keys to the apps, integrates the Container Apps
environment with a VNet and puts Cosmos behind a private endpoint. Storage,
Search (configured for key auth), Foundry and ACR keep public endpoints, and the
API, the web app and the MCP servers have public ingress. Treat it as a demo
posture and tighten it before production. The migrated environment is released
through the scoped workflows, not `azd up`; see the README.

## Also shipped, but not wired into the portal

| Capability                                                | **Azure AI Foundry** *(this repo)*                                                  | OpenAI Assistants / Responses | AWS Bedrock Agents           | GCP Vertex AI Agent Builder | LangChain / LlamaIndex on anything |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------- | ---------------------------- | --------------------------- | ---------------------------------- |
| Hosted agent runtime (multi-turn, tools, threads)         | ⚠️ Agent Service *(`agents/`: a manager and four specialists, deployed from `azure.yaml`; the hosting package is a beta pin and the client sets `allow_preview=True`)* | ✅ Assistants                  | ✅ Bedrock Agents             | ✅ Reasoning Engine          | ❌ DIY                             |
| Manager/worker with typed tool contracts                  | ✅ Agent Framework + `@tool`                                                        | ⚠️ Handoffs preview            | ⚠️ Multi-agent collab preview | ⚠️ ADK                       | ❌ DIY (LangGraph)                 |
| MCP tools as first-class in the agent runtime             | ✅ *(mcp-tech-lookup, mcp-work-order)*                                              | ✅                             | ⚠️                            | ⚠️                           | ⚠️                                  |
| Grounded retrieval that rewrites its own sub-queries      | ⚠️ Foundry IQ knowledge bases *(`scripts/create-knowledge-base.py` on the Search `2026-05-01-preview` API; the hosted `policylookup` agent)* | ⚠️ File Search (basic)         | ✅ Knowledge Bases            | ✅ Vertex Search             | ❌ DIY                             |

The portal does not call the hosted agents, the MCP servers or the per-department
Foundry projects, and the Help Center uses a plain Azure AI Search keyword query
plus one tool-less model call, not agentic retrieval. Present these rows as "also
in the repo", never as part of the demo flow.

## What the demo adds on top of the platform

These are application patterns in this repo, not platform features, so they carry
no vendor comparison. They run on Foundry models and are what the demo defends
(see [`demo-script.md`](./demo-script.md)).

- **Identity-bound by construction** *(Beats 1, 2, 4)*. FastAPI verifies the Entra
  token and takes the role and policyholder from a server-side run mapping. The
  Claims Concierge gets four read-only tools bound to that caller; request bodies
  and model arguments can never supply an identity, and a record that belongs to
  someone else is indistinguishable from one that doesn't exist.
- **Fail closed** *(Beat 2)*. An answer is shown only if it parses, cites only
  wording it was shown, names only records it read and doesn't read like a
  coverage, claim or payment decision. Otherwise there is an error and no answer,
  and nothing is saved. A content-filter block from the model deployment becomes a
  neutral message, never provider text.
- **Extractive Help Center** *(Beat 5)*. The model only selects excerpts from
  twelve published customer help articles. The server checks that each is a
  verbatim substring of a retrieved article and renders every word shown. No
  match, a personal-record question or a request for a decision gets one fixed
  message.
- **A person decides** *(Beats 3, 6)*. Claim stages can't express a decision
  (reported, evidence requested, evidence received, adjuster review, customer
  update prepared). Each adjuster action is version-checked and recorded on a
  timeline, extracted document fields stay untrusted suggestions until an
  adjuster confirms each one, and no assistant can write.
- **Evidence integrity** *(Beat 4)*. Original documents sit in private immutable
  blobs, and the portal re-checks type, size and SHA-256 before it offers a
  download.
- **Provenance** *(Beats 2, 7)*. Each answer shows its model deployment and prompt
  version. `GET /build` reports the git SHA and a SHA-256 per assistant prompt,
  and each turn logs one metadata-only `AI-conversation` line (surface,
  department stamp, one-way session reference, outcome, tool-call count) and an
  `assistant.turn` span, never the question or the answer.

## Eval tiers: what exists now

| Tier | Status                                                    | What it is                                                                                                                                                   |
| ---- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1    | Kept (`smoke-eval.yml`, `evals/smoke_eval.py`)            | GitHub Models smoke eval: five micro-cases against the prompts in `agents/*.yaml`; free endpoint, no Azure credentials.                                        |
| 2    | Retired (files removed)                                   | Foundry golden-set runner on PRs (`run_evals.py`, `evals-on-pr.yml`). It exercised the deleted `/chat`, `/hitl` and `/harness` endpoints.                      |
| 3    | Kept (`red-team.yml`)                                     | Foundry AI Red Team against the hosted `northwind-orchestrator` agent: weekly, on prompt or agent changes pushed to `main`, or on demand. See the caveat below.   |
| 4    | Retired (files removed)                                   | Continuous production eval and drift detector (`continuous_eval.py`, `drift_detector.py`), for the same reason.                                                |
| —    | Active in CI                                              | Deterministic assistant golden eval (`data/golden/assistant.jsonl`, 60+ rows, graded by `apps/api/tests/test_assistants_golden.py`) plus the contract tests in `ci.yml`. |

The golden eval replays the real routes with a scripted model and search, so it
proves scoping, guards and rendering rather than live model quality. Only CI and
the two scoped deploy workflows are meant to be enabled; the rest stay disabled
until configured and budgeted. `evals/baseline.json`, which the prompt optimizer
also reads, is empty on purpose until a reviewer approves a measured live run. See
[`genaiops.md`](./genaiops.md).

**Caveat for Tier 3 and the optimizer.** As pinned today, both pipelines install
`azure-ai-evaluation==1.1.0`, whose wheel has no `RedTeam` API and no optimizer.
Their runners catch the missing SDK and still exit 0, with a stub report (red
team) or `improved=false` (optimizer), so a green run proves nothing until the
pins are updated. Don't present either as a result.

## The one-line pitch

> **Foundry ships the models, document understanding, evaluators, red teaming,
> observability and IaC as first-party services, and this repo shows how to put
> them behind a product that stays under human control: AI embedded where the
> work happens, scoped to the signed-in person, failing closed, and tested from
> GitHub on every PR.** On the other stacks in the table, more of that is DIY or
> preview.

## What this repo demonstrates end-to-end

- **Story:** Northwind Insurance, a fictional auto insurer with synthetic records.
  A policyholder signs in, reads their policy, asks the Claims Concierge, reports
  a loss, attaches evidence and gets plain-English help. An adjuster works the
  case with versioned actions and a recorded timeline, and decides everything.
- **Every ✅ or ⚠️ above maps to code, infra or a workflow in this repo.** Anything
  that needs a live Entra tenant, model deployment, Search index or budget is
  *operator-enabled*.
- **GitHub gates the code:** CI runs on every PR and on `main`, and the scoped app
  and infrastructure releases run only after CI passes on `main`.

## For the SE presenting this

Print the table. Walk into the room. Ask the customer to point at any row and
ask "does your current stack ship this?" — the answer will almost always be
"no, we built it." That's the wedge.

Be as plain about the gaps as about the wins. Staff-facing assistants, an
in-portal approvals workflow, voice and live-measured eval baselines are not
built, the hosted agents are not part of the demo flow, and the default network
posture is a demo posture. A demo that says what it isn't is the one customers
trust.
