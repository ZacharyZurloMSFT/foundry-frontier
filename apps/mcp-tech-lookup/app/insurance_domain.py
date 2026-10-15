"""Shared, fail-closed synthetic insurance records and draft validation."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTION_KIND = "insurance_claim_action"


class InsuranceValidationError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def business_domain() -> str:
    domain = os.environ.get("NORTHWIND_BUSINESS_DOMAIN", "retail")
    if domain not in {"retail", "insurance"}:
        raise ValueError("NORTHWIND_BUSINESS_DOMAIN must be retail or insurance")
    return domain


def require_insurance_domain() -> None:
    if business_domain() != "insurance":
        raise InsuranceValidationError("Insurance actions require NORTHWIND_BUSINESS_DOMAIN=insurance.", 409)


def fixtures() -> dict:
    # The deployment staging step copies the same canonical JSON into each image.
    here = Path(__file__).resolve().parent
    candidates = [p / "data" / "northwind" / "insurance-fixtures.json" for p in here.parents]
    candidates.extend([here / "insurance-fixtures.json", here / "_northwind" / "insurance-fixtures.json"])
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise FileNotFoundError("Canonical synthetic insurance fixtures were not packaged.")
    records = json.loads(path.read_text(encoding="utf-8"))
    if records.get("synthetic") is not True or records.get("business_domain") != "insurance":
        raise ValueError("Insurance demo requires explicitly synthetic insurance fixtures")
    return records


def _record(rows: list[dict], field: str, identifier: str) -> dict:
    for row in rows:
        if row[field] == identifier:
            return deepcopy(row)
    raise InsuranceValidationError(f"Unknown synthetic {field}: {identifier}", 404)


def _effective(record: dict, loss_date: str) -> None:
    loss = date.fromisoformat(loss_date)
    if not date.fromisoformat(record["effective_from"]) <= loss <= date.fromisoformat(record["effective_to"]):
        raise InsuranceValidationError("Loss date is outside the policy or wording effective period; escalate for human review.")


def lookup_policy_record(policy_id: str, policyholder_id: str | None = None,
                         loss_date: str | None = None, state: str | None = None) -> dict:
    records = fixtures()
    policy = _record(records["policies"], "policy_id", policy_id)
    if policyholder_id is not None and policy["policyholder_id"] != policyholder_id:
        raise InsuranceValidationError("Policy and policyholder IDs do not match.")
    if state is not None and state != policy["state"]:
        raise InsuranceValidationError("Policy jurisdiction does not match the loss state.")
    wording = _record(records["policy_wordings"], "wording_id", policy["wording_id"])
    if wording["state"] != policy["state"] or wording.get("synthetic") is not True:
        raise InsuranceValidationError("Policy wording jurisdiction or provenance does not match.")
    references = {section["policy_ref"] for section in wording["sections"]}
    for endorsement in policy["endorsements"]:
        if (endorsement["state"] != policy["state"] or endorsement.get("synthetic") is not True
                or endorsement["policy_ref"] not in references):
            raise InsuranceValidationError("Endorsement jurisdiction, provenance or wording reference does not match.")
        if loss_date is not None:
            _effective(endorsement, loss_date)
    if loss_date is not None:
        _effective(policy, loss_date)
        _effective(wording, loss_date)
    return {
        "business_domain": "insurance", "synthetic": True, "disclaimer": records["disclaimer"],
        "policy": policy,
        "policyholder": _record(records["policyholders"], "policyholder_id", policy["policyholder_id"]),
        "policy_wording": wording,
        "citations": [
            {**section, "title": wording["title"], "version": wording["version"],
             "state": wording["state"], "effective_from": wording["effective_from"],
             "effective_to": wording["effective_to"]}
            for section in wording["sections"]
        ],
    }


def lookup_claim_record(claim_id: str, policy_id: str | None = None,
                        policyholder_id: str | None = None) -> dict:
    records = fixtures()
    claim = _record(records["claims"], "claim_id", claim_id)
    if policy_id is not None and claim["policy_id"] != policy_id:
        raise InsuranceValidationError("Claim and policy IDs do not match.")
    if policyholder_id is not None and claim["policyholder_id"] != policyholder_id:
        raise InsuranceValidationError("Claim and policyholder IDs do not match.")
    policy = lookup_policy_record(claim["policy_id"], claim["policyholder_id"],
                                  claim["loss_date"], claim["state"])
    return {**policy, "claim": claim}


class ClaimActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim_id: str = Field(pattern=r"^CLM-\d+$")
    policy_id: str = Field(pattern=r"^POL-\d+$")
    policyholder_id: str = Field(pattern=r"^PH-\d+$")
    action_type: Literal["request_evidence", "assign_adjuster", "customer_update",
                         "escalate_review", "settlement_recommendation"]
    summary: str = Field(min_length=1, max_length=2000)
    policy_refs: list[str] = Field(default_factory=list, max_length=20)
    amount_usd: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_context(self):
        result = lookup_claim_record(self.claim_id, self.policy_id, self.policyholder_id)
        citations = {c["policy_ref"] for c in result["citations"]}
        if len(set(self.policy_refs)) != len(self.policy_refs) or any(ref not in citations for ref in self.policy_refs):
            raise InsuranceValidationError("Policy references must identify applicable, versioned wording for this claim.")
        if self.action_type == "settlement_recommendation":
            if self.amount_usd is None or self.amount_usd <= 0 or not self.policy_refs:
                raise InsuranceValidationError("Settlement recommendations require a positive amount and applicable policy citations.")
            limits = [max(c["limit_usd"] for c in result["policy"]["coverages"])]
            limits.extend(e["limit_usd"] for e in result["policy"]["endorsements"]
                          if e["policy_ref"] in self.policy_refs and "limit_usd" in e)
            if self.amount_usd > min(limits):
                raise InsuranceValidationError("Recommendation exceeds an illustrative policy or cited endorsement limit; escalate review instead.")
        elif self.amount_usd is not None:
            raise InsuranceValidationError("Only a settlement_recommendation may include an amount; no payment is executed.")
        return self


def action_payload(draft: ClaimActionDraft) -> dict:
    return draft.model_dump(exclude_none=True)


def pending_action(item: dict) -> dict:
    return {
        "status": "pending_approval", "hitl_id": item["id"], "workflow_id": item["workflowId"],
        "kind": ACTION_KIND, "business_domain": "insurance", "synthetic": True,
        "draft": item["payload"], "execution_status": "not_executed",
        "message": "Draft saved for human review only. No coverage decision, claim approval, settlement, or payment was executed.",
    }
