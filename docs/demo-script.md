# Northwind Insurance demo script

## Before presenting

Use synthetic records only. The retained projects are `nw-insurance-claims`,
`nw-insurance-support`, `nw-insurance-documents`, and
`nw-insurance-contact-center` under the existing shared Foundry account.
The Orders/retail projects are retired, not fallback targets.

Open the existing web-tech portal and, if its independent transport checks and
flags are enabled, the web-voice front door. Check `/insurance/fixtures`:
`agent_runtime_ready` must be true before presenting live inference. A deployment
status of `active` or packaged source files alone do not prove readiness.
Keep unavailable states visible rather than substituting recorded answers.
The current portal defaults to the Entra-signed-in business experience. Enter
the AI workspace through the server-assigned adjuster/presenter role. The legacy
department shell is a separate explicit frontend opt-out, not a reason to relax
identity controls. UI role gating is not backend authorization on legacy AI routes.

The API fleet-ready guard is enabled only after a matching source/config/prompt/
serving-version verification receipt. It is not a flag on every hosted agent.
Business-portal identity, evidence uploads, live CU and Voice Live have their
own opt-in controls; this walkthrough does not authorize enabling them.

## One insurer, four departments

Show the available insurance departments in the authorized AI workspace (or
the explicitly selected legacy department shell):

- **Claims Concierge:** synthetic policy/claim records, five hosted agents and
  pending human-review drafts.
- **Employee & Adjuster Support:** grounded HR/IT and adjuster-procedure answers.
- **Claims Document Intelligence:** actual allowlisted synthetic PDFs or enabled
  uploads, live Content Understanding, confidence and source spans.
- **Policyholder Contact Center:** completed voice transcripts and explicitly
  confirmed insurance handoff.

The browser's legacy department keys remain stable for history and API
compatibility. Their project destinations are the four insurance projects.
They share model deployments, Search, telemetry and existing infrastructure;
project isolation is not a claim that every dependency is dedicated.

## Claims Concierge: records, grounding and draft review

1. Open Claims / Policies. Inspect synthetic claim `CLM-2001`, policy
   `POL-1001`, and policyholder `PH-1001`. Point out linked identifiers,
   loss date, jurisdiction, outstanding evidence and versioned policy references.
2. Ask: "For synthetic claim CLM-2001, policy POL-1001 and policyholder PH-1001,
   summarize the current status and outstanding evidence. Cite the applicable
   fictional Washington policy wording; do not make a coverage decision."
3. Follow actual tool/delegation events: insurance triage, policy/claim lookup,
   then grounded policy wording. The manager is
   `northwind-insurance-orchestrator`; all five hosted agents live in
   `nw-insurance-claims`.
4. Ask for a **request-evidence draft** for the outstanding photos, not a
   settlement or payment. Confirm that the tool persisted a workflow-bound
   `pending_approval` result before describing it as saved.
5. Open the human-review queue and inspect the linked identifiers, proposed
   action, references and `execution_status=not_executed`. A presenter may
   demonstrate a draft review only when that operation is separately intended.

Say: "A human reviews a proposed next step. Even draft approval does not approve
or deny coverage, execute payment, change a real claim or send an external message."
For a release-verification run, leave its single synthetic draft pending and
retain the workflow/draft IDs as evidence; do not approve, replay or delete it.

## Policy wording and adjuster support

Ask a wording question with the policy ID, loss date and jurisdiction. Require
citations from the insurance knowledge base, not a web search or model memory.
Show the `nw-insurance-claims-kb-mcp` connection targeting
`northwind-insurance-kb` and the `northwind-insurance-knowledge` index.

In Employee & Adjuster Support, ask about missing-evidence review procedures.
Point to the returned title, version and source attribution. This surface uses
the isolated adjuster-support index; it does not require creating an agent in
the otherwise empty `nw-insurance-support` project.

## Document intelligence

Select an allowlisted synthetic FNOL or repair-estimate PDF. If live CU is
enabled, show the real source bytes, typed extracted fields, confidence and
source spans. An estimate is not an invoice, an invoice is not proof of payment,
and a photograph does not establish cause, fault, fraud or coverage.

Missing source bytes or an unavailable analyzer must remain explicit errors.
Do not describe golden expected extractions as live results or claim that
documents are automatically indexed or used to mutate claim records.

## Voice and confirmed handoff

If native Voice Live is ready, speak: "I need an update on synthetic claim
CLM-2001 under policy POL-1001." Show only completed transcript text entering
the editable intake. Review the supplied identifiers, loss date, jurisdiction
and asset before explicitly confirming the handoff.

The `northwind-intent-classifier` prompt agent has insurance-specific
instructions in `nw-insurance-contact-center`. A physical microphone interaction
is a separate check from service-level transport smoke tests. The browser uses
an origin-bound application ticket, never a broad Azure credential.

## Operate and explain provenance

Show actual runtime metadata and the release evidence: source commit, uploaded
artifact/content hash, per-file manifest hash, selected canonical prompt hash,
normalized configuration hash, immutable agent version and serving selector.
A locally rebuilt ZIP differing from an old deployment is not itself proof
that the old prompt was wrong.

`scripts/verify-insurance-fleet.py` reads previously completed synthetic
responses and the persisted pending draft without starting more inference.
Guarded API activation verifies that this evidence still matches current
serving versions. A failed post-activation check disables inference rather
than restoring an old true flag that points at deleted projects.

Explain the four unchanged eval tiers: PR smoke, golden-set evaluation,
red teaming, and continuous evaluation. Historical approved packs/receipts
are immutable. New native evaluation runs, dataset uploads or training require
their own approval and verified spend bounds; a deployment smoke is not a
completed quality evaluation or production approval.

## Retained backend harness

The current portal intentionally removes the Agent Harness UI. Backend SDK
compatibility code and historical evidence remain, but this rollout does not
restore that UI or enable its independent flag. Reported token/cost values are
execution observations, not a complete Azure billing total. No part of the
demo authorizes autonomous insurance decisions.
