# Why Foundry — feature-by-feature vs the alternatives

A one-page checklist mapping the capabilities this repo actually demonstrates to what each
competing agent stack ships in 2026. "Native" means the SDK/portal ships it; "DIY" means you can
build it but you own the code, tests, ops, and regressions.

**How to read it.** Every cell in the Foundry column describes *this repo* and names the file,
workflow or screen that backs it ([`demo-script.md`](./demo-script.md) walks the screens). The four
competitor columns are an indicative snapshot kept from the original comparison: this repository
cannot evidence them, so re-check each cell against the vendor's current documentation before you
present it.

| Capability                                                | **Azure AI Foundry** *(this repo)* | OpenAI Assistants / Responses | AWS Bedrock Agents           | GCP Vertex AI Agent Builder | LangChain / LlamaIndex on anything |
| --------------------------------------------------------- | ---------------------------------- | ----------------------------- | ---------------------------- | --------------------------- | ---------------------------------- |
| Realtime voice front door (barge-in)                      | ✅ Voice Live *(`apps/web-voice`, Beat 2: keyless WebSocket proxy, server-side barge-in, no tools in the call)* | ✅ Realtime API                | ⚠️ Bedrock + Nova Sonic (preview) | ⚠️ Gemini Live (preview)     | ❌ DIY                             |
| Multimodal document understanding (classify + extract)    | ✅ Content Understanding *(Claims Document Intelligence, analyzer `northwindInsuranceClaimsV1`, Beat 6)* | ❌ DIY on vision              | ⚠️ Textract stitched         | ⚠️ Document AI stitched      | ❌ DIY                             |
| Hosted agent runtime (multi-turn, tools, threads)         | ✅ Agent Service hosted agents *(five roles, `agents/hosted`)* | ✅ Assistants                  | ✅ Bedrock Agents             | ✅ Reasoning Engine          | ❌ DIY                             |
| Manager/worker with typed tool contracts                  | ✅ Agent Framework + `@tool` *(orchestrator → four specialists, Beat 3)* | ⚠️ Handoffs preview            | ⚠️ Multi-agent collab preview | ⚠️ ADK                       | ❌ DIY (LangGraph)                 |
| Grounded retrieval that rewrites its own sub-queries      | ⚠️ Foundry IQ knowledge base on Azure AI Search, configured here for minimal, extractive retrieval (no query-planning model); grounding is enforced in code *(Beat 5)* | ⚠️ File Search (basic)         | ✅ Knowledge Bases            | ✅ Vertex Search             | ❌ DIY                             |
| AI Red Teaming Agent (PyRIT-backed, categorized attacks)  | ✅ Foundry AI Red Team *(`pipelines/red-team`, weekly in `.github/workflows/red-team.yml`)* | ❌ DIY                        | ❌ DIY                        | ❌ DIY                       | ❌ DIY                             |
| One SDK / one auth over many frontier models              | ✅ Foundry deployments behind one account and identity *(Bicep parameters in `infra/main.bicep`: planner `gpt-5.4`, worker `gpt-5.4-mini`, prompt agents `gpt-4o`; adjuster support offers `Kimi-K2.6` when it is deployed)* | ❌ OpenAI-only                | ⚠️ Bedrock catalog            | ⚠️ Model Garden              | ⚠️ Adapters per provider           |
| First-class evaluators (built-in + custom)                | ✅ 7 evaluators in the Tier-2 run: 4 built-in, 3 custom *(`evals/`)* | ⚠️ Evals API                  | ⚠️ Model Evaluation           | ⚠️ Gen AI Evaluation         | ⚠️ DIY on ragas / trulens          |
| Prompt Optimizer that opens PRs when it beats baseline    | ✅ *(`.github/workflows/optimizer.yml`; needs a measured baseline)* | ❌                            | ❌                           | ⚠️ Prompt Tuning             | ❌                                 |
| PR eval gate + continuous production eval                 | ✅ *(`.github/workflows/smoke-eval.yml`, `.github/workflows/evals-on-pr.yml`, `.github/workflows/continuous-eval.yml`; regression gates report `unavailable` until a reviewer approves a measured baseline)* | ❌ DIY                        | ❌ DIY                        | ❌ DIY                       | ❌ DIY                             |
| In-app trace panel + agent-handoff diagram + token and cost totals | ✅ *(Trace panel under the Claims Concierge chat, Beat 7)* | ❌                            | ❌                           | ❌                          | ❌                                 |
| Trace → build and prompt-version pivot                    | ✅ *(git SHA and prompt hashes on spans and in the panel; the SHA links to the commit when `VITE_REPO_URL` is set)* | ❌                            | ❌                           | ❌                          | ❌                                 |
| MCP tools as first-class in the agent runtime             | ✅ *(`apps/mcp-policy-lookup`: `lookup_policy`, `lookup_claim`; `apps/mcp-claim-review`: `draft_claim_action`)* | ✅                             | ⚠️                            | ⚠️                           | ⚠️                                  |
| Managed identity and private networking for the data plane | ⚠️ *(one user-assigned managed identity for Foundry, Cosmos DB and Storage; Cosmos DB behind a private endpoint; Foundry, Search, Storage and the registry stay public and Search runs in API-key mode, see `infra/`)* | ❌ (public API only)          | ✅ VPC endpoints              | ✅ VPC-SC                    | N/A                                |
| One-command deploy from empty subscription                | ⚠️ `azd up` *(provisions the infrastructure and deploys the five apps; knowledge, hosted agents, the analyzer and identity are explicit scripts, see README)* | ❌ N/A                        | ⚠️ CDK                        | ⚠️ Terraform                 | ❌ N/A                             |

