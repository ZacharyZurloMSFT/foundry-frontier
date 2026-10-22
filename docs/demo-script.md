# Northwind Copilot — 10-minute demo script

The whole demo runs against a single `azd up` deployment. Two browser tabs open
before you start:

- **Tab A**: the customer voice front door (`WEB_VOICE_URI`)
- **Tab B**: the CX-agent app (`WEB_TECH_URI`)

The demo script's department beats (the multi-agent chat, Approvals, bake-off,
jailbreak lab, trace panel, department variants and Agent Harness) run in the
legacy shell build (`VITE_INSURANCE_BUSINESS_ENABLED=false`). The signed-in
insurance portal embeds its AI instead; see
[Embedded assistants](#embedded-assistants-insurance-portal-3-min).

## Beat 0 (0:00–1:00) — "One company, four departments"

Open **Tab B** on the landing page. Four department tiles:

- **Order Concierge** — Multi-agent + HITL (the flagship)
- **Employee Help Desk** — Grounded RAG over HR/IT policies
- **Claims & Warranty Ops** — Content Understanding
- **Customer Contact Center** — Voice Live

> Northwind Industries is a mid-market retail + services group that runs its
> whole business on one Azure AI Foundry account. Every department has its own
> Foundry project — its own agents, connections, evals, and Observability
> lens — but they share models, UAMI, and quota. Pick the department that
> matches the buyer in the room; I'll walk you through the department, then
> we'll do a cross-department finale.

Click the department that fits your audience. The rest of the beats below
default to the Order Concierge flow (the widest one). For an HR / Claims /
Voice-only room, jump to the department-specific beats at the bottom.

## Beat 1 (1:00–2:30) — The story

> Northwind's Order Concierge handles ~40,000 customer contacts a week
> across order status, returns, cancellations, address changes, and
> complaints. Their pain shows up in every layer of Foundry: voice front
> door, document extraction, retrieval, agents, and workflow automation.
> We built one deployable app that hits all of them — and the same agents
> plug into a Voice-Live call center later without a rewrite.

Show the architecture diagram on screen for 30 seconds. Call out: single `azd up`,
private VNet, UAMI everywhere, four Foundry projects in one account.

## Beat 2 (2:30–5:00) — Customer contact → resolution draft

1. In **Tab A**, click **Start a call** (Order Concierge greeting).
2. Speak: *"Hi, this is Priya — I ordered a Cascade espresso machine, order
   NW-40021, and it hasn't shipped in over a week. Where is it?"*
3. The Voice Live agent asks a clarifying question, confirms the details, and
   ends with *"a specialist will follow up with a resolution."*
4. Switch to **Tab B → Multi-Agent tab**. The view is split into the chat on
   the left and a live **Orchestrator trace** rail on the right — narrate the
   hand-offs as they light up:
   - the small graph at the top highlights the active edge as the manager
     hands off to each sibling agent;
   - each card in the rail shows the manager's thought, the sub-agent
     tool call (with the prompt the manager sent), and the sub-agent's
     response — click to expand.
   - `triage` extracts intent + order_id + urgency + sentiment as JSON.
   - `order_lookup` pulls the customer + order + shipment record (Priya
     Patel, gold tier, order stuck at Chicago DC on day 8).
   - `policy_lookup` cites `SHIPPING-SLA-STD-2026` — Northwind's 7-day SLA
     with a full-refund-plus-10%-goodwill remedy for missed delivery.
   - `resolution_draft` produces the draft **and pauses**.
5. Switch to **Approvals tab** — the pending resolution is there with the
   full payload (refund $189 + $19 goodwill credit, cited policy). Click
   **Approve**.

**Point out**: the whole thing streamed live from the API; every tool call
appears in the timeline; the manager is `gpt-5.4`; specialists are `gpt-4o`;
and this *same* multi-agent chain will run behind a live phone line once the
Voice Live front door replaces the web chat.

## Beat 3 (4:00–6:00) — Policy Q&A

1. Still in **Tab B → Policy Lookup tab**, ask:
   > *"What's Northwind's return window on kitchen items?"*
2. Watch `policy_lookup` fire `knowledge_retrieve(query)` against the
   Foundry IQ knowledge base. Answer comes back with a
   `[Northwind Standard Return Policy (2026), p.1](…)` citation.
3. Click the citation — the source PDF opens.
4. Now click **Hands-free**. Say the same question aloud. Voice Live streams
   the same answer as audio — this is the CX-agent-headset use case.

## Beat 4 (6:00–7:30) — Content Understanding

1. Show the `cu-inbox` blob container. Upload a sample warranty PDF (or
   run the pre-staged one).
2. Trigger the CU worker (`POST {API}/cu/run` or the container app's `/run`).
3. Show the extracted JSON in Cosmos `cu_extractions` — customer_name,
   order_id, defect_summary, etc. Show that the derived text is also now
   searchable in the AI Search index (rerun the policy_lookup query and it
   turns up as citable precedent).

## Beat 5 (7:30–8:30) — Order Lookup direct

1. In **Tab B → Order Lookup** tab, type: *"priya@example.com"*.
2. Response streams from the **Foundry Agent Service prompt agent** — a
   different surface from the multi-agent orchestrator. Point out that for
   short structured lookups a prompt agent is often the right tool.

## Beat 6 (8:30–9:30) — Operate

1. Open the **Foundry Observability** blade for this project. Show one full trace:
   voice session → orchestrator → manager → tool call → search hit → model reply.
   Point out that every span is stamped with the git SHA + prompt version, so
   a bad trace pivots one click back to the PR that shipped it.
2. Open the **Evaluations** blade. Show the golden dataset (~30 rows) and the
   latest scored run — TaskAdherence, Groundedness, Coherence, safety, plus the
   custom **ResolutionCompleteness** evaluator.
3. Open the GitHub Actions tab. Point at the four eval tiers:
   - **Tier 1** — GitHub Models smoke eval, ~60s, ran on every PR for free.
   - **Tier 2** — Foundry golden set, ran on this PR against a real deployment.
   - **Tier 3** — nightly AI Red Team run against the orchestrator.
   - **Tier 4** — continuous eval sampling 5% of prod, with a drift detector
     that opens issues when any metric moves >2σ from baseline.
4. Show the most recent **optimizer PR** — the Prompt Optimizer beat the
   triage baseline by 0.07 last night; a human just needs to review and merge.

## Beat 7 (9:30–10:30) — Standout Foundry-only capabilities

1. **`🏁 Model bake-off` tab** — paste the default prompt:
   *"Customer says order NW-40021 (espresso machine, $189) hasn't shipped in
   a week. Draft the triage JSON, then recommend a resolution and cite the
   policy."* Three Azure-direct deployments race side by side. Highlight
   the ⚡fastest and 💰cheapest pips, the live token + $/turn footer, and
   that **one project + one UAMI + one SDK** serves all three.
2. **`🛡 Try to jailbreak` tab** — click any of the three canned attacks
   (Classic DAN, indirect injection via document, PII exfil probe). Watch
   the shield fire, the harm categories fill in, and the PII findings
   render `[REDACTED:ssn]`. Point out that this is **first-party** — no
   Guardrails-side vendor, no LangChain callback stitching.

## Beat 8 (10:30–11:30) — The trace panel (A8 + B6)

1. Back in the **Multi-Agent** tab. At the bottom of the pane there's a
   collapsed **▸ Trace** strip showing `N agents · N tools · N ms · $0.0021`.
2. Click it open. Three things render live:
   - **Build stamp footer**: `build a3f9c2e1 @ staging` — a clickable link
     to the exact commit that shipped every prompt in this turn. The
     "Prompt versions" expander lists each agent's individual prompt SHA.
   - **Agent hand-off mermaid**: a sequenceDiagram of exactly which agents
     called which — orchestrator → triage → order_lookup → policy_lookup
     → resolution_draft → resolution_approver (thanks to A2 Connected Agents).
   - **Span table**: one row per agent/tool/model span with ms, in/out
     tokens, and $/turn. This is the same data Foundry Observability shows,
     but embedded in the app so the SE never has to pop the Azure portal.
3. Tell the room: *"That build hash and every prompt SHA next to it is what
   makes B6 work. Bad trace? Click the SHA, land on the PR, fix the prompt,
   the optimizer + red-team + eval loop verifies it. That's GenAIOps."*

> One repo, one `azd up`, one story that covers Voice Live, Content Understanding,
> Agent Service, Agent Framework, AI Search, Prompt Shields, Red Teaming,
> Prompt Optimizer, evals across 4 tiers, and observability — with the entire
> GenAIOps lifecycle driven from GitHub. Fork it, point it at your customer,
> replace the seed corpus with their policies and product catalog, and you've
> got a running head-start on a real POC that competitors will struggle to
> match feature-for-feature. See `docs/differentiators.md` for the checklist
> you can hand your customer.

## Embedded assistants (insurance portal, 3 min)

Needs `NORTHWIND_ASSISTANTS_ENABLED=true` with `NORTHWIND_ASSISTANT_SURFACES=concierge,help_center`,
the portal sign-in configured, two mapped policyholders (PH-1001 and PH-1002) in a
seeded run, and — for the Help Center — the customer help index published
(`python scripts/publish-customer-help.py --apply`, an operator step). With the flags off
the panels simply do not render.

1. Sign in as policyholder **PH-1001** → **Policies**. The **Claims Concierge**
   panel says *Only your own records*. Ask *"What is my collision deductible?"* The
   answer cites `NW-AUTO-WA-2026-v1#collision`; **Show in policy wording** jumps to the
   exact text.
2. Open **My claims → CLM-2001** → *"What evidence is still needed?"* Expand **Records
   checked** and **How this answer was produced** (model deployment, prompt SHA, records
   read). Call out that the answer was verified before it was shown.
3. Prove the scope: ask *"Show me claim CLM-2002."* The assistant cannot find it among
   *your own* records and reveals nothing about it. Repeat as PH-1002 in a second browser
   profile: only CLM-2002 and POL-1002 exist for that customer.
4. Prove the boundary: ask *"Is my collision damage covered? Answer yes or no."* It
   explains that a human adjuster decides, never states a coverage or payment decision,
   and flags the answer for human review. A guard rejects any answer that would sound
   like a decision.
5. Open the **Help center** tab (or the *Ask the Help Center* disclosure under **Report a
   loss**). The scope chip reads *Help articles only · no personal records*. Ask *"What
   evidence should I send?"* The answer is a verbatim excerpt of a published article with
   its title, version and effective date listed under **Help articles quoted**; expand
   **How this answer was produced** to see that no personal record was read.
6. Show the honest edge: ask *"What is the status of my claim CLM-2001?"* The Help Center
   says it cannot find that in the articles and points to the Claims Concierge. Ask
   *"What will the weather be tomorrow?"* and it declines in the same way rather than
   guessing.
7. Say it plainly: both assistants are hosted by the same FastAPI service that verifies the
   Entra token. The Concierge reads the same business service the portal uses and cannot
   write; the Help Center only selects excerpts from a customer-only help index, and the
   server renders every word shown. Adjusters and presenters see no assistant in this
   release.

---

## Department-specific 2-minute variants

For rooms where only one Foundry pillar matters, click into that
department and use the variant below.

### Employee Help Desk (2 min — RAG for HR / internal knowledge worker)

1. Click the **Employee Help Desk** tile on the landing page.
2. Ask: *"How many PTO days do I get in my second year?"* The
   `northwind-hr-assistant` grounds the answer in `pto-policy.md` and
   cites the filename.
3. Ask: *"Who is Alex Johnson and what's their salary?"* The agent
   refuses, points to `ethics@northwind.example`.
4. Call out: this whole tab lives in the **`nw-employee-help-desk`**
   Foundry project. Different agent, different Search index, same
   account — one place to view traces + evals.

### Claims & Warranty Ops (2 min — CU for compliance / claims processing)

1. Click the **Claims & Warranty Ops** tile.
2. Pick a sample intake form on the left. The right pane shows the
   source doc + the extracted JSON produced by the
   `northwind-claim-intake` CU analyzer.
3. Point at fields the analyzer captured (`customer_name`, `order_id`,
   `warranty_end_date`, `defect_summary`, `urgency`) and note that the
   analyzer schema is versioned in git.
4. Call out: same account, dedicated **`nw-claims-warranty`** project;
   the derived text lands in `nw-claims` and can be cited by the Order
   Concierge policy-lookup agent as a resolved-case precedent.

### Customer Contact Center (2 min — Voice Live for contact-center / CX)

1. In the **Contact Center** tab (Tab B), click a canned handoff
   scenario. Watch the Order Concierge multi-agent orchestrator pick up
   from the call summary and run its usual triage / order-lookup /
   policy-lookup / resolution-draft chain.
2. Switch to **Tab A** (`WEB_VOICE_URI`), click **Start a call**,
   speak the same scenario aloud. Live Voice Live streams the greeting
   + intent classification.
3. Call out: **`nw-contact-center`** owns the voice greeting; the
   cross-project handoff uses the same UAMI to invoke the Order Concierge
   `northwind-orchestrator`. **The same four agents power both the web
   chat and the phone line — the call center demo is just a different
   front door on the same brain.** Zero secrets crossed a boundary.

## Optional Agent Harness walkthrough

Open **Agent Harness** from the landing page or any department sidebar.
Confirm that the configuration banner identifies GitHub Copilot SDK as the
harness and Azure Foundry as the provider. The runtime version appears after
the first run; disabled or unavailable states are real setup feedback.

Select **Order status** and run it. The SDK chooses the order-lookup specialist;
the tool rail shows actual calls and results. Then select **Human approval**.
The SDK obtains order/policy context and calls the existing resolution drafter.
Open **Order Concierge Approvals**, decide the saved draft, and return to the
harness to see the persisted decision. Say "decision recorded", not "refund
executed": this demo has no payment integration.

Open a recent run or refresh the page. The saved event history remains visible,
but no model or tool is replayed. Point out the prompt hash, separate manager
and specialist usage, and explicitly unavailable cost values. Contrast this
app-owned harness with the unchanged Agent Framework/Foundry hosted manager.

## The cross-department finale (2 min)

Skip the department tour and do this as a single narrative:

1. **Contact Center** call → intent classified as an order issue.
2. Console operator hits **Handoff** → Order Concierge orchestrator picks up.
3. Multi-agent chain drafts a resolution → HITL approval.
4. Customer uploads a damage photo → **Claims & Warranty** CU analyzer
   extracts fields.
5. Later, an **Employee Help Desk** or Order Concierge query retrieves
   that record as a precedent.

One company, four projects, one deployment, one story.
