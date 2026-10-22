# Northwind Insurance — Claims Document Intelligence corpus

All documents are fictional and versioned, effective 2026-01-01, for
an illustrative Washington/Colorado personal-auto demo. They are not binding policy
contracts, legal advice or coverage decisions.

Legacy filenames remain stable for compatibility, but their active content is:

| Existing file | Insurance source |
| --- | --- |
| `intake-form-CL-8817.md` | FNOL NW-FNOL-DEMO-2001; CLM-2001 / POL-1001 / PH-1001 |
| `intake-form-CL-8901-hw.md` | Itemised repair estimate and diagnostic invoice |
| `warranty-carrier-30gx.md` | Fictional WA wording NW-AUTO-WA-2026-v1, version 1, with exact clause citations |
| `warranty-trane-rtac.md` | Endorsement/evidence procedure NW-CLAIMS-HANDLING-2026-V1 |

The legacy `GET /claims/samples` / `POST /claims/extract` document viewer was
removed with the department shell. These sample documents are now reached through
the portal's evidence routes (`/api/v1/evidence/samples` and
`/api/v1/claims/{claim_id}/documents/samples`), where an adjuster compares the
source with untrusted Content Understanding suggestions and confirms each field.
An authored sample must never be portrayed as a live CU run. No `ca-cu-ingest`
worker is required for this API surface, and nothing here indexes documents
automatically or executes claims actions.

`../cu-analyzers/claim-intake.json` and the pipeline insurance schemas cover
FNOL, repair estimates, invoices and damage observations. Source references,
dates, identifiers, amounts and missing fields support human review.
Photos cannot definitively establish cause, fault or fraud; an estimate is
not proof of completed repairs and an invoice is not proof of payment.

`data/seed/generate.py --domain insurance` renders the active flagship
knowledge PDFs locally without model/cloud calls. Existing retail PDFs and
legacy schemas remain historical compatibility assets, not active UI examples.