Legend: ✅ native/GA · ⚠️ preview or partial (needs stitching) · ❌ not offered / DIY.

## Harness choice without replacing the Foundry platform

The opt-in **Copilot SDK harness** (`COPILOT_HARNESS_ENABLED=true`, `/harness/*`, API only)
demonstrates GitHub Copilot SDK as an alternative application-owned orchestration loop. Foundry still
supplies models (reached with a managed-identity token), identity-backed specialist agents, retrieval
and the existing MCP tools. The SDK runs inside FastAPI; do not describe it as another native
Foundry hosted agent or as requiring GitHub model billing.

Its tool events, saved Cosmos results and human-approval status (`GET /harness/runs/{run_id}`) are
real execution evidence. The hosted-agent flow remains the default. This is a harness choice, not a
claim that Foundry's full hosted-agent lifecycle is automatically provided by the SDK.

## The one-line pitch

> **Foundry puts the pieces of an agent lifecycle — realtime voice, document understanding, hosted
> agents, grounded retrieval, MCP tools, evaluators, red teaming, observability and IaC — under one
> account, one identity and one SDK family, and this repo shows how to drive them from GitHub.**
> You can stitch the same pieces from separate vendors; the point is that you do not have to own
> every seam and failure mode yourself.

## What this repo demonstrates end-to-end

- **Story:** Northwind Insurance, a fictional property-and-casualty insurer (Washington and Colorado
  auto). A policyholder reports a loss by voice or chat → the claims-concierge orchestrator hands the
  work to four hosted specialists (triage, policy and claim lookup, policy wording, claim-action draft)
  → a claim action is drafted (request evidence, assign an adjuster, update the policyholder,
  escalate for review, or a settlement *recommendation*) → a person approves or rejects the draft →
  nothing is executed. No agent or tool can approve, deny or pay a claim.
- **Beside it:** adjuster support (cited procedures), claim-document understanding with human review
  of every extraction, the adjuster workbench in the portal and the Copilot SDK harness. All data is
  synthetic.
- **What `azd up` does and does not do.** It provisions the infrastructure and deploys the five apps.
  The hosted agents, knowledge, the Content Understanding analyzer and identity setup are explicit,
  dry-run-first scripts (README, *Deploy*), so the Foundry-backed rows need those steps and a verified
  agent fleet, and the CI rows need GitHub secrets.
- **Delivery and evaluation are driven by GitHub Actions.** [`genaiops.md`](./genaiops.md) says what each
  gate does and does not enforce today; in particular the committed `evals/baseline.json` is
  intentionally empty until a reviewer approves a measured run.

## For the SE presenting this

Print the table. Walk into the room. Ask the customer to point at any row and ask "does your current
stack ship this, and who owns it?" That is the conversation to have, but check the competitor columns
against current vendor documentation first, and be candid about every ⚠️ in the Foundry column.
