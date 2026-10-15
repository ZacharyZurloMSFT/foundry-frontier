"""Seed Cosmos with a couple of demo records so the tech app has something to show."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "apps" / "api"))

from app.repositories import append_message, enqueue_hitl, save_workflow  # noqa: E402


async def main() -> None:
    demo_session = "demo-session"
    await append_message(
        demo_session,
        "user",
        "Order NW-40021 (my espresso machine) hasn't shipped in a week. What can you do?",
    )
    await append_message(
        demo_session,
        "assistant",
        "I see the order is stuck at the Chicago DC. Northwind's shipping SLA "
        "commits to 7 days; you're at day 8. I've drafted a full refund plus a "
        "goodwill $20 credit — pending supervisor approval.",
        meta={"agents_used": ["triage", "order_lookup", "policy_lookup", "resolution_draft"]},
    )

    wf_id = f"wf-{demo_session}"
    await save_workflow(wf_id, {"sessionId": demo_session, "state": "awaiting_hitl"})
    await enqueue_hitl(
        wf_id,
        "resolution",
        {
            "order_id": "NW-40021",
            "customer_id": "C-8801",
            "resolution_type": "refund_full",
            "amount_usd": 189.00,
            "reason": "Shipping SLA breached (day 8 of 7-day commitment on order NW-40021).",
            "policy_ref": "SHIPPING-SLA-STD-2026",
            "urgency": "P2",
        },
    )
    print("Seeded demo conversation + one pending HITL resolution.")


if __name__ == "__main__":
    asyncio.run(main())
