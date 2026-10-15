# Northwind Insurance — Intent Taxonomy

**Source:** NW-VOICE-INTENTS-2026-V1. **Version:** 1.0.
**Effective:** 2026-01-01. **Jurisdiction:** fictional Washington/Colorado auto demo.
Classification routes conversations; it never decides coverage or liability.

| Intent | When to select |
| --- | --- |
| `fnol` | Report a new auto loss, collision, theft or other incident. |
| `claim_status` | Ask about an existing claim or adjuster review status. |
| `policy_question` | Ask about policy dates, a deductible or an endorsement. |
| `evidence_submission` | Ask what evidence is missing or how to supply it. |
| `billing` | Premium/invoice questions; human billing route, no transactions. |
| `escalation` | Human request, injury, urgent safety, coverage dispute or complaint. |
| `other` | Unclear intent; clarify without guessing. |

Priority: escalation > fnol > claim_status > evidence_submission >
policy_question > billing > other. For immediate danger, give emergency
guidance before routing. Do not interpret frustrated language as fraud.
Return confidence in [0,1]; below 0.65 choose `other` and ask a brief
clarifying question. Missing policy/claim IDs remain null.
