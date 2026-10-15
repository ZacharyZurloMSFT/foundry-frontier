# Northwind Insurance Policyholder Contact Center — seed corpus

Version 1.0, effective 2026-01-01, fictional Washington/Colorado auto procedures.
Not legal advice or a coverage decision. Converted Voice Live and intent
classification can use this corpus after authorised rollout and indexing into
`nw-contact-center` Azure AI Search index.

## What's here

| File                       | Purpose                                                    |
| -------------------------- | ---------------------------------------------------------- |
| `call-scripts.md`          | FNOL, claim status, evidence, policy questions and human handoff |
| `ivr-flow.md`              | The IVR decision tree for phone / voice inbound            |
| `escalation-playbook.md`   | When and how to bump a call to a human supervisor          |
| `intent-taxonomy.md`       | Canonical list of intents the classifier routes to         |
| `deflection-faq.md`        | Versioned policyholder FAQ with human-review boundaries |

The **Claims Concierge / adjuster handoff** on the Contact Center console
posts the running call summary to `POST /handoff` on the API. The API
starts a converted `northwind-orchestrator` run in the stable `order-concierge`
department (see `department.yaml` handoff_target). The web UI never starts
voice or sends a handoff until `/insurance/fixtures` reports agent readiness.
No claim approval/denial, real assignment, payment or external communication
is executed. No callback or staffing guarantees are implied.
