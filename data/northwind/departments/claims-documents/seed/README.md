# Northwind Insurance — Claims Document Intelligence corpus

All documents are fictional and versioned, effective 2026-01-01, for
an illustrative Washington/Colorado personal-auto demo. They are not binding policy
contracts, legal advice or coverage decisions.

| File | Insurance source |
| --- | --- |
| `fnol-demo-2001.md` | FNOL NW-FNOL-DEMO-2001; CLM-2001 / POL-1001 / PH-1001 |
| `repair-estimate-demo-2001.md` | Itemised repair estimate and diagnostic invoice |
| `auto-policy-wa-2026.md` | Fictional WA wording NW-AUTO-WA-2026-v1, version 1, with exact clause citations |
| `claims-handling-endorsements.md` | Endorsement/evidence procedure NW-CLAIMS-HANDLING-2026-V1 |

The web document view uses `GET /claims/samples` and `POST /claims/extract`,
showing source text alongside extracted JSON and reported provenance.
It must not portray an authored sample as a live CU run. The view does not
upload files, automatically index them or execute claims actions.

`../cu-analyzers/claim-intake.json` covers FNOL, repair estimates, invoices and
damage observations. Source references,
dates, identifiers, amounts and missing fields support human review.
Photos cannot definitively establish cause, fault or fraud; an estimate is
not proof of completed repairs and an invoice is not proof of payment.

`data/seed/generate.py --domain insurance` renders the active flagship
knowledge PDFs locally without model/cloud calls.
