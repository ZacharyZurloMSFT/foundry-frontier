"""Authorised manual insurance operations, independent of all AI providers."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Literal, TypeVar

from ..insurance_domain import fixtures, lookup_policy_record
from ..repositories import _now
from .models import (
    AccountContext, ActorMapping, AssignAdjuster, BusinessError, CaseNote,
    ClaimRecord, ClaimStatus, CompleteTask, ConcurrentChange, CreateNote, CreateTask,
    DemoRun, EvidenceTask, LossReceipt, LossResult, LossSubmission, Page, PageRequest,
    PolicySnapshot, ReviewFlag, TimelineEvent, TransitionCommand,
)
from .store import BusinessRepository, StoredRun

MAX_CLAIMS = 100
MAX_TIMELINE_EVENTS = 200
MAX_CREATE_ATTEMPTS = 3
MAX_TASKS = 50
MAX_NOTES = 100
TRANSITIONS: dict[ClaimStatus, set[ClaimStatus]] = {
    "reported": {"evidence_requested"},
    "evidence_requested": {"evidence_received"},
    "evidence_received": {"adjuster_review", "evidence_requested"},
    "adjuster_review": {"customer_update_prepared", "evidence_requested"},
    "customer_update_prepared": {"evidence_requested"},
}
STATUS_SUMMARIES: dict[ClaimStatus, str] = {
    "reported": "Loss report received. A human will review the case.",
    "evidence_requested": "Additional evidence has been requested.",
    "evidence_received": "Evidence has been recorded for human review.",
    "adjuster_review": "The case is with an adjuster for review.",
    "customer_update_prepared": "A synthetic in-app customer update is being prepared.",
}
T = TypeVar("T")


def _review_flags(snapshot: PolicySnapshot, loss_date: date, state: str) -> list[ReviewFlag]:
    flags: list[ReviewFlag] = []
    policy, wording = snapshot.policy, snapshot.policy_wording
    if not date.fromisoformat(policy["effective_from"]) <= loss_date <= date.fromisoformat(policy["effective_to"]):
        flags.append("loss_outside_policy_period")
    if state != policy["state"]:
        flags.append("loss_jurisdiction_mismatch")
    if not date.fromisoformat(wording["effective_from"]) <= loss_date <= date.fromisoformat(wording["effective_to"]):
        flags.append("loss_outside_wording_period")
    if any(
        not date.fromisoformat(e["effective_from"]) <= loss_date <= date.fromisoformat(e["effective_to"])
        for e in policy["endorsements"]
    ):
        flags.append("loss_outside_endorsement_period")
    return flags


def seed_run(run_id: str, actors: dict[str, ActorMapping]) -> DemoRun:
    """Build an administrative seed; callers must authorise and persist it separately."""
    records = fixtures()
    now = _now()
    policies = {}
    for policy in records["policies"]:
        result = lookup_policy_record(policy["policy_id"])
        policies[policy["policy_id"]] = PolicySnapshot(
            source_version=records["dataset_version"],
            **{key: result[key] for key in ("policy", "policyholder", "policy_wording", "citations")},
        )
    state = DemoRun(
        run_id=run_id, source_version=records["dataset_version"], created_at=now,
        actors=actors, policies=policies, claims={},
    )
    customer_ids = {p.policy["policyholder_id"] for p in policies.values()}
    if not actors or any(not subject.strip() for subject in actors):
        raise BusinessError("invalid_actors", "A seed requires nonempty authenticated subject mappings.")
    if any(a.role == "policyholder" and a.policyholder_id not in customer_ids for a in state.actors.values()):
        raise BusinessError("invalid_actors", "A mapped policyholder must exist in the seed.")
    for record in records["claims"]:
        snapshot = policies[record["policy_id"]]
        if snapshot.policy["policyholder_id"] != record["policyholder_id"]:
            raise BusinessError("invalid_seed", "Seed claim and policyholder do not match.")
        status: ClaimStatus = "evidence_requested" if record["status"] == "awaiting_evidence" else record["status"]
        event = TimelineEvent(
            event_id=str(uuid.uuid4()), command_id=f"seed-{run_id}",
            actor_id="system:seed", actor_role="system", occurred_at=now,
            summary="Synthetic scenario seeded for this run.",
            before_version=0, after_version=1,
        )
        state.claims[record["claim_id"]] = ClaimRecord(
            **{k: record[k] for k in (
                "claim_id", "policy_id", "policyholder_id", "incident_type",
                "loss_date", "state", "description", "outstanding_evidence",
            )},
            run_id=run_id, status=status, version=1, created_at=now, updated_at=now,
            policy_snapshot=snapshot.model_copy(deep=True), timeline=[event],
            review_flags=_review_flags(snapshot, date.fromisoformat(record["loss_date"]), record["state"]),
        )
    return state


def _visible(actor: ActorMapping, policyholder_id: str) -> bool:
    return actor.role != "policyholder" or actor.policyholder_id == policyholder_id


def _claim_view(claim: ClaimRecord, actor: ActorMapping) -> ClaimRecord:
    result = claim.model_copy(deep=True)
    if actor.role == "policyholder":
        result.timeline = [
            event.model_copy(update={"actor_id": None, "internal_note": None})
            for event in result.timeline if event.visibility == "customer"
        ]
        result.notes = [
            note.model_copy(update={"author_id": None})
            for note in result.notes if note.visibility == "customer"
        ]
        result.tasks = [
            task.model_copy(update={"assigned_to": None, "created_by": None, "completed_by": None})
            for task in result.tasks
        ]
        result.assigned_adjuster_id = None
    return result


def _page(items: list[T], request: PageRequest | None) -> Page[T]:
    query = request or PageRequest()
    end = query.offset + query.limit
    return Page(items=items[query.offset:end], next_offset=end if end < len(items) else None)


class InsuranceBusinessService:
    """Subject IDs must come from verified authentication, never a form/persona switch."""

    def __init__(self, repository: BusinessRepository):
        self.repository = repository

    async def _context(self, run_id: str, subject_id: str) -> tuple[StoredRun, ActorMapping]:
        stored = await self.repository.get(run_id)
        actor = stored.state.actors.get(subject_id)
        if actor is None:
            raise BusinessError("forbidden", "This identity cannot access the demo run.", 403)
        return stored, actor

    async def account_context(self, run_id: str, subject_id: str) -> AccountContext:
        stored, actor = await self._context(run_id, subject_id)
        return AccountContext(
            run_id=run_id,
            actor_key=hashlib.sha256(f"{run_id}\0{subject_id}".encode()).hexdigest(),
            role=actor.role, policyholder_id=actor.policyholder_id,
            source_version=stored.state.source_version,
            adjusters=sorted(
                subject for subject, mapping in stored.state.actors.items() if mapping.role == "adjuster"
            ) if actor.role != "policyholder" else [],
        )

    @staticmethod
    def _claim(state: DemoRun, actor: ActorMapping, claim_id: str) -> ClaimRecord:
        claim = state.claims.get(claim_id)
        if claim is None or not _visible(actor, claim.policyholder_id):
            raise BusinessError("not_found", "Claim not found.", 404)
        return claim

    async def list_policies(self, run_id: str, subject_id: str, page: PageRequest | None = None) -> Page[PolicySnapshot]:
        stored, actor = await self._context(run_id, subject_id)
        policies = [
            p.model_copy(deep=True) for _, p in sorted(stored.state.policies.items())
            if _visible(actor, p.policy["policyholder_id"])
        ]
        return _page(policies, page)

    async def get_policy(self, run_id: str, subject_id: str, policy_id: str) -> PolicySnapshot:
        stored, actor = await self._context(run_id, subject_id)
        policy = stored.state.policies.get(policy_id)
        if policy is None or not _visible(actor, policy.policy["policyholder_id"]):
            raise BusinessError("not_found", "Policy not found.", 404)
        return policy.model_copy(deep=True)

    async def list_claims(self, run_id: str, subject_id: str, page: PageRequest | None = None,
                          status: ClaimStatus | None = None, assigned_to: str | None = None) -> Page[ClaimRecord]:
        stored, actor = await self._context(run_id, subject_id)
        if actor.role == "policyholder" and assigned_to is not None:
            raise BusinessError("forbidden", "Employee assignment filters are not available in the customer portal.", 403)
        claims = [
            _claim_view(c, actor) for _, c in sorted(stored.state.claims.items())
            if _visible(actor, c.policyholder_id)
            and (status is None or c.status == status)
            and (assigned_to is None or c.assigned_adjuster_id == assigned_to)
        ]
        return _page(claims, page)

    async def get_claim(self, run_id: str, subject_id: str, claim_id: str) -> ClaimRecord:
        stored, actor = await self._context(run_id, subject_id)
        return _claim_view(self._claim(stored.state, actor, claim_id), actor)

    @staticmethod
    def _replay(state: DemoRun, actor: ActorMapping, receipt_key: str, request_hash: str) -> LossResult | None:
        receipt = state.loss_receipts.get(receipt_key)
        if receipt is None:
            return None
        if receipt.request_hash != request_hash:
            raise BusinessError("idempotency_conflict", "Idempotency key was used for different loss details.", 409)
        InsuranceBusinessService._claim(state, actor, receipt.claim.claim_id)
        return LossResult(claim=_claim_view(receipt.claim, actor), replayed=True)

    async def report_loss(self, run_id: str, subject_id: str, submission: LossSubmission) -> LossResult:
        report = submission.report
        request_hash = hashlib.sha256(
            json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt_key = hashlib.sha256(f"{subject_id}\0{submission.idempotency_key}".encode()).hexdigest()
        claim_id = f"CLM-{uuid.uuid5(uuid.NAMESPACE_URL, f'northwind:{run_id}:{receipt_key}').int}"
        for _ in range(MAX_CREATE_ATTEMPTS):
            stored, actor = await self._context(run_id, subject_id)
            if actor.role != "policyholder":
                raise BusinessError("forbidden", "Only a mapped policyholder can submit this loss form.", 403)
            state = stored.state
            replay = self._replay(state, actor, receipt_key, request_hash)
            if replay is not None:
                return replay
            snapshot = state.policies.get(report.policy_id)
            if snapshot is None or not _visible(actor, snapshot.policy["policyholder_id"]):
                raise BusinessError("not_found", "Policy not found.", 404)
            asset = snapshot.policy["insured_asset"]
            if asset["type"] != "auto" or asset["vin"] != report.vehicle_id:
                raise BusinessError("vehicle_mismatch", "The synthetic vehicle must belong to this policy.")
            if len(state.claims) >= MAX_CLAIMS:
                raise BusinessError("run_capacity", "This demo run has reached its claim limit.", 409)
            if claim_id in state.claims:
                raise BusinessError("claim_conflict", "The claim identifier is already in use.", 409)
            now = _now()
            claim = ClaimRecord(
                claim_id=claim_id, run_id=run_id, policy_id=report.policy_id,
                policyholder_id=snapshot.policy["policyholder_id"], incident_type=report.incident_type,
                loss_date=report.loss_date, state=report.state, description=report.description,
                status="reported", version=1, created_at=now, updated_at=now,
                policy_snapshot=snapshot.model_copy(deep=True),
                review_flags=_review_flags(snapshot, report.loss_date, report.state),
                timeline=[TimelineEvent(
                    event_id=str(uuid.uuid4()), command_id=f"fnol-{receipt_key}",
                    actor_id=subject_id, actor_role=actor.role, occurred_at=now,
                    summary=STATUS_SUMMARIES["reported"], before_version=0, after_version=1,
                )],
            )
            state.claims[claim_id] = claim
            state.loss_receipts[receipt_key] = LossReceipt(request_hash=request_hash, claim=claim.model_copy(deep=True))
            try:
                await self.repository.replace(state, stored.etag)
            except ConcurrentChange:
                continue
            return LossResult(claim=_claim_view(claim, actor), replayed=False)
        stored, actor = await self._context(run_id, subject_id)
        replay = self._replay(stored.state, actor, receipt_key, request_hash)
        if replay is not None and actor.role == "policyholder":
            return replay
        raise ConcurrentChange()

    async def transition_claim(self, run_id: str, subject_id: str, claim_id: str,
                               command: TransitionCommand) -> ClaimRecord:
        stored, actor, claim = await self._case_command(run_id, subject_id, claim_id, command.expected_version)
        if command.target not in TRANSITIONS[claim.status]:
            raise BusinessError("invalid_transition", "This workflow transition is not allowed.", 409)
        claim.status = command.target
        return await self._save_case_command(
            stored, actor, subject_id, claim, STATUS_SUMMARIES[command.target], internal_note=command.reason,
        )

    async def _case_command(self, run_id: str, subject_id: str, claim_id: str,
                            expected_version: int) -> tuple[StoredRun, ActorMapping, ClaimRecord]:
        stored, actor = await self._context(run_id, subject_id)
        claim = self._claim(stored.state, actor, claim_id)
        if actor.role != "adjuster":
            raise BusinessError("forbidden", "Only an authorised adjuster can work this case.", 403)
        if claim.version != expected_version:
            raise ConcurrentChange()
        if len(claim.timeline) >= MAX_TIMELINE_EVENTS:
            raise BusinessError("run_capacity", "This demo claim has reached its history limit.", 409)
        return stored, actor, claim

    @staticmethod
    def _require_adjuster(state: DemoRun, subject_id: str | None) -> None:
        if subject_id is not None:
            mapping = state.actors.get(subject_id)
            if mapping is None or mapping.role != "adjuster":
                raise BusinessError("invalid_assignee", "Choose a permitted adjuster in this demo run.")

    async def _save_case_command(self, stored: StoredRun, actor: ActorMapping, subject_id: str,
                                 claim: ClaimRecord, summary: str, *, internal_note: str | None = None,
                                 visibility: Literal["customer", "internal"] = "customer") -> ClaimRecord:
        previous = claim.version
        claim.version += 1
        claim.updated_at = _now()
        claim.timeline.append(TimelineEvent(
            event_id=str(uuid.uuid4()), command_id=f"case-{uuid.uuid4()}",
            actor_id=subject_id, actor_role=actor.role, occurred_at=claim.updated_at,
            summary=summary, before_version=previous, after_version=claim.version,
            internal_note=internal_note, visibility=visibility,
        ))
        await self.repository.replace(stored.state, stored.etag)
        return _claim_view(claim, actor)

    async def create_task(self, run_id: str, subject_id: str, claim_id: str,
                          command: CreateTask) -> ClaimRecord:
        stored, actor, claim = await self._case_command(run_id, subject_id, claim_id, command.expected_version)
        self._require_adjuster(stored.state, command.assigned_to)
        if len(claim.tasks) >= MAX_TASKS:
            raise BusinessError("run_capacity", "This demo claim has reached its task limit.", 409)
        now = _now()
        claim.tasks.append(EvidenceTask(
            task_id=f"task-{uuid.uuid4()}", title=command.title,
            requested_evidence=command.requested_evidence, assigned_to=command.assigned_to,
            created_by=subject_id, created_at=now, updated_at=now,
        ))
        if command.requested_evidence not in claim.outstanding_evidence:
            claim.outstanding_evidence.append(command.requested_evidence)
        return await self._save_case_command(
            stored, actor, subject_id, claim, "An evidence task has been created.",
        )

    async def complete_task(self, run_id: str, subject_id: str, claim_id: str, task_id: str,
                            command: CompleteTask) -> ClaimRecord:
        stored, actor, claim = await self._case_command(run_id, subject_id, claim_id, command.expected_version)
        task = next((task for task in claim.tasks if task.task_id == task_id), None)
        if task is None:
            raise BusinessError("not_found", "Task not found.", 404)
        if task.status != "open":
            raise BusinessError("task_completed", "This evidence task is already completed.", 409)
        task.status = command.status
        task.completed_by = subject_id
        task.completed_at = task.updated_at = _now()
        if not any(t.status == "open" and t.requested_evidence == task.requested_evidence for t in claim.tasks):
            claim.outstanding_evidence = [e for e in claim.outstanding_evidence if e != task.requested_evidence]
        return await self._save_case_command(
            stored, actor, subject_id, claim,
            "An evidence task was completed manually. Document extraction and facts were not automatically confirmed.",
        )

    async def create_note(self, run_id: str, subject_id: str, claim_id: str,
                          command: CreateNote) -> ClaimRecord:
        stored, actor, claim = await self._case_command(run_id, subject_id, claim_id, command.expected_version)
        if len(claim.notes) >= MAX_NOTES:
            raise BusinessError("run_capacity", "This demo claim has reached its note limit.", 409)
        claim.notes.append(CaseNote(
            note_id=f"note-{uuid.uuid4()}", body=command.body, visibility=command.visibility,
            author_id=subject_id, created_at=_now(),
        ))
        return await self._save_case_command(
            stored, actor, subject_id, claim, "A case note has been added.",
            visibility=command.visibility,
        )

    async def assign_adjuster(self, run_id: str, subject_id: str, claim_id: str,
                              command: AssignAdjuster) -> ClaimRecord:
        stored, actor, claim = await self._case_command(run_id, subject_id, claim_id, command.expected_version)
        self._require_adjuster(stored.state, command.adjuster_id)
        if claim.assigned_adjuster_id == command.adjuster_id:
            raise BusinessError("assignment_unchanged", "This assignment is already current.", 409)
        claim.assigned_adjuster_id = command.adjuster_id
        return await self._save_case_command(
            stored, actor, subject_id, claim, "The case assignment has been updated.",
        )
