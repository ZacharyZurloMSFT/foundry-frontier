"""Evidence registration and human review, atomically scoped to a live run."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date

from fastapi import HTTPException

from ..content_understanding import CUConfig, document_catalog, sample_bytes, validate_document
from ..repositories import _now
from ..settings import Settings
from .evidence_blobs import EvidenceBlobs, blob_key, integrity_error
from .evidence_capacity import MAX_EVIDENCE_AUDIT
from .evidence_models import (
    NOTICE, BlobReference, ConfirmedField, EvidenceAudit, EvidenceDocument, EvidenceDocumentView,
    EvidenceJob, EvidenceJobView, EvidenceReceipt, EvidenceResult, EvidenceStatus,
    JobCommand, ReviewCommand, finite_number,
)
from .models import ActorMapping, BusinessError, ClaimRecord, ConcurrentChange, DemoRun, Page, PageRequest, TimelineEvent
from .service import MAX_TIMELINE_EVENTS, InsuranceBusinessService, _page
from .store import BusinessRepository, StoredRun, _document

MAX_DOCUMENTS = 8
MAX_CLAIM_DOCUMENTS = 4
MAX_UPLOAD_BYTES = 10_485_760
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


def extraction_config(settings: Settings) -> CUConfig:
    return CUConfig(
        enabled=settings.cu_enabled,
        endpoint=settings.cu_endpoint or settings.foundry_endpoint,
        analyzer_id=settings.cu_analyzer_id,
        completion_model=settings.cu_completion_model,
        completion_deployment=settings.cu_completion_deployment,
        embedding_deployment=settings.cu_embedding_deployment,
        client_id=settings.azure_client_id,
        # Durable processing uses managed identity, not a persisted API credential.
        timeout_seconds=settings.cu_timeout_seconds,
    )


def capabilities(settings: Settings, run_id: str) -> EvidenceStatus:
    configured = False
    try:
        extraction_config(settings).check()
        configured = True
    except HTTPException:
        pass
    enabled = settings.evidence_enabled and bool(settings.storage_account)
    extraction = enabled and settings.evidence_extraction_enabled and configured
    runs = {run.strip() for run in settings.evidence_worker_run_ids.split(",") if run.strip()}
    return EvidenceStatus(
        enabled=enabled, uploads_enabled=enabled and settings.evidence_uploads_enabled,
        extraction_enabled=extraction,
        worker_enabled=extraction and settings.evidence_worker_enabled and run_id in runs,
        notice=(
            "Configuration only; storage, identity, worker deployment and CU connectivity have not been probed. "
            "Existing evidence reads and manual review do not require CU. " + NOTICE
        ),
    )


def document_view(document: EvidenceDocument, actor: ActorMapping) -> EvidenceDocumentView:
    result = EvidenceDocumentView.model_validate(document.model_dump(include=set(EvidenceDocumentView.model_fields)))
    if actor.role == "policyholder":
        result.uploaded_by = None
        result.confirmed_fields = {
            name: field.model_copy(update={"reviewed_by": None}) for name, field in result.confirmed_fields.items()
        }
    return result


def job_view(job: EvidenceJob) -> EvidenceJobView:
    return EvidenceJobView.model_validate(job.model_dump(include=set(EvidenceJobView.model_fields)))


def audit(state: DemoRun, document: EvidenceDocument, action: str, subject_id: str,
          *, field_name: str | None = None, before: ConfirmedField | None = None,
          after: ConfirmedField | None = None) -> None:
    if len(state.evidence.audit) >= MAX_EVIDENCE_AUDIT:
        raise BusinessError("evidence_capacity", "This bounded run has reached its evidence history limit.", 409)
    state.evidence.audit.append(EvidenceAudit(
        event_id=str(uuid.uuid4()), document_id=document.document_id, job_id=document.job_id,
        action=action, actor_id=subject_id, occurred_at=_now(), field_name=field_name,
        before=before.model_copy(deep=True) if before else None,
        after=after.model_copy(deep=True) if after else None,
    ))


def validate_source(data: bytes, filename: str, media_type: str) -> None:
    try:
        validate_document(data, filename, media_type, MAX_UPLOAD_BYTES)
    except HTTPException as exc:
        detail: dict = exc.detail if isinstance(exc.detail, dict) else {}
        raise BusinessError(detail.get("code", "invalid_document"), detail.get("message", "Invalid document."), exc.status_code) from None


class EvidenceService:
    def __init__(self, repository: BusinessRepository, blobs: EvidenceBlobs, settings: Settings):
        self.repository, self.blobs, self.settings = repository, blobs, settings
        self.business = InsuranceBusinessService(repository)

    async def authorise(self, run_id: str, subject_id: str, claim_id: str,
                        *, write: bool = False) -> tuple[StoredRun, ActorMapping, ClaimRecord]:
        stored, actor = await self.business._context(run_id, subject_id)
        claim = self.business._claim(stored.state, actor, claim_id)
        if write and actor.role == "presenter":
            raise BusinessError("forbidden", "Presenter access is read-only.", 403)
        return stored, actor, claim

    async def status(self, run_id: str, subject_id: str) -> EvidenceStatus:
        await self.business._context(run_id, subject_id)
        return capabilities(self.settings, run_id)

    async def samples(self, run_id: str, subject_id: str) -> dict:
        await self.business._context(run_id, subject_id)
        try:
            rows = document_catalog()
        except HTTPException as exc:
            raise BusinessError("sample_catalog_unavailable", "The synthetic source catalog is unavailable.", exc.status_code) from None
        return {"items": [
            {"filename": row["source_file"], "title": row.get("title", row["source_file"]), "media_type": row["media_type"]}
            for row in rows
        ]}

    def require_uploads(self, *, sample: bool) -> None:
        status = capabilities(self.settings, "")
        if not status.enabled or (not sample and not status.uploads_enabled):
            raise BusinessError("evidence_disabled", "This synthetic evidence submission path is not enabled.", 503)

    async def register_sample(self, run_id: str, subject_id: str, claim_id: str,
                              filename: str, expected_version: int, key: str, acknowledged: bool) -> EvidenceResult:
        await self.authorise(run_id, subject_id, claim_id, write=True)
        self.require_uploads(sample=True)
        if acknowledged is not True:
            raise BusinessError("synthetic_acknowledgement_required", "Acknowledge synthetic data only.", 422)
        try:
            row, data = sample_bytes(filename)
        except HTTPException as exc:
            detail: dict = exc.detail if isinstance(exc.detail, dict) else {}
            raise BusinessError(detail.get("code", "sample_unavailable"), detail.get("message", "Sample unavailable."), exc.status_code) from None
        return await self.register(
            run_id, subject_id, claim_id, data, filename, row["media_type"],
            expected_version, key, acknowledged, sample=True,
        )

    async def register(self, run_id: str, subject_id: str, claim_id: str, data: bytes,
                       filename: str, media_type: str, expected_version: int, key: str,
                       acknowledged: bool, *, sample: bool = False) -> EvidenceResult:
        await self.authorise(run_id, subject_id, claim_id, write=True)
        self.require_uploads(sample=sample)
        if acknowledged is not True:
            raise BusinessError("synthetic_acknowledgement_required", "Acknowledge synthetic data only.", 422)
        if not re.fullmatch(IDEMPOTENCY_PATTERN, key):
            raise BusinessError("invalid_idempotency_key", "Use a bounded Idempotency-Key.", 422)
        validate_source(data, filename, media_type)
        sha = hashlib.sha256(data).hexdigest()
        request_hash = hashlib.sha256(json.dumps(
            [subject_id, run_id, claim_id, sha, filename, media_type], separators=(",", ":"),
        ).encode()).hexdigest()
        receipt_key = hashlib.sha256(f"{subject_id}\0{key}".encode()).hexdigest()
        identity = uuid.uuid5(uuid.NAMESPACE_URL, f"northwind-evidence:{run_id}:{receipt_key}")
        document_id, job_id = f"doc-{identity}", f"job-{identity}"
        source = None
        for _ in range(4):
            stored, actor, claim = await self.authorise(run_id, subject_id, claim_id, write=True)
            state, evidence = stored.state, stored.state.evidence
            receipt = evidence.receipts.get(receipt_key)
            if receipt:
                if receipt.request_hash != request_hash:
                    raise BusinessError("idempotency_conflict", "This key was used for different evidence.", 409)
                document = evidence.documents[receipt.document_id]
                if await self.blobs.get(document.source_blob) != data:
                    raise integrity_error()
                return EvidenceResult(
                    document=document_view(document, actor), job=job_view(evidence.jobs[receipt.job_id]), replayed=True,
                )
            if claim.version != expected_version:
                raise ConcurrentChange()
            if (
                len(evidence.documents) >= MAX_DOCUMENTS
                or sum(d.claim_id == claim_id for d in evidence.documents.values()) >= MAX_CLAIM_DOCUMENTS
                or len(claim.timeline) >= MAX_TIMELINE_EVENTS
            ):
                raise BusinessError("evidence_capacity", "This bounded run has reached its evidence limit.", 409)
            now = _now()
            document = EvidenceDocument(
                document_id=document_id, claim_id=claim_id, filename=filename, media_type=media_type,
                size_bytes=len(data), sha256=sha, created_at=now,
                source_mode="synthetic_sample" if sample else "synthetic_upload",
                uploaded_by=subject_id, job_id=job_id,
                source_blob=source or BlobReference(
                    key=blob_key(run_id, data, "source"), sha256=sha,
                    size_bytes=len(data), etag="pending", kind="source",
                ),
            )
            job = EvidenceJob(
                job_id=job_id, document_id=document_id, claim_id=claim_id,
                created_at=now, updated_at=now, max_attempts=self.settings.evidence_worker_max_attempts,
            )
            evidence.documents[document_id], evidence.jobs[job_id] = document, job
            evidence.receipts[receipt_key] = EvidenceReceipt(
                request_hash=request_hash, document_id=document_id, job_id=job_id,
            )
            audit(state, document, "evidence_registered", subject_id)
            claim.version += 1
            claim.updated_at = now
            claim.timeline.append(TimelineEvent(
                event_id=str(uuid.uuid4()), command_id=job_id, actor_id=subject_id,
                actor_role=actor.role, occurred_at=now, summary="Synthetic evidence registered for human review.",
                before_version=claim.version - 1, after_version=claim.version,
            ))
            _document(state)
            if source is None:
                source = await self.blobs.put(blob_key(run_id, data, "source"), data, "source")
                if source.sha256 != sha or source.size_bytes != len(data) or await self.blobs.get(source) != data:
                    raise integrity_error()
            document.source_blob = source
            _document(state)
            try:
                await self.repository.replace(state, stored.etag)
            except ConcurrentChange:
                # Source is immutable and shared. A losing CAS must never delete it.
                continue
            return EvidenceResult(document=document_view(document, actor), job=job_view(job), replayed=False)
        raise ConcurrentChange()

    async def list_documents(self, run_id: str, subject_id: str, claim_id: str,
                             page: PageRequest) -> Page[EvidenceDocumentView]:
        stored, actor, _ = await self.authorise(run_id, subject_id, claim_id)
        documents = [
            document_view(document, actor)
            for _, document in sorted(stored.state.evidence.documents.items())
            if document.claim_id == claim_id
        ]
        return _page(documents, page)

    async def _document(self, run_id: str, subject_id: str,
                        document_id: str) -> tuple[StoredRun, ActorMapping, EvidenceDocument]:
        stored, actor = await self.business._context(run_id, subject_id)
        document = stored.state.evidence.documents.get(document_id)
        if document is None:
            raise BusinessError("not_found", "Document not found.", 404)
        self.business._claim(stored.state, actor, document.claim_id)
        return stored, actor, document

    async def get_document(self, run_id: str, subject_id: str, document_id: str) -> EvidenceDocumentView:
        _, actor, document = await self._document(run_id, subject_id, document_id)
        return document_view(document, actor)

    async def source(self, run_id: str, subject_id: str, document_id: str) -> tuple[EvidenceDocumentView, bytes]:
        _, actor, document = await self._document(run_id, subject_id, document_id)
        return document_view(document, actor), await self.blobs.get(document.source_blob)

    async def _job(self, run_id: str, subject_id: str,
                   job_id: str) -> tuple[StoredRun, ActorMapping, EvidenceJob, EvidenceDocument]:
        stored, actor = await self.business._context(run_id, subject_id)
        job = stored.state.evidence.jobs.get(job_id)
        if job is None:
            raise BusinessError("not_found", "Job not found.", 404)
        self.business._claim(stored.state, actor, job.claim_id)
        return stored, actor, job, stored.state.evidence.documents[job.document_id]

    async def get_job(self, run_id: str, subject_id: str, job_id: str) -> EvidenceJobView:
        _, _, job, _ = await self._job(run_id, subject_id, job_id)
        return job_view(job)

    async def job_command(self, run_id: str, subject_id: str, job_id: str,
                          command: JobCommand, *, cancel: bool) -> EvidenceJobView:
        stored, actor, job, document = await self._job(run_id, subject_id, job_id)
        if actor.role != "adjuster":
            raise BusinessError("forbidden", "Only an authorised adjuster can command evidence jobs.", 403)
        if job.version != command.expected_version:
            raise ConcurrentChange()
        if cancel:
            if job.status not in ("queued", "running", "failed"):
                raise BusinessError("invalid_job_state", "This job cannot be cancelled.", 409)
            # Submission without a saved operation must remain non-retryable, even after cancellation.
            if job.phase == "submitting" and not job.operation_location:
                job.submission_uncertain = True
            job.status = "cancelled"
        else:
            if job.submission_uncertain or (job.phase == "submitting" and not job.operation_location):
                raise BusinessError("cu_submission_uncertain", "Operator reconciliation is required; resubmission was refused.", 409)
            if job.status != "failed" or job.attempts >= job.max_attempts:
                raise BusinessError("invalid_job_state", "Only failed jobs with attempts remaining can be retried.", 409)
            job.status = "queued"
            job.error_code = job.error_message = None
        job.version += 1
        job.updated_at = _now()
        job.lease_id, job.lease_until = None, 0
        audit(stored.state, document, "job_cancelled" if cancel else "job_retry_requested", subject_id)
        await self.repository.replace(stored.state, stored.etag)
        return job_view(job)

    async def review(self, run_id: str, subject_id: str,
                     document_id: str, command: ReviewCommand) -> EvidenceDocumentView:
        stored, actor, document = await self._document(run_id, subject_id, document_id)
        if actor.role != "adjuster":
            raise BusinessError("forbidden", "Only an authorised adjuster can confirm evidence fields.", 403)
        if command.synthetic_acknowledged is not True:
            raise BusinessError("synthetic_acknowledgement_required", "Acknowledge synthetic data only.", 422)
        if (
            command.expected_review_version != document.review_version
            or command.expected_document_version != document.version
        ):
            raise ConcurrentChange()
        claim = stored.state.claims[document.claim_id]
        for field in command.fields:
            value = field.value
            if field.decision == "accept":
                extracted = document.fields.get(field.name)
                if extracted is None or extracted.missing or extracted.value is None:
                    raise BusinessError("missing_extracted_value", "There is no extracted value to accept.", 409)
                value = extracted.value
            if field.decision != "clear":
                self._validate_confirmation(claim, field.name, value)
            confirmed = ConfirmedField(
                value=None if field.decision == "clear" else value, decision=field.decision,
                reviewed_by=subject_id, reviewed_at=_now(),
                extraction_sha256=document.raw_result.sha256 if document.raw_result else None,
            )
            audit(stored.state, document, "field_reviewed", subject_id, field_name=field.name,
                  before=document.confirmed_fields.get(field.name), after=confirmed)
            document.confirmed_fields[field.name] = confirmed
        document.review_version += 1
        document.version += 1
        await self.repository.replace(stored.state, stored.etag)
        return document_view(document, actor)

    @staticmethod
    def _validate_confirmation(claim: ClaimRecord, name: str, value) -> None:
        canonical = {
            "claim_id": claim.claim_id, "policy_id": claim.policy_id, "policyholder_id": claim.policyholder_id,
        }
        if name in canonical:
            if value != canonical[name]:
                raise BusinessError("evidence_reference_conflict", "Confirmed identifiers must match the linked case.", 409)
        elif name == "loss_date":
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise BusinessError("invalid_loss_date", "Use an ISO calendar date.", 422) from None
        elif not finite_number(value) or value < 0:
            raise BusinessError("invalid_amount", "Amounts must be finite, nonnegative numbers.", 422)
