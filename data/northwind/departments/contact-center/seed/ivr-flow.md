# Northwind Insurance — Voice-First Intake Flow

**Source:** NW-VOICE-FLOW-2026-V1. **Version:** 1.0.
**Effective:** 2026-01-01. **Jurisdiction:** fictional Washington/Colorado auto demo.
No live staffing, callback guarantee or coverage determination is implied.

1. Verify insurance runtime readiness before opening a voice session.
   If unavailable, show the explanation and do not open a session.
2. Ask whether the caller is safe. Immediate danger → emergency guidance.
3. Capture free-form intent using NW-VOICE-INTENTS-2026-V1. Caller ID alone
   is not identity verification; do not expose records without authorisation.
4. FNOL → record identifiers, reported loss date/incident and evidence.
   Claim status → retrieve matching records or explicitly say unavailable.
   Policy question → cite applicable versioned sources and reserve judgment.
   Evidence → explain missing items, not a fictitious completed upload.
   Escalation → authorised human review; billing → human billing route.
5. With caller agreement, prepare a claim/policy/policyholder-linked summary
   for Claims Concierge and adjuster review. Cross-project handoff is gated
   until converted agents are ready.
6. Internal action approval is not insurance claim approval/denial, payment,
   a real assignment or an external communication. No autonomous execution.
