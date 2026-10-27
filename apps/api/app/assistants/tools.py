"""Server-bound tools. The model supplies record IDs only; identity, role and visibility are never arguments.

Every result is a whitelisted projection of the caller's own, already visibility-filtered records, so a new
internal field added to a record can never reach a model by accident.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..business.models import BusinessError, ClaimRecord, PolicySnapshot
from ..business.service import STATUS_SUMMARIES
from .grounding import Grounding
from .models import ClaimId, Contract, PolicyId, ToolEvent
from .scope import ScopedReader

MAX_RESULT_CHARS = 24_000
NOTE_LIMIT = 10
TIMELINE_LIMIT = 15
TASK_LIMIT = 20
FLAG_EXPLANATIONS = {
    "loss_outside_policy_period": "The reported loss date is outside the policy's effective dates. "
                                  "This is recorded for a human reviewer; it is not a denial.",
    "loss_jurisdiction_mismatch": "The loss state differs from the policy's state. "
                                  "This is recorded for a human reviewer; it is not a denial.",
    "loss_outside_wording_period": "The loss date is outside the effective dates of the policy wording version on file. "
                                   "This is recorded for a human reviewer; it is not a denial.",
    "loss_outside_endorsement_period": "The loss date is outside an endorsement's effective dates. "
                                       "This is recorded for a human reviewer; it is not a denial.",
}
ENDORSEMENT_FIELDS = ("endorsement_id", "title", "version", "state", "effective_from", "effective_to",
                      "coverage_type", "daily_limit_usd", "maximum_days", "limit_usd", "policy_ref", "description")


class NoArguments(Contract):
    pass


class PolicyArguments(Contract):
    policy_id: PolicyId


class ClaimArguments(Contract):
    claim_id: ClaimId


def _spec(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function", "name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": properties, "required": list(properties),
                       "additionalProperties": False},
    }


SPECS: dict[str, dict[str, Any]] = {
    "list_my_policies": _spec(
        "list_my_policies", "List the signed-in policyholder's own policies (summary only). Takes no arguments.", {}),
    "get_policy": _spec(
        "get_policy",
        "Read one of the signed-in policyholder's own policies: schedule, endorsements and versioned wording "
        "with policy_ref citations.",
        {"policy_id": {"type": "string", "description": "A policy ID such as POL-1001, exactly as supplied or listed."}}),
    "list_my_claims": _spec(
        "list_my_claims", "List the signed-in policyholder's own claims (summary only). Takes no arguments.", {}),
    "get_claim": _spec(
        "get_claim",
        "Read one of the signed-in policyholder's own claims as the customer sees it, including status, outstanding "
        "evidence, recorded review flags and the policy wording recorded with the claim.",
        {"claim_id": {"type": "string", "description": "A claim ID such as CLM-2001, exactly as supplied or listed."}}),
}
ARGUMENTS: dict[str, type[Contract]] = {
    "list_my_policies": NoArguments, "get_policy": PolicyArguments,
    "list_my_claims": NoArguments, "get_claim": ClaimArguments,
}


def project_policy(snapshot: PolicySnapshot) -> dict[str, Any]:
    policy, wording = snapshot.policy, snapshot.policy_wording
    return {
        "policy_id": policy["policy_id"], "status": policy.get("status"), "state": policy.get("state"),
        "effective_from": policy.get("effective_from"), "effective_to": policy.get("effective_to"),
        "insured_asset": (policy.get("insured_asset") or {}).get("description"),
        "coverages": [
            {key: coverage.get(key) for key in ("coverage_type", "limit_usd", "deductible_usd")}
            for coverage in policy.get("coverages", [])
        ],
        "endorsements": [
            {key: endorsement.get(key) for key in ENDORSEMENT_FIELDS} for endorsement in policy.get("endorsements", [])
        ],
        "wording": {
            **{key: wording.get(key) for key in ("wording_id", "title", "version", "state", "effective_from", "effective_to")},
            "sections": [
                {key: section.get(key) for key in ("section_id", "policy_ref", "text")}
                for section in wording.get("sections", [])
            ],
        },
        "source_version": snapshot.source_version,
    }


def project_policy_summary(snapshot: PolicySnapshot) -> dict[str, Any]:
    policy = snapshot.policy
    return {
        "policy_id": policy["policy_id"], "status": policy.get("status"), "state": policy.get("state"),
        "effective_from": policy.get("effective_from"), "effective_to": policy.get("effective_to"),
        "insured_asset": (policy.get("insured_asset") or {}).get("description"),
        "coverage_types": [coverage.get("coverage_type") for coverage in policy.get("coverages", [])],
    }


def project_claim(claim: ClaimRecord) -> dict[str, Any]:
    notes = [note for note in claim.notes if note.visibility == "customer"][-NOTE_LIMIT:]
    timeline = [event for event in claim.timeline if event.visibility == "customer"][-TIMELINE_LIMIT:]
    return {
        "claim_id": claim.claim_id, "policy_id": claim.policy_id, "incident_type": claim.incident_type,
        "loss_date": claim.loss_date.isoformat(), "state": claim.state, "description": claim.description,
        "status": claim.status, "status_summary": STATUS_SUMMARIES[claim.status],
        "created_at": claim.created_at, "updated_at": claim.updated_at,
        "review_flags": [{"flag": flag, "explanation": FLAG_EXPLANATIONS[flag]} for flag in claim.review_flags],
        "outstanding_evidence": list(claim.outstanding_evidence),
        "evidence_tasks": [
            {"title": task.title, "requested_evidence": task.requested_evidence, "status": task.status,
             "completed_at": task.completed_at}
            for task in claim.tasks[:TASK_LIMIT]
        ],
        "case_updates": [{"posted_at": note.created_at, "message": note.body} for note in notes],
        "timeline": [{"occurred_at": event.occurred_at, "summary": event.summary} for event in timeline],
        "policy_recorded_with_claim": project_policy(claim.policy_snapshot),
    }


def project_claim_summary(claim: ClaimRecord) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id, "policy_id": claim.policy_id, "incident_type": claim.incident_type,
        "loss_date": claim.loss_date.isoformat(), "state": claim.state, "status": claim.status,
        "status_summary": STATUS_SUMMARIES[claim.status], "updated_at": claim.updated_at,
        "outstanding_evidence_count": len(claim.outstanding_evidence),
        "review_flags": list(claim.review_flags),
    }


def dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class ToolOutcome:
    output: str
    event: ToolEvent


class ToolBox:
    def __init__(self, reader: ScopedReader, grounding: Grounding, allowed: tuple[str, ...]):
        self.reader = reader
        self.grounding = grounding
        self.allowed = tuple(name for name in allowed if name in SPECS)
        self.handlers: dict[str, Callable[[Any], Awaitable[tuple[dict[str, Any], str]]]] = {
            "list_my_policies": self._list_policies, "get_policy": self._get_policy,
            "list_my_claims": self._list_claims, "get_claim": self._get_claim,
        }

    def specs(self) -> list[dict[str, Any]]:
        return [SPECS[name] for name in self.allowed]

    async def pinned_context(self) -> dict[str, Any]:
        scope = self.reader.scope
        pinned: dict[str, Any] = {}
        if scope.claim_id:
            claim = await self.reader.claim(scope.claim_id)
            self.grounding.add_claim(claim)
            self.grounding.add_policy(claim.policy_snapshot)
            pinned["claim"] = project_claim(claim)
        elif scope.policy_id:
            policy = await self.reader.policy(scope.policy_id)
            self.grounding.add_policy(policy)
            pinned["policy"] = project_policy(policy)
        return pinned

    async def call(self, name: str, arguments: str) -> ToolOutcome:
        if name not in self.allowed:
            return self._failure("invalid", "Unavailable tool request", "tool_not_available",
                                 "That tool is not available for this assistant.", name="unavailable")
        try:
            parsed = ARGUMENTS[name].model_validate(json.loads(arguments or "{}"))
        except (ValueError, ValidationError):
            return self._failure("invalid", f"Rejected a malformed {name} request", "invalid_arguments",
                                 "Arguments must match the tool schema exactly.", name=name)
        try:
            result, label = await self.handlers[name](parsed)
        except BusinessError as error:
            if error.code == "not_found":
                return self._failure("not_found", "Record not found among your records", "not_found",
                                     "No such record among the signed-in user's own records.", name=name)
            return self._failure("error", "A lookup could not be completed", "lookup_failed",
                                 "The lookup could not be completed.", name=name)
        output = dump(result)
        if len(output) > MAX_RESULT_CHARS:
            return self._failure("error", "A lookup returned too much data", "result_too_large",
                                 "The result exceeded its size budget.", name=name)
        return ToolOutcome(output, ToolEvent(name=name, status="ok", label=label))

    @staticmethod
    def _failure(status: str, label: str, code: str, message: str, *, name: str) -> ToolOutcome:
        event = ToolEvent(name=name, status=status, label=label)  # type: ignore[arg-type]
        return ToolOutcome(dump({"error": code, "message": message}), event)

    async def _list_policies(self, _: NoArguments) -> tuple[dict[str, Any], str]:
        policies, more = await self.reader.policies()
        self.grounding.ids.update(policy.policy["policy_id"] for policy in policies)
        return {"policies": [project_policy_summary(p) for p in policies], "more_available": more}, "Looked up your policies"

    async def _get_policy(self, arguments: PolicyArguments) -> tuple[dict[str, Any], str]:
        policy = await self.reader.policy(arguments.policy_id)
        self.grounding.add_policy(policy)
        return project_policy(policy), f"Read policy {arguments.policy_id}"

    async def _list_claims(self, _: NoArguments) -> tuple[dict[str, Any], str]:
        claims, more = await self.reader.claims()
        for claim in claims:
            self.grounding.add_claim(claim)
        return {"claims": [project_claim_summary(c) for c in claims], "more_available": more}, "Looked up your claims"

    async def _get_claim(self, arguments: ClaimArguments) -> tuple[dict[str, Any], str]:
        claim = await self.reader.claim(arguments.claim_id)
        self.grounding.add_claim(claim)
        self.grounding.add_policy(claim.policy_snapshot)
        return project_claim(claim), f"Read claim {arguments.claim_id}"
