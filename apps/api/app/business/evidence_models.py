"""Durable evidence contracts. Private persistence types never leave the API."""
from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, model_validator

FieldName = Literal[
    "claim_id", "policy_id", "policyholder_id", "loss_date",
    "estimate_total_usd", "invoice_total_usd",
]
FieldValue = StrictStr | StrictInt | StrictFloat | None
Decision = Literal["accept", "correct", "clear"]
MAX_FIELD_VALUE_BYTES = 1202
MAX_PROJECTED_FIELDS_BYTES = 65_536
MAX_PROVENANCE_BYTES = 4096
MAX_OPERATION_LOCATION_CHARS = 1024
NOTICE = (
    "Synthetic evidence for human review only. Extraction is untrusted; confirmation "
    "does not determine coverage or payment and does not change claim workflow. "
    "Cancellation stops further processing, not necessarily provider compute."
)


def finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class EvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExtractedField(EvidenceContract):
    type: str
    value: FieldValue = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_spans: list[dict | list | str | int | float | None] = Field(default_factory=list)
    missing: bool = True


class ConfirmedField(EvidenceContract):
    value: FieldValue
    decision: Decision
    reviewed_by: str | None
    reviewed_at: str
    extraction_sha256: str | None


class EvidenceDocumentView(EvidenceContract):
    document_id: str
    claim_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    version: int = 1
    created_at: str
    source_mode: Literal["synthetic_sample", "synthetic_upload"]
    uploaded_by: str | None
    job_id: str
    review_version: int = 0
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    confirmed_fields: dict[str, ConfirmedField] = Field(default_factory=dict)
    notice: str = NOTICE


class EvidenceJobView(EvidenceContract):
    job_id: str
    document_id: str
    claim_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    version: int = 1
    attempts: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    notice: str = NOTICE


class BlobReference(EvidenceContract):
    key: str = Field(pattern=r"^[A-Za-z0-9/_-]{1,512}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=10_485_760)
    etag: str = Field(pattern=r"^[\x21-\x7e]{1,256}$")
    kind: Literal["source", "result"]


class EvidenceDocument(EvidenceDocumentView):
    source_blob: BlobReference
    raw_result: BlobReference | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class EvidenceJob(EvidenceJobView):
    phase: Literal["ready", "submitting", "polling", "done"] = "ready"
    operation_location: str | None = Field(default=None, max_length=MAX_OPERATION_LOCATION_CHARS)
    provider_endpoint: str | None = Field(default=None, max_length=512)
    provider_analyzer: str | None = Field(default=None, max_length=128)
    provider_model: str | None = Field(default=None, max_length=128)
    lease_id: str | None = None
    lease_until: float = 0
    submission_uncertain: bool = False


class EvidenceReceipt(EvidenceContract):
    request_hash: str
    document_id: str
    job_id: str


class EvidenceAudit(EvidenceContract):
    event_id: str
    document_id: str
    job_id: str
    action: str
    actor_id: str
    occurred_at: str
    field_name: str | None = None
    before: ConfirmedField | None = None
    after: ConfirmedField | None = None


class EvidenceState(EvidenceContract):
    documents: dict[str, EvidenceDocument] = Field(default_factory=dict)
    jobs: dict[str, EvidenceJob] = Field(default_factory=dict)
    receipts: dict[str, EvidenceReceipt] = Field(default_factory=dict)
    audit: list[EvidenceAudit] = Field(default_factory=list)


class EvidenceResult(EvidenceContract):
    document: EvidenceDocumentView
    job: EvidenceJobView
    replayed: bool


class EvidenceSampleRequest(EvidenceContract):
    filename: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1, strict=True)
    synthetic_acknowledged: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_acknowledgement(self):
        if not self.synthetic_acknowledged:
            raise ValueError("Acknowledge synthetic data only.")
        return self


class JobCommand(EvidenceContract):
    expected_version: int = Field(ge=1, strict=True)


class FieldReview(EvidenceContract):
    name: FieldName
    decision: Decision
    value: FieldValue = None

    @model_validator(mode="after")
    def validate_value(self):
        if isinstance(self.value, str) and len(self.value) > 200:
            raise ValueError("Review values must be bounded.")
        if isinstance(self.value, (int, float)) and not finite_number(self.value):
            raise ValueError("Review values must be finite.")
        if len(json.dumps(self.value, ensure_ascii=True, allow_nan=False).encode()) > MAX_FIELD_VALUE_BYTES:
            raise ValueError("Review values must fit the bounded confirmation record.")
        if self.decision != "correct" and "value" in self.model_fields_set:
            raise ValueError("Only corrections accept a value.")
        if self.decision == "correct" and self.value is None:
            raise ValueError("Use clear to remove a value.")
        return self


class ReviewCommand(EvidenceContract):
    expected_review_version: int = Field(ge=0, strict=True)
    expected_document_version: int = Field(ge=1, strict=True)
    fields: Annotated[list[FieldReview], Field(min_length=1, max_length=6)]
    synthetic_acknowledged: bool = Field(strict=True)

    @model_validator(mode="after")
    def unique_fields(self):
        if not self.synthetic_acknowledged:
            raise ValueError("Acknowledge synthetic data only.")
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("Each field can be reviewed only once per command.")
        return self


class EvidenceStatus(EvidenceContract):
    enabled: bool
    uploads_enabled: bool
    extraction_enabled: bool
    worker_enabled: bool
    max_upload_bytes: int = 10_485_760
    notice: str
