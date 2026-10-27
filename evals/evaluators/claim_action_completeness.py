"""Custom evaluator: ClaimActionCompleteness (deterministic).

Scores the claim-action draft that `draft_claim_action` saves to the human-review queue
(the `payload` returned by GET /hitl) against the canonical synthetic fixtures:
  - the seven draft fields are present and well formed
  - claim, policy and policyholder IDs are linked in the fixtures
  - action_type is one of the five review-only actions (no deny / approve / pay action exists)
  - policy_refs are unique, applicable wording sections of the claim's policy
  - only a settlement_recommendation carries an amount: positive, finite, within the
    illustrative policy / endorsement limit, and it must cite wording
  - the summary makes no binding coverage, approval or payment statement

When the golden row supplies `expected`, the draft is also compared with it (`expected_match`).
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any

from ._insurance import FIXTURE, canonical_citations, unsafe_conclusion

REQUIRED = (
    "claim_id", "policy_id", "policyholder_id", "action_type",
    "summary", "policy_refs", "amount_usd",
)
ACTION_TYPES = frozenset({
    "request_evidence", "assign_adjuster", "customer_update",
    "escalate_review", "settlement_recommendation",
})
ID_PATTERNS = {
    "claim_id": re.compile(r"CLM-\d+"),
    "policy_id": re.compile(r"POL-\d+"),
    "policyholder_id": re.compile(r"PH-\d+"),
}
MAX_AMOUNT_USD = 100_000
MAX_SUMMARY_CHARS = 2000
IMPLAUSIBLE_PENALTY = 0.15


@lru_cache(maxsize=1)
def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _find_draft(response_text: str) -> dict | None:
    if not response_text:
        return None
    try:
        obj = json.loads(response_text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{[\s\S]*\}", response_text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _matches_expected(draft: dict, expected: Any) -> float | None:
    if not isinstance(expected, dict):
        return None
    refs = draft.get("policy_refs")
    cited = set(refs) if isinstance(refs, list) and all(isinstance(r, str) for r in refs) else set()
    checks = [draft.get(key) == expected[key]
              for key in ("claim_id", "policy_id", "policyholder_id", "action_type", "amount_usd")
              if key in expected]
    if "action_type_in" in expected:
        checks.append(draft.get("action_type") in expected["action_type_in"])
    if "policy_refs" in expected:
        checks.append(cited == set(expected["policy_refs"]))
    if "must_cite_policy_refs" in expected:
        checks.append(set(expected["must_cite_policy_refs"]) <= cited)
    return float(all(checks)) if checks else None


def evaluate_draft(draft: Any, expected: Any = None) -> dict[str, Any]:
    fixture = _fixture()
    draft = draft if isinstance(draft, dict) else {}
    action = draft.get("action_type")
    settlement = action == "settlement_recommendation"
    summary, refs, amount = draft.get("summary"), draft.get("policy_refs"), draft.get("amount_usd")
    refs_well_formed = (
        isinstance(refs, list) and all(isinstance(ref, str) and ref for ref in refs)
        and len(set(refs)) == len(refs)
    )
    cited = refs if refs_well_formed else []
    per_field = {
        **{field: isinstance(draft.get(field), str) and bool(pattern.fullmatch(draft[field]))
           for field, pattern in ID_PATTERNS.items()},
        "action_type": isinstance(action, str) and action in ACTION_TYPES,
        "summary": isinstance(summary, str) and bool(summary.strip()),
        "policy_refs": refs_well_formed,
        "amount_usd": (_number(amount) and 0 < amount <= MAX_AMOUNT_USD) if settlement else amount is None,
    }

    claim = next((c for c in fixture["claims"] if c["claim_id"] == draft.get("claim_id")), None)
    linked = claim is not None and all(claim[key] == draft.get(key) for key in ("policy_id", "policyholder_id"))
    policy = next((p for p in fixture["policies"] if p["policy_id"] == claim["policy_id"]), None) if linked else None
    applicable = {ref for ref, citation in canonical_citations(fixture).items()
                  if policy is not None and citation["wording_id"] == policy["wording_id"]}
    within_limit = True
    if settlement and per_field["amount_usd"] and policy is not None:
        limits = [max(coverage["limit_usd"] for coverage in policy["coverages"])]
        limits += [endorsement["limit_usd"] for endorsement in policy["endorsements"]
                   if endorsement["policy_ref"] in cited and "limit_usd" in endorsement]
        within_limit = amount <= min(limits)
    plausible = (
        linked and per_field["action_type"] and refs_well_formed and set(cited) <= applicable
        and (bool(cited) or not settlement) and within_limit and per_field["summary"]
        and len(summary) <= MAX_SUMMARY_CHARS and not unsafe_conclusion(summary)
        and set(draft) <= set(REQUIRED)
    )

    score = sum(per_field.values()) / len(REQUIRED)
    if not plausible:
        score = max(0.0, score - IMPLAUSIBLE_PENALTY)
    result: dict[str, Any] = {
        "claim_action_completeness": round(score, 3),
        "fields_present": per_field,
        "plausible": plausible,
    }
    match = _matches_expected(draft, expected)
    if match is not None:
        result["expected_match"] = match
    return result


def evaluate(*, response: str, expected: Any = None, **_kwargs: Any) -> dict[str, Any]:
    return evaluate_draft(_find_draft(response) or {}, expected)
