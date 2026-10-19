"""Contracts for the bounded, synthetic insurance business foundation."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence_models import EvidenceState

RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
PolicyId = Annotated[str, StringConstraints(pattern=r"^POL-\d+$")]
PolicyholderId = Annotated[str, StringConstraints(pattern=r"^PH-\d+$")]
ClaimStatus = Literal[
    "reported", "evidence_requested", "evidence_received",
    "adjuster_review", "customer_update_prepared",
]
ActorRole = Literal["policyholder", "adjuster", "presenter"]
ReviewFlag = Literal[
    "loss_outside_policy_period", "loss_jurisdiction_mismatch",
    "loss_outside_wording_period", "loss_outside_endorsement_period",
]


class BusinessError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ConcurrentChange(BusinessError):
    def __init__(self):
        super().__init__("concurrent_change", "The record changed. Reload before retrying.", 409)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorDetail(Contract):
    code: str
    message: str
    correlation_id: str


class ErrorResponse(Contract):
    error: ErrorDetail


class ActorMapping(Contract):
    role: ActorRole
    policyholder_id: PolicyholderId | None = None

    @model_validator(mode="after")
    def validate_customer(self):
        if (self.role == "policyholder") != (self.policyholder_id is not None):
            raise ValueError("Only a policyholder mapping must specify a policyholder ID.")
        return self


class PolicySnapshot(Contract):
    source_version: str
    policy: dict
    policyholder: dict
    policy_wording: dict
    citations: list[dict]


class TimelineEvent(Contract):
    event_id: str
    command_id: str
    actor_id: str | None
    actor_role: Literal["policyholder", "adjuster", "presenter", "system"]
    occurred_at: str
    summary: str
    before_version: int = Field(ge=0)
    after_version: int = Field(ge=1)
    internal_note: str | None = None
    visibility: Literal["customer", "internal"] = "customer"


class EvidenceTask(Contract):
    task_id: str
    title: str
    requested_evidence: str
    status: Literal["open", "completed"] = "open"
    assigned_to: str | None = None
    created_by: str | None
    created_at: str
    updated_at: str
    completed_by: str | None = None
    completed_at: str | None = None


class CaseNote(Contract):
    note_id: str
    body: str
    visibility: Literal["customer", "internal"]
    author_id: str | None
    created_at: str


class ClaimRecord(Contract):
    claim_id: str = Field(pattern=r"^CLM-\d+$")
    run_id: RunId
    policy_id: PolicyId
    policyholder_id: PolicyholderId
    incident_type: str
    loss_date: date
    state: str = Field(pattern=r"^[A-Z]{2}$")
    description: str
    status: ClaimStatus
    version: int = Field(ge=1)
    created_at: str
    updated_at: str
    policy_snapshot: PolicySnapshot
    review_flags: list[ReviewFlag] = Field(default_factory=list)
    outstanding_evidence: list[str] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    tasks: list[EvidenceTask] = Field(default_factory=list)
    notes: list[CaseNote] = Field(default_factory=list)
    assigned_adjuster_id: str | None = None
    synthetic: Literal[True] = True


class LossReport(Contract):
    policy_id: PolicyId
    vehicle_id: str = Field(min_length=1, max_length=80)
    incident_type: Literal["auto_collision", "auto_hail", "auto_other"]
    loss_date: date
    state: str = Field(pattern=r"^[A-Z]{2}$")
    description: str = Field(min_length=10, max_length=2000)
    synthetic_acknowledged: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_synthetic(self):
        if not self.synthetic_acknowledged:
            raise ValueError("Acknowledge that this report contains synthetic information only.")
        return self


class LossSubmission(Contract):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    report: LossReport


class LossReceipt(Contract):
    request_hash: str
    claim: ClaimRecord


class LossResult(Contract):
    claim: ClaimRecord
    replayed: bool


class TransitionCommand(Contract):
    target: ClaimStatus
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class CreateTask(Contract):
    expected_version: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=200)
    requested_evidence: str = Field(min_length=1, max_length=80)
    assigned_to: str | None = Field(default=None, min_length=1, max_length=200)


class CompleteTask(Contract):
    expected_version: int = Field(ge=1, strict=True)
    status: Literal["completed"]


class CreateNote(Contract):
    expected_version: int = Field(ge=1, strict=True)
    body: str = Field(min_length=1, max_length=2000)
    visibility: Literal["customer", "internal"] = "internal"


class AssignAdjuster(Contract):
    expected_version: int = Field(ge=1, strict=True)
    adjuster_id: str | None = Field(min_length=1, max_length=200)


class AccountContext(Contract):
    run_id: RunId
    actor_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: ActorRole
    policyholder_id: PolicyholderId | None
    source_version: str
    synthetic: Literal[True] = True
    adjusters: list[str] = Field(default_factory=list)


class PageRequest(Contract):
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=20, ge=1, le=100, strict=True)


T = TypeVar("T")


class Page(Contract, Generic[T]):
    items: list[T]
    next_offset: int | None


class DemoRun(Contract):
    schema_version: Literal[1] = 1
    run_id: RunId
    source_version: str
    created_at: str
    actors: dict[str, ActorMapping]
    policies: dict[str, PolicySnapshot]
    claims: dict[str, ClaimRecord]
    loss_receipts: dict[str, LossReceipt] = Field(default_factory=dict)
    evidence: EvidenceState = Field(default_factory=EvidenceState)
    synthetic: Literal[True] = True
