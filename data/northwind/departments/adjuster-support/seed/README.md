# Northwind Insurance Adjuster Support — seed corpus

Synthetic, versioned adjuster claims-handling procedures published to the
isolated `nw-insurance-adjuster-support` Azure AI Search index by
`scripts/publish-adjuster-support.py`. The API's `/adjuster-support/*`
routes retrieve from that index and return only exact, cited excerpts.

All content is fictional and written for a Washington/Colorado personal-auto
demo. It is not authoritative claims guidance.

## Contents

| File | Purpose |
| --- | --- |
| `adjuster-procedures.json` | Four versioned procedures (duty and review boundaries, evidence intake, escalation, privacy) published to the support index |
| `adjuster-review-boundaries.md` | Adjuster review procedure NW-CLAIMS-HANDLING-2026-V1 section 4; rendered into the flagship claims knowledge corpus by `data/seed/generate.py` |

The procedures cite NW-CLAIMS-HANDLING-2026-V1, version 1.0, effective
2026-01-01. They require a refreshed index before retrieval reflects any edit:
run `python scripts/publish-adjuster-support.py` to preview and `--apply` to
publish. The publisher never deletes documents.

No agent approves or denies coverage, assigns a real adjuster, sends
messages or pays claims; support answers are read-only and human reviewed.
