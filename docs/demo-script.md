# Northwind Insurance — 10-minute demo script

The whole demo runs in **one browser tab** against one deployment of this repo:
the Northwind Insurance portal (`WEB_TECH_URI`). A second browser profile holds
the adjuster for Beat 6. AI is embedded in the product where the work happens;
there is no separate "AI workspace" and no department picker.

> **Not in this demo any more.** The department shell (department tiles, the
> multi-agent chat, Approvals, Model bake-off, Safety lab, trace panel and Agent
> Harness), the customer voice front door and the unauthenticated endpoints
> behind them were deleted. If an audience saw an earlier version, say so and
> show what replaced them: identity-bound assistants and a case workbench where
> a person decides.

## Before you start

Sign-in, the assistants and evidence upload are all **disabled by default** and
are operator steps. Nothing in the repo verifies a live Entra tenant, model
deployment or Search index for you, so rehearse once against your seeded run.

| Needed for                  | Operator step                                                                                                                                                                                                                                                                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Sign-in (Beats 1–6)         | Entra registrations from `scripts/configure-insurance-identity.py`, the build-time `VITE_INSURANCE_*` settings, `NORTHWIND_BUSINESS_DOMAIN=insurance` and `NORTHWIND_BUSINESS_API_ENABLED=true` (see the [README](../README.md#insurance-portal-with-entra-sign-in)). Without them **My account** says it is unavailable and no demo account is substituted. |
| Roles and records           | A seeded run (`scripts/seed-insurance-run.py`) that maps your test users: two policyholders (**PH-1001** Avery Example and **PH-1002** Jordan Sample, both fictional), an adjuster and, optionally, a presenter. The server decides the role; the browser cannot.                                                                           |
| Assistants (Beats 2, 4, 5)  | `NORTHWIND_ASSISTANTS_ENABLED=true` and `NORTHWIND_ASSISTANT_SURFACES=concierge,help_center`, a Foundry project endpoint and model deployment the API's identity may call, and Cosmos. The Help Center also needs its index: `python scripts/publish-customer-help.py --apply`. With the flags off the panels simply do not render.        |
| Evidence upload (Beat 4)    | `NORTHWIND_EVIDENCE_ENABLED=true`. Automatic extraction also needs the evidence worker and Content Understanding; manual field review needs no model.                                                                                                                                                                                      |

A seeded run is bounded and has no reset yet: at most 100 claims, and eight
evidence documents per run (four per claim). Rehearse the write beats sparingly.
Model wording varies from run to run; the server-enforced parts (scope,
citations, no decisions) do not.

## Beat 0 (0:00–1:00) — A front door, not a chatbot

Open the portal home page; no sign-in is needed. Point at the **Synthetic
demonstration** banner, the headline *Here for the road ahead.*, the card
*A report is the beginning. Not a decision.*, and the trust strip (*Human-led*,
*Plain-English policy wording with citations*, *Secure Microsoft Entra sign-in*).
Click **Coverage information** and **Claims help**: public, general information
only, with no automated coverage or payment decisions.

> Northwind Insurance is a fictional auto insurer whose people answer the same
> three questions all day: *what does my policy say, what do you still need from
> me, and what happens next?* We did not bolt a chatbot onto the side. The portal
> is the product. AI sits inside the pages where those questions come up, sees
> only what the signed-in person may see, and never decides a claim.

Optional, 30 seconds: the sequence diagram in
[architecture](./architecture.md#identity-bound-embedded-assistants). One FastAPI
service verifies the Entra token, resolves the caller's role and policyholder
from the run mapping, and binds the model's tools to that caller.

**Presenter note**: the home page's *People when you need them* card says
adjusters use AI to prepare cases faster. No staff-facing assistant exists in this
release (Beat 6), so do not demo or promise one.

## Beat 1 (1:00–2:00) — Sign in: the server decides who you are

1. Click **View my policy**, then **Sign in with Microsoft**. The page shows
   *Signed in as …* and a strip: *Server-authorized role: policyholder*, the run
   and the source version.
2. Say: the role and the policyholder come from a server-side mapping of the
   verified token. The browser cannot pick or change either, and the token stays
   in memory. The tabs are **Policies**, **My claims**, **Report a loss** and
   (when enabled) **Help center**.
3. Open **Policies**, then **View schedule and wording** on **POL-1001**
   (*Fictional 2022 passenger sedan*). Show the coverage schedule (collision
   $30,000 limit and $500 deductible), the rental reimbursement endorsement and
   **Read policy wording and source references**.

**Point out**: the table is captioned *Scheduled limits and deductibles, not a
coverage determination*. The portal never says "you're covered".

## Beat 2 (2:00–4:00) — Ask the Claims Concierge about the policy

1. With POL-1001 open, the panel above the schedule reads **Ask about this
   policy** with the badge *Only your own records · POL-1001*. Ask *"What is my
   collision deductible?"* (or click a suggested question). Expect a short
   plain-English answer ($500 on the schedule) with **Policy wording cited**,
   typically the collision section `NW-AUTO-WA-2026-v1#collision`. Expand it and
   click **Show in policy wording**: the portal opens the wording and scrolls to
   the cited section. The citation text is rendered by the server from the policy
   snapshot it read, not by the model.
2. Expand **How this answer was produced**: the model deployment, the prompt
   version (the first 12 characters of the prompt file's SHA-256), how many
   records were checked and whether human review is recommended.
3. Prove the boundary. Ask *"Is my collision damage covered? Answer yes or no."*
   Expect the assistant to say a human adjuster decides and to flag the answer
   (*A person needs to review this. The assistant cannot decide coverage or
   claims.*). If a draft ever sounded like a decision, the server withholds it and
   shows *No answer was shown* with a reference code instead. Either outcome is
   by design; a plain "yes" never appears.
4. Say it plainly: the Concierge has four read-only tools (list and get policy,
   list and get claim), all bound to the signed-in policyholder. It cannot write,
   it cannot name another customer's record, and the server rejects any answer
   that cites wording it was not shown or reads like a coverage, claim or payment
   decision. That is also why an answer arrives whole rather than token by token:
   nothing is shown until every check has passed.

## Beat 3 (4:00–5:15) — Report a loss: a report is the beginning

1. On the open policy click **Report a loss on this policy**. The form is
   **Report an auto loss**: the policy is preselected; choose *Auto hail*, a
   recent loss date and, to show a review flag, the state `OR` (the policy is
   `WA`); describe it in a sentence of synthetic facts, tick the synthetic-data
   acknowledgement and click **Submit loss report**. (Under the form, a collapsed
   **Questions about reporting a loss? Ask the Help Center** cannot see anything
   you type.)
2. You land on the new case with *Loss report received — claim CLM-…*. The status
   is *reported*, the timeline reads *Loss report received. A human will review
   the case.* and **Review flags** lists *loss jurisdiction mismatch*.
3. In **Ask about this claim**, click *Why does my claim show a review flag?*
   Expect an explanation that the flag is recorded for a human reviewer and is not
   a denial.

**Point out**: review flags (loss date outside the policy, loss state different
from the policy, wording or endorsement periods) are computed by the server and
recorded. They never become an automatic denial. Submitting is safe to retry:
the portal keeps only an idempotency key and a report hash in session storage,
never the description or the token, and it never resubmits on its own.

## Beat 4 (5:15–6:30) — Cases, evidence and documents

1. Open **My claims**. The list shows the claim you just reported and the seeded
   **CLM-2001**. In the panel above the list (*Ask about your policies and
   claims*) ask *"Where do my claims stand?"*; the steps it took appear first
   (*Looked up your claims*, done). Then ask *"Show me claim CLM-2002."* The step
   reads *Record not found among your records* and the answer reveals nothing
   about that claim: another customer's record is indistinguishable from a
   missing one. (Optional: repeat as **PH-1002** in a private window. Only
   POL-1002 and CLM-2002 exist for that customer.)
2. Open **View case CLM-2001**. **Next evidence steps** lists what is outstanding.
   In **Ask about this claim** (*Only your own records · CLM-2001*) click *What
   evidence is still needed?* The answer comes from the same customer view you
   see on screen; internal notes and adjuster ids are never part of it.
3. In **Documents and evidence review**, open **Attach synthetic evidence**:
   choose *Allowlisted synthetic sample*, pick a sample, tick the synthetic-data
   acknowledgement and click **Store document and queue processing**. Then
   **Inspect** the stored document: it shows the source SHA-256, and **Download
   verified original** re-checks type, size and hash in the browser before it
   offers the file.
4. If the evidence worker and Content Understanding are enabled, *Processing:*
   moves from queued to completed; the extracted values are shown only to the
   adjuster, as untrusted suggestions (Beat 6). If they are not enabled, the panel
   says *Automatic extraction is paused…* and manual review still works with no
   model. A customer only ever sees **Human-reviewed facts**.

## Beat 5 (6:30–7:30) — Help Center: published answers only

1. Open the **Help center** tab. The scope badge reads *Help articles only · no
   personal records*. Click *What evidence should I send?* The answer starts *Here
   is what the Help Center says:* and continues with verbatim quotes, each followed
   by its article title. **Help articles quoted** lists title, version, effective
   date and article id (the evidence checklist is `HELP-EVIDENCE-001`).
2. Expand **How this answer was produced**: *Personal records read: None*. Say:
   the model only **selects** excerpts from twelve fictional customer help
   articles; the server checks that each is an exact substring of a retrieved
   article and renders every word shown. The articles are source-controlled in
   `data/northwind/departments/employee-help-desk/seed/customer/`.
3. Show the honest edge. Ask *"What is the status of my claim CLM-2001?"* The
   Help Center says it could not find that in its articles, will not guess, and
   points to the portal or Northwind Claims; the tab's banner points to the Claims
   Concierge. Ask *"What will the weather be tomorrow?"* or *"Will my claim be
   approved?"* and it declines the same way instead of guessing.

## Beat 6 (7:30–8:45) — The adjuster's view: a person is in control

1. In the second browser profile, sign in as the adjuster. The strip reads
   *Server-authorized role: adjuster*. The tabs are **Policies** and **Claim
   queue**: no **Report a loss**, no **Help center** and no assistant panels.
   Staff-facing assistants are not built yet, and a presenter account is
   read-only on top of that (*Presenter access is read-only. No case commands are
   available.*).
2. Open **CLM-2001**. The workbench shows the assigned adjuster, **Evidence
   tasks**, **Case notes** and a **Case timeline** where every event shows its
   version, before and after.
3. Under **Adjuster actions**, use **Move workflow** to take the case from
   *evidence requested* to *evidence received* with a reason, then **Add case
   note** twice: once *Internal — employees only*, once *Customer-visible in this
   portal*. Each button names the case version it applies to; the server checks it,
   and after a conflict or an uncertain result the actions pause until the case is
   read again. Nothing is resubmitted for you, and saving a note does not email,
   text or contact the policyholder.
4. Read the stages aloud: *reported → evidence requested → evidence received →
   adjuster review → customer update prepared*. There is no approved, denied or
   paid stage, on purpose. The panel says it too: *You are not authorizing
   coverage, settlement, payment, or an external communication.*
5. Open the document attached in Beat 4 and use **Compare suggestions with the
   original source**. Six fields (claim id, policy id, policyholder id, loss date,
   estimate total, invoice total) each show the *Untrusted extracted suggestion*,
   any *Available confidence* and the *Human-confirmed value*. With extraction off,
   each suggestion reads *Missing — manual entry is available* and the adjuster
   types the value from the source. The adjuster accepts, corrects or clears each
   field separately after inspecting the original; the raw extraction is never
   overwritten.
6. Back in the policyholder profile click **Read current case**: **Case updates**
   shows only the customer-visible note, the timeline shows the new stage, and the
   internal note and the adjuster's id are nowhere. Optional: ask the Concierge
   *"What did the adjuster write in the internal notes on my claim?"* It only
   receives the customer-visible projection, so it cannot repeat them.

## Beat 7 (8:45–10:00) — What keeps it honest: GitHub, CI and evals

Open the repo's **Actions** tab (the lifecycle diagram is in
[genaiops](./genaiops.md)).

1. **CI** (`.github/workflows/ci.yml`) runs on every PR and on `main` with no Azure
   credentials and no model calls: ruff and mypy on the business and assistant
   packages, a Bicep compile, the API and release contract tests, and the web
   build, typecheck and tests. That includes the **deterministic assistant golden
   eval**: 60+ rows in `data/golden/assistant.jsonl` (Concierge scope, internal
   data, determination bait and prompt injection; Help Center grounding, no-match,
   personal-record and injection rows) are replayed through the real routes with a
   scripted model and search. The grader must pass honest answers and catch every
   leak class: another customer's data, an internal marker, altered citations,
   decision language, a non-verbatim Help excerpt. Run it offline with
   `python evals/assistant_eval.py --self-check`. Be precise: this proves scoping,
   guards and rendering; it is not a measurement of live model quality.
2. **Provenance**. `curl https://<API_URI>/build` returns the git SHA and a SHA-256
   per assistant prompt (`prompt_sha.assistant-concierge`,
   `prompt_sha.assistant-help-center`); the prompt version in each answer is the
   first 12 characters of the same hash. Each turn emits an `assistant.turn` span
   and one `AI-conversation` log line. The line carries metadata only: the
   assistant surface, a department stamp, a one-way session reference, the outcome
   and the tool-call count, never the question or the answer.
3. **Release**. The scoped app release (`deploy-apps.yml`) runs only after CI
   passes on `main`, pins images by digest and stamps the API with the source SHA
   that `/build` reports. Infrastructure goes through a plan and apply pair
   (`deploy-infra.yml`).
4. **Be upfront about the other tiers.** Of the original four eval tiers, Tier 1
   (a GitHub Models smoke eval, five micro-cases against the prompts in
   `agents/*.yaml`, no Azure credentials) and Tier 3 (Foundry AI Red Team against
   the hosted `northwind-orchestrator` agent) are still defined, along with the
   prompt optimizer. Tier 2 (the Foundry golden set on PRs) and Tier 4 (continuous
   eval and the drift detector) were retired with the endpoints they exercised.
   Those workflows stay disabled until configured and budgeted, and
   `evals/baseline.json` is empty on purpose: no measured numbers are published
   until a reviewer approves a live run. As pinned today, the Tier 3 and optimizer
   pipelines install `azure-ai-evaluation==1.1.0`, which has no Red Team API or
   optimizer, and their runners exit 0 with a stub report when the SDK is missing,
   so never present a green run from them as a result. See
   [differentiators](./differentiators.md#eval-tiers-what-exists-now).

> One repo, one deployment, one story: a portal where AI is embedded, scoped to
> whoever is signed in and fails closed; a case workbench where a person decides
> every claim and confirms every extracted fact; and a GitHub pipeline that tests
> those guarantees on every PR. Fork it, replace the synthetic records and help
> articles with your customer's, and you have a running head-start on a real
> proof of concept. See [`differentiators.md`](./differentiators.md) for the
> checklist you can hand your customer.

## Audience variants

- **Five-minute cut**: Beats 0, 1, 2 and 5.
- **Risk, compliance or security room**: spend the time on Beat 2 and the scope
  proof in Beat 4. Ask for another customer's claim, for a yes/no coverage answer
  and, in the Help Center, for a personal record. Then open
  `data/golden/assistant.jsonl` to show the same attacks written as rows that CI
  grades on every PR.
- **Claims operations room**: Beats 3, 4 and 6. Report a loss, attach evidence,
  then work the case as the adjuster and show internal versus customer-visible
  notes.
- **Engineering or platform room**: Beat 7 at length. Walk `ci.yml`,
  `apps/api/tests/test_assistants_golden.py`, `agents/insurance/assistants/`
  (the prompt files are the source of truth for both assistants) and the output
  of `/build`.

## If a panel is missing

| You see                                                              | Why                                                                                                                                                                                  |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| *My account is unavailable*                                          | The `VITE_INSURANCE_*` sign-in settings are missing or invalid. No demo account is substituted.                                                                                      |
| No assistant panels and no **Help center** tab                       | Assistants are off, the surface is not in `NORTHWIND_ASSISTANT_SURFACES`, or you are signed in as an adjuster or presenter (the server lists surfaces per role).                      |
| *The Claims Concierge is not available right now.*                   | The surface is enabled but its configuration is incomplete (Foundry endpoint, Cosmos, packaged prompt; for the Help Center also the Search endpoint and index).                       |
| No **Attach synthetic evidence** form                                | `NORTHWIND_EVIDENCE_ENABLED` is off, or you are signed in as a presenter (read-only).                                                                                                |
| *Automatic extraction is paused…*                                    | The evidence worker or extraction is not enabled. Stored sources and manual field review still work.                                                                                 |
