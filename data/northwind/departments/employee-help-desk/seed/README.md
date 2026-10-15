# Northwind Insurance Employee & Adjuster Support — seed corpus

Synthetic HR / benefits / IT-runbook documents indexed into the
`nw-hr` Azure AI Search index by the postprovision seeder. The
`northwind-hr-assistant` prompt agent grounds every answer in these docs
and cites the filename in its response.

All content is fictional but written to feel like a real mid-market
North-American fictional insurer's internal handbook. Do not treat as
authoritative HR guidance.

## Contents

| File                              | Category            | Notes                                        |
| --------------------------------- | ------------------- | -------------------------------------------- |
| `handbook-conduct.md`             | Code of conduct     | High-level company values + expectations     |
| `handbook-remote-work.md`         | Work policy         | Hybrid schedule, home-office stipend         |
| `pto-policy.md`                   | Benefits            | Accrual, carryover, blackout dates           |
| `parental-leave.md`               | Benefits            | Birth / adoption / partner leave             |
| `benefits-medical.md`             | Benefits            | Plan tiers, HSA, dependents                  |
| `benefits-401k.md`                | Benefits            | Match, vesting, provider                     |
| `expense-policy.md`               | Finance             | Per-diems, receipts, approvals               |
| `it-vpn-setup.md`                 | IT runbook          | Zero-trust VPN client install                |
| `it-laptop-refresh.md`            | IT runbook          | 3-year cadence, request workflow             |
| `it-mfa-reset.md`                 | IT runbook          | Self-serve + escalation                      |
| `security-training-schedule.md`   | Security            | Annual required training + phishing sim      |
| `manager-approval-workflow.md`    | Ops                 | Employee approvals plus versioned adjuster evidence/review procedures |
| `travel-policy.md`                | Finance             | Booking tools, allowable classes             |
| `holiday-schedule-2026.md`        | Benefits            | Company-observed holidays                    |
| `learning-development.md`         | Benefits            | Annual L&D budget + reimbursement            |

Original HR/IT documents are preserved. Added adjuster procedures cite
NW-CLAIMS-HANDLING-2026-V1, version 1.0, effective 2026-01-01, fictional
Washington/Colorado auto context. They require a refreshed corpus before retrieval.
Employee expense approvals are not insurance claim decisions; no agent
approves/denies coverage, assigns a real adjuster, sends messages or pays claims.
