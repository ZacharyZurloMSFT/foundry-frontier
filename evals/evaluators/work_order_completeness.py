"""Custom evaluator: ResolutionCompleteness (module + function name kept as
`work_order_completeness` for backwards compatibility with the CI harness).

Scores a resolution-draft response on:
  - all 7 required fields present
  - amount_usd is a plausible number for the resolution_type
  - resolution_type is in the allowed vocabulary
  - urgency matches the P1..P4 vocabulary
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

REQUIRED = [
    "order_id", "customer_id", "resolution_type",
    "amount_usd", "reason", "policy_ref", "urgency",
]
URGENCY_VOCAB = {"P1", "P2", "P3", "P4"}
RESOLUTION_TYPES = {
    "refund_full", "refund_partial", "reship", "rma",
    "store_credit", "address_change", "cancel", "goodwill_credit",
}


def _find_draft(response_text: str) -> dict | None:
    if not response_text:
        return None
    try:
        obj = json.loads(response_text)
        if isinstance(obj, dict) and ("resolution_type" in obj or "order_id" in obj):
            return obj
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{[\s\S]*\}", response_text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def evaluate(*, response: str, **_kwargs: Any) -> dict[str, Any]:
    draft = _find_draft(response) or {}
    # `policy_ref` is legitimately allowed to be null when the drafter
    # couldn't find a matching policy — count it as present when the key
    # exists (even if the value is None).
    per_field = {
        f: (f in draft) if f in {"policy_ref", "amount_usd"} else bool(draft.get(f))
        for f in REQUIRED
    }
    present = sum(per_field.values())
    score = present / len(REQUIRED)
    plausible = True
    amount = draft.get("amount_usd")
    if not (isinstance(amount, (int, float)) and not isinstance(amount, bool)
            and math.isfinite(amount) and 0 <= amount <= 10_000):
        plausible = False
    if draft.get("resolution_type") not in RESOLUTION_TYPES:
        plausible = False
    if draft.get("urgency") not in URGENCY_VOCAB:
        plausible = False
    if not plausible:
        score = max(0.0, score - 0.15)
    return {
        "work_order_completeness": round(score, 3),
        "fields_present": per_field,
        "plausible": plausible,
    }
