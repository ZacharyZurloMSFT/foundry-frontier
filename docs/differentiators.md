# Why Foundry — feature-by-feature vs the alternatives

A one-page checklist mapping the capabilities this repo actually demonstrates
to what each competing agent stack ships in 2026. "Native" means the SDK/portal
ships it; "DIY" means you can build it but you own the code, tests, ops, and
regressions.

| Capability                                                | **Azure AI Foundry** *(this repo)* | OpenAI Assistants / Responses | AWS Bedrock Agents           | GCP Vertex AI Agent Builder | LangChain / LlamaIndex on anything |
| --------------------------------------------------------- | ---------------------------------- | ----------------------------- | ---------------------------- | --------------------------- | ---------------------------------- |
| Realtime voice front door (barge-in, function calling)    | ✅ Voice Live *(Tab A + hands-free)* | ✅ Realtime API                | ⚠️ Bedrock + Nova Sonic (preview) | ⚠️ Gemini Live (preview)     | ❌ DIY                             |
| Multimodal document understanding (classify + extract)    | ✅ Content Understanding *(Beat 4)* | ❌ DIY on vision              | ⚠️ Textract stitched         | ⚠️ Document AI stitched      | ❌ DIY                             |
| Hosted agent runtime (multi-turn, tools, threads)         | ✅ Agent Service                    | ✅ Assistants                  | ✅ Bedrock Agents             | ✅ Reasoning Engine          | ❌ DIY                             |
| Manager/worker with typed tool contracts                  | ✅ Agent Framework + `@tool`        | ⚠️ Handoffs preview            | ⚠️ Multi-agent collab preview | ⚠️ ADK                       | ❌ DIY (LangGraph)                 |
| Agent-to-agent (Connected Agents / A2A protocol)          | ✅ *(A2 in plan)*                  | ❌                            | ⚠️ Preview                    | ⚠️ ADK                       | ❌                                 |
| Grounded retrieval that rewrites its own sub-queries      | ✅ Foundry Knowledge *(A4)*         | ⚠️ File Search (basic)         | ✅ Knowledge Bases            | ✅ Vertex Search             | ❌ DIY                             |
| Web grounding with attribution                            | ✅ Bing Grounding *(A4)*            | ✅ web_search                  | ⚠️ Bedrock + Kendra           | ✅ Google Search grounding   | ❌ DIY                             |
| First-party jailbreak / prompt-injection shields          | ✅ Prompt Shields *(A5, "Try to jailbreak" tab)* | ⚠️ Moderation only            | ⚠️ Guardrails                 | ⚠️ Safety Filters            | ❌ DIY                             |
| Protected-material + copyright detection                  | ✅ Protected Material *(A5)*        | ❌                            | ❌                           | ❌                          | ❌                                 |
| AI Red Teaming Agent (PyRIT-backed, categorized attacks)  | ✅ Foundry AI Red Team *(A6)*       | ❌ DIY                        | ❌ DIY                        | ❌ DIY                       | ❌ DIY                             |
| Persistent per-user + per-thread agent memory             | ✅ Agent memory *(A3)*              | ⚠️ Threads only                | ⚠️ Memory (preview)           | ⚠️ Sessions                  | ❌ DIY                             |
| One SDK / one auth over many frontier models              | ✅ Foundry Models catalog *(A1: GPT + Kimi K2.6 in bake-off tab)*    | ❌ OpenAI-only                | ⚠️ Bedrock catalog            | ⚠️ Model Garden              | ⚠️ Adapters per provider           |
| First-class evaluators (built-in + custom)                | ✅ 7 evaluators *(evals/)*          | ⚠️ Evals API                  | ⚠️ Model Evaluation           | ⚠️ Gen AI Evaluation         | ⚠️ DIY on ragas / trulens          |
| Prompt Optimizer that opens PRs when it beats baseline    | ✅ *(A7)*                          | ❌                            | ❌                           | ⚠️ Prompt Tuning             | ❌                                 |
| PR eval gate + continuous production eval                 | ✅ `.github/workflows/` *(B3)*      | ❌ DIY                        | ❌ DIY                        | ❌ DIY                       | ❌ DIY                             |
| In-app trace panel + agent-handoff mermaid + cost HUD              | ✅ *(A8, TracePanel + CostHUD)*     | ❌                            | ❌                           | ❌                          | ❌                                 |
| Trace → prompt → PR pivot in one click                    | ✅ Foundry Observability *(B6)*     | ❌                            | ❌                           | ❌                          | ❌                                 |
| MCP tools as first-class in the agent runtime             | ✅ *(mcp-tech-lookup, mcp-work-order)* | ✅                             | ⚠️                            | ⚠️                           | ⚠️                                  |
| Private VNet, cross-region PEs, UAMI, no keys             | ✅ *(infra/)*                       | ❌ (public API only)          | ✅ VPC endpoints              | ✅ VPC-SC                    | N/A                                |
| One-command deploy from empty subscription                | ✅ `azd up`                         | ❌ N/A                        | ⚠️ CDK                        | ⚠️ Terraform                 | ❌ N/A                             |

Legend: ✅ native/GA · ⚠️ preview or partial (needs stitching) · ❌ not offered / DIY.

## Harness choice without replacing the Foundry platform

The opt-in **Agent Harness** workspace demonstrates GitHub Copilot SDK as an
alternative application-owned orchestration loop. Foundry still supplies
models, identity-backed specialist agents, retrieval, and the existing MCP
tools. The SDK runs inside FastAPI; do not describe it as another native
Foundry hosted agent or as requiring GitHub model billing.

Its visible tool timeline, saved Cosmos results, and human-approval status are
real execution evidence. The existing Agent Framework flow remains available.
This is a harness choice, not a claim that Foundry's full hosted-agent lifecycle
is automatically provided by the SDK.

## The one-line pitch

> **Foundry is the only stack that ships the whole agent lifecycle — realtime
> voice, multimodal doc understanding, agentic retrieval, safety shields, red
> teaming, memory, evals, observability, optimizer, and IaC — as first-party,
> and lets you drive it all from GitHub.** Competitors force you to stitch 3+
> vendors and own the failure modes yourself.

## What this repo demonstrates end-to-end

- **Story:** Northwind Order Concierge handles ~40,000 customer contacts a
  week. A customer contacts support → voice/chat triages → orchestrator hands
  off to specialists → resolution drafted (refund / reship / RMA / credit /
  cancel) → human approves → action applied.
- **Every row of the table above with a ✅ against Foundry is executable in
  this repo, no external accounts required, in one `azd up`.**
- **Every row is also gated by a GitHub Action** — see [`genaiops.md`](./genaiops.md).

## For the SE presenting this

Print the table. Walk into the room. Ask the customer to point at any row and
ask "does your current stack ship this?" — the answer will almost always be
"no, we built it." That's the wedge.
