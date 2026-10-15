# Manager Approval Workflow

**Doc ID:** OPS-610

Manager approval is required for the following actions. All approvals
route through Workday unless otherwise noted.

| Action                                    | Approver                | System            |
| ----------------------------------------- | ----------------------- | ----------------- |
| PTO ≥ 2 consecutive days                  | Direct manager          | Workday           |
| PTO during claims-review blackout         | Regional manager        | Workday           |
| Expense report ≤ USD 500                  | Direct manager          | Expensify         |
| Expense report USD 501 – 2,500            | Manager + VP            | Expensify         |
| Expense report ≥ USD 2,501                | VP + Finance controller | Expensify         |
| Home-office stipend claim                 | Direct manager          | Expensify         |
| International travel                      | VP                      | Concur            |
| New software purchase                     | IT + direct manager     | ServiceNow        |
| Role change or promotion                  | Skip-level + HR partner | Workday           |

## SLA

Managers are expected to action approvals within **3 business days**.
After 5 business days a request auto-escalates to the skip-level.

## Delegating during PTO

Before you take PTO of 3+ days, delegate your approval queue in Workday
› Delegations. Delegations expire automatically when the PTO ends.

## Adjuster procedures and claims-review boundaries

**Source reference:** NW-CLAIMS-HANDLING-2026-V1, section 4.
**Version:** 1.0. **Effective:** 2026-01-01. **Jurisdiction:** fictional
Washington/Colorado personal-auto demo procedures; not a statement of applicable law.
Northwind Insurance and all policyholders/claims are synthetic.

1. Confirm the policyholder, policy and claim identifiers match before
   accessing evidence. A caller ID or an email address alone does not
   authenticate a policyholder. Do not request an SSN or full card number.
2. Record the reported incident, loss date and source separately from
   verified observations. Identify the policy form and endorsement versions
   effective on the loss date; expiry today is not a coverage decision.
3. Request only necessary evidence: FNOL narrative, repair estimate,
   relevant invoices, damage photos and, where available, a report reference.
   Flag illegible handwriting, inconsistent dates and missing totals for
   human review. Photos do not prove causation, fault or fraud.
4. A draft may request evidence, propose adjuster assignment, prepare a
   policyholder update, escalate review or recommend a settlement amount.
   A proposed amount is not a payment instruction. All drafts require a
   human decision in the internal Approvals queue.
5. Approval of a demo draft records that review only. It does not approve
   or deny insurance coverage, change policy terms, send communications,
   execute payments or assign a real person. Licensed/authorised humans
   make final decisions in the insurer's systems using the complete file.
6. Escalate injury, immediate danger, legal/regulatory complaints, disputed
   coverage, ambiguous endorsements and suspected evidence tampering to
   an authorised specialist. Do not accuse a claimant of fraud.

Employee expense approvals above remain separate from insurance claims.
When citing this procedure, include its source reference, section, version,
effective date and fictional jurisdiction.
Procedural source IDs are distinct from action `policy_refs`, which must
use exact applicable canonical clauses: NW-AUTO-WA-2026-v1#collision /
NW-AUTO-WA-2026-v1#duties-after-loss for WA POL-1001, or
NW-AUTO-CO-2026-v1#comprehensive / NW-AUTO-CO-2026-v1#duties-after-loss
for CO POL-1002. Never substitute a generic procedure ID.
