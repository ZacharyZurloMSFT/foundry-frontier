"""Per-agent smoke eval inputs.

For agents with rows in data/golden/dataset.jsonl (triage, manual_retrieval /
policy-lookup, work_order_draft / resolution-draft) we reuse the golden
inputs. For agents without golden rows (tech_lookup / order-lookup,
orchestrator) we ship a small inline list of representative customer /
upstream prompts here.

Keeping these inline means the smoke eval runs even before a full golden set
lands for those agents.

NOTE ON LEGACY KEY NAMES: the constant names `TECH_LOOKUP_INLINE` and the
dataset key `tech_lookup` are legacy from the earlier Field Services
scaffold. Semantically they now feed the order-lookup smoke eval.
"""
from __future__ import annotations

TECH_LOOKUP_INLINE = [
    {
        "id": "order-lookup-001",
        "input": "Find order NW-40021.",
    },
    {
        "id": "order-lookup-002",
        "input": "What has priya@example.com ordered recently?",
    },
    {
        "id": "order-lookup-003",
        "input": "Look up any orders for customer C-7455.",
    },
]

ORCHESTRATOR_INLINE = [
    {
        "id": "orch-001",
        "input": (
            "Priya Patel (gold tier) says her Cascade espresso machine (order "
            "NW-40021) hasn't shipped in a week. Look up the order, ground the "
            "recommendation in policy, and draft a resolution."
        ),
    },
    {
        "id": "orch-002",
        "input": (
            "Customer Marcus Chen wants to return the cast-iron skillet from "
            "order NW-40022 — arrived cracked. Please handle end-to-end and "
            "draft a resolution."
        ),
    },
    {
        "id": "orch-003",
        "input": (
            "Sofia Alvarez wants to cancel order NW-40023 before it ships. "
            "Confirm the order isn't already shipped, cite the cancellation "
            "policy, and draft the resolution."
        ),
    },
]
