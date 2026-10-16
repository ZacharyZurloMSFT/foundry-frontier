"""Capacity escrow derived from durable jobs, enforced on every aggregate CAS."""
from __future__ import annotations

import json
from typing import get_args

from .evidence_models import MAX_FIELD_VALUE_BYTES, MAX_PROJECTED_FIELDS_BYTES, EvidenceJob, FieldName
from .models import BusinessError, DemoRun

MAX_EVIDENCE_AUDIT = 200
MAX_JOB_METADATA_BYTES = 16_384
FIELD_NAMES = frozenset(get_args(FieldName))


def encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def worker_audit_slots(job: EvidenceJob) -> int:
    if job.status in ("completed", "cancelled"):
        return 0
    remaining = max(0, job.max_attempts - job.attempts)
    if job.submission_uncertain or job.status == "failed" and job.phase == "submitting" and not job.operation_location:
        return 1  # Explicit cancellation must still be attributable.
    if job.status == "failed":
        if not remaining:
            return 1
        return 3 * remaining + (3 if job.phase == "ready" else 1)
    if job.status == "queued":
        if not remaining:
            return 2  # Attempt exhaustion followed by cancellation.
        return 3 * remaining + (2 if job.phase == "ready" else 0)
    # One submission at most; retries reuse an operation and consume retry/lease/outcome.
    return 3 * remaining + {"ready": 4, "submitting": 3, "polling": 2}.get(job.phase, 4)


def reservations(state: DemoRun) -> tuple[int, int]:
    worker_slots = sum(worker_audit_slots(job) for job in state.evidence.jobs.values())
    review_slots = sum(len(FIELD_NAMES - document.confirmed_fields.keys())
                       for document in state.evidence.documents.values())
    actor_bytes = max(encoded_size(actor) for actor in ["system:evidence-worker", *state.actors])
    value_bytes = MAX_FIELD_VALUE_BYTES
    for document in state.evidence.documents.values():
        for confirmed in document.confirmed_fields.values():
            actor_bytes = max(actor_bytes, encoded_size(confirmed.reviewed_by))
        values = [field.value for field in document.fields.values()]
        values.extend(field.value for field in document.confirmed_fields.values())
        for value in values:
            value_bytes = max(value_bytes, encoded_size(value))
    # Includes old/new confirmations, three actor occurrences and all event metadata.
    audit_bytes = 5120 + 3 * actor_bytes + 2 * (value_bytes - MAX_FIELD_VALUE_BYTES)
    future_bytes = (worker_slots + review_slots) * audit_bytes
    future_bytes += review_slots * (256 + actor_bytes + value_bytes)
    for document in state.evidence.documents.values():
        job = state.evidence.jobs.get(document.job_id)
        if job is None:
            raise BusinessError("invalid_storage_scope", "Evidence job linkage is invalid.", 500)
        can_complete = (
            job.status in ("queued", "running")
            or job.status == "failed" and not job.submission_uncertain and job.attempts < job.max_attempts
        )
        if can_complete:
            future_bytes += max(0, MAX_PROJECTED_FIELDS_BYTES - encoded_size({
                name: field.model_dump(mode="json") for name, field in document.fields.items()
            }))
            metadata = {
                "job": job.model_dump(mode="json"), "provenance": document.provenance,
                "raw_result": document.raw_result.model_dump(mode="json") if document.raw_result else None,
                "document_version": document.version,
            }
            future_bytes += max(0, MAX_JOB_METADATA_BYTES - encoded_size(metadata))
    return worker_slots + review_slots, future_bytes


def require_capacity(state: DemoRun, current_bytes: int, max_bytes: int) -> None:
    slots, future_bytes = reservations(state)
    if len(state.evidence.audit) + slots > MAX_EVIDENCE_AUDIT:
        raise BusinessError(
            "evidence_capacity", "Evidence history capacity is reserved for outstanding jobs and first field reviews.", 409,
        )
    if current_bytes + future_bytes > max_bytes:
        raise BusinessError(
            "run_capacity", "Run storage capacity is reserved for outstanding evidence and review records.", 409,
        )
