"""Run-scoped durable worker: python -m app.business.evidence_worker --run-id RUN --once."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable

import httpx
from azure.core.exceptions import AzureError
from azure.identity.aio import ManagedIdentityCredential
from fastapi import HTTPException
from pydantic import ValidationError

from ..content_understanding import CUService
from ..repositories import _now
from ..settings import Settings, get_settings
from .evidence_blobs import MAX_BLOB_BYTES, AzureEvidenceBlobs, EvidenceBlobs, blob_key, integrity_error
from .evidence_models import MAX_OPERATION_LOCATION_CHARS, EvidenceDocument, EvidenceJob
from .evidence_provider import (
    CUEvidenceProvider, EvidenceProvider, ProviderFailure, project_fields, validate_provenance, validate_result,
)
from .evidence_service import audit, capabilities, extraction_config
from .models import BusinessError, ConcurrentChange, DemoRun
from .store import RUN_ID, BusinessRepository, CosmosBusinessRepository, StoredRun

logger = logging.getLogger(__name__)


class LeaseLost(Exception):
    pass


class EvidenceWorker:
    def __init__(self, repository: BusinessRepository, blobs: EvidenceBlobs, provider: EvidenceProvider,
                 settings: Settings, *, clock: Callable[[], float] = time.time,
                 poll_interval: float = 1, poll_limit: int = 30):
        self.repository, self.blobs, self.provider, self.settings = repository, blobs, provider, settings
        self.clock, self.poll_interval = clock, poll_interval
        self.poll_limit = max(1, min(poll_limit, 120))

    def require_run(self, run_id: str) -> None:
        try:
            RUN_ID.validate_python(run_id)
        except ValidationError:
            raise BusinessError("invalid_run", "Use one explicit valid run identifier.", 422) from None
        if not capabilities(self.settings, run_id).worker_enabled:
            raise BusinessError("evidence_worker_disabled", "Worker is disabled, unconfigured or outside the explicit run allowlist.", 503)

    async def _acquire(self, run_id: str, job_id: str) -> str | None:
        for _ in range(4):
            stored = await self.repository.get(run_id)
            job = stored.state.evidence.jobs[job_id]
            if job.status != "queued" and not (job.status == "running" and job.lease_until <= self.clock()):
                return None
            if job.attempts >= job.max_attempts:
                self._mark_failed(job, "cu_attempts_exhausted")
                job.version += 1
                job.updated_at = _now()
                audit(stored.state, stored.state.evidence.documents[job.document_id], "job_failed", "system:evidence-worker")
                try:
                    await self.repository.replace(stored.state, stored.etag)
                    return None
                except ConcurrentChange:
                    continue
            active = sum(
                current.status == "running" and current.lease_until > self.clock()
                for current in stored.state.evidence.jobs.values()
            )
            if active >= 2:
                return None
            lease = str(uuid.uuid4())
            job.lease_id, job.lease_until = lease, self.clock() + self.settings.evidence_worker_lease_seconds
            job.status = "running"
            job.attempts += 1
            job.version += 1
            job.updated_at = _now()
            audit(stored.state, stored.state.evidence.documents[job.document_id], "job_attempt_started", "system:evidence-worker")
            try:
                await self.repository.replace(stored.state, stored.etag)
            except ConcurrentChange:
                continue
            # A response failure before here never allows a submit.
            return lease
        return None

    async def _owned(self, run_id: str, job_id: str, lease: str) -> tuple[StoredRun, EvidenceJob, EvidenceDocument]:
        stored = await self.repository.get(run_id)
        job = stored.state.evidence.jobs[job_id]
        if job.lease_id != lease or job.status != "running" or job.lease_until <= self.clock():
            raise LeaseLost
        return stored, job, stored.state.evidence.documents[job.document_id]

    async def _change(self, run_id: str, job_id: str, lease: str,
                      change: Callable[[DemoRun, EvidenceJob, EvidenceDocument], None]) -> None:
        for _ in range(4):
            stored, job, document = await self._owned(run_id, job_id, lease)
            change(stored.state, job, document)
            job.version += 1
            job.updated_at = _now()
            try:
                await self.repository.replace(stored.state, stored.etag)
                return
            except ConcurrentChange:
                continue
        raise ConcurrentChange()

    @staticmethod
    def _mark_failed(job: EvidenceJob, code: str) -> None:
        if job.phase == "submitting" and not job.operation_location:
            job.submission_uncertain = True
        job.error_code = "cu_submission_uncertain" if job.submission_uncertain else code
        job.error_message = (
            "Submission outcome is uncertain. Operator reconciliation is required; do not resubmit."
            if job.submission_uncertain else "Evidence processing did not complete. Review configuration and retry only if permitted."
        )
        job.status = "failed"
        job.lease_id, job.lease_until = None, 0

    async def _fail(self, run_id: str, job_id: str, lease: str, code: str) -> None:
        def fail(state: DemoRun, job: EvidenceJob, document: EvidenceDocument) -> None:
            self._mark_failed(job, code)
            audit(state, document, "job_failed", "system:evidence-worker")
        # Storage/poll retries are bounded, and every retry revalidates the current lease.
        for attempt in range(3):
            try:
                await self._change(run_id, job_id, lease, fail)
                return
            except (AzureError, TimeoutError):
                stored = await self.repository.get(run_id)
                current = stored.state.evidence.jobs[job_id]
                if current.status == "failed" and current.lease_id is None and current.error_code:
                    return
                if attempt == 2:
                    raise
                await asyncio.sleep(0.1)

    async def _process(self, run_id: str, job_id: str, lease: str) -> None:
        try:
            _, job, document = await self._owned(run_id, job_id, lease)
            if job.phase == "submitting" and not job.operation_location:
                await self._fail(run_id, job_id, lease, "cu_submission_uncertain")
                return
            if job.attempts > job.max_attempts or job.submission_uncertain:
                await self._fail(run_id, job_id, lease, "cu_attempts_exhausted")
                return
            async with asyncio.timeout(min(
                self.settings.evidence_worker_lease_seconds - 5, self.settings.cu_timeout_seconds,
            )):
                data = await self.blobs.get(document.source_blob)
                if not job.operation_location:
                    provenance = validate_provenance(await self.provider.preflight())

                    def submitting(state: DemoRun, current: EvidenceJob, doc: EvidenceDocument) -> None:
                        current.phase = "submitting"
                        current.provider_endpoint = provenance["endpoint"]
                        current.provider_analyzer = provenance["analyzer_id"]
                        current.provider_model = provenance["model"]
                        doc.provenance = dict(provenance)
                        audit(state, doc, "cu_submission_started", "system:evidence-worker")
                    # Critical paid boundary: no POST until this intent is confirmed durable.
                    await self._change(run_id, job_id, lease, submitting)
                    await self._owned(run_id, job_id, lease)
                    location = await self.provider.submit(data, document.filename, document.media_type)
                    if not isinstance(location, str) or len(location) > MAX_OPERATION_LOCATION_CHARS:
                        raise ProviderFailure("cu_submission_uncertain")

                    def submitted(state: DemoRun, current: EvidenceJob, doc: EvidenceDocument) -> None:
                        current.operation_location = location
                        current.phase = "polling"
                        audit(state, doc, "cu_operation_recorded", "system:evidence-worker")
                    await self._change(run_id, job_id, lease, submitted)
                for _ in range(self.poll_limit):
                    _, job, document = await self._owned(run_id, job_id, lease)
                    if not job.operation_location:
                        raise ProviderFailure("cu_submission_uncertain")
                    result = await self.provider.poll(job.operation_location, document.provenance)
                    if result is None:
                        await asyncio.sleep(self.poll_interval)
                        continue
                    result = validate_result(result, document.provenance["analyzer_id"])
                    fields = project_fields(result)
                    raw = json.dumps({
                        "result": result, "provenance": document.provenance,
                        "source_sha256": document.sha256, "document_id": document.document_id,
                        "source": {
                            "filename": document.filename, "media_type": document.media_type,
                            "size_bytes": document.size_bytes, "sha256": document.sha256,
                            "etag": document.source_blob.etag, "source_mode": document.source_mode,
                        },
                    }, sort_keys=True, allow_nan=False).encode()
                    if len(raw) > MAX_BLOB_BYTES:
                        raise ProviderFailure("cu_result_too_large")
                    await self._owned(run_id, job_id, lease)
                    reference = await self.blobs.put(blob_key(run_id, raw, "result"), raw, "result")
                    if await self.blobs.get(reference) != raw:
                        raise integrity_error()

                    def completed(state: DemoRun, current: EvidenceJob, doc: EvidenceDocument) -> None:
                        doc.raw_result, doc.fields = reference, fields
                        doc.version += 1
                        current.status, current.phase = "completed", "done"
                        current.error_code = current.error_message = None
                        current.lease_id, current.lease_until = None, 0
                        audit(state, doc, "extraction_completed", "system:evidence-worker")
                    await self._change(run_id, job_id, lease, completed)
                    return
                # Retain the operation for the next bounded lease; never resubmit.
                def pause(state: DemoRun, current: EvidenceJob, doc: EvidenceDocument) -> None:
                    current.lease_until = 0
                await self._change(run_id, job_id, lease, pause)
        except LeaseLost:
            return
        except ProviderFailure as exc:
            await self._safe_fail(run_id, job_id, lease, exc.code)
        except (AzureError, TimeoutError):
            # Reconcile an uncertain Cosmos response before attempting a failure write.
            stored = await self.repository.get(run_id)
            if stored.state.evidence.jobs[job_id].status == "completed":
                return
            await self._safe_fail(run_id, job_id, lease, "evidence_dependency_unavailable")
        except BusinessError as exc:
            await self._safe_fail(run_id, job_id, lease, exc.code)

    async def _safe_fail(self, run_id: str, job_id: str, lease: str, code: str) -> None:
        try:
            await self._fail(run_id, job_id, lease, code)
        except LeaseLost:
            return

    async def run_once(self, run_id: str) -> int:
        self.require_run(run_id)
        stored = await self.repository.get(run_id)
        candidates = [
            job_id for job_id, job in sorted(stored.state.evidence.jobs.items())
            if job.status == "queued" or job.status == "running" and job.lease_until <= self.clock()
        ][:self.settings.evidence_worker_batch_size]
        processed = 0
        for job_id in candidates:
            lease = await self._acquire(run_id, job_id)
            if lease:
                await self._process(run_id, job_id, lease)
                processed += 1
        return processed


async def _main(run_id: str, iterations: int) -> None:
    settings = get_settings()
    try:
        RUN_ID.validate_python(run_id)
    except ValidationError:
        raise BusinessError("invalid_run", "Use one explicit valid run identifier.", 422) from None
    if not capabilities(settings, run_id).worker_enabled:
        raise BusinessError("evidence_worker_disabled", "Worker requires explicit enablement and a configured run allowlist.", 503)
    config = extraction_config(settings)
    async with ManagedIdentityCredential(client_id=settings.azure_client_id or None) as credential:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15), follow_redirects=False, trust_env=False) as client:
            worker = EvidenceWorker(
                CosmosBusinessRepository(), AzureEvidenceBlobs(settings),
                CUEvidenceProvider(CUService(config, client, credential)), settings,
            )
            for iteration in range(iterations):
                async with asyncio.timeout(
                    settings.evidence_worker_batch_size * (settings.evidence_worker_lease_seconds + 30),
                ):
                    processed = await worker.run_once(run_id)
                logger.info("evidence_worker_batch processed=%d", processed)
                if iteration + 1 < iterations:
                    await asyncio.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded, explicitly run-scoped evidence worker; no run discovery.")
    parser.add_argument("--run-id", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true")
    group.add_argument("--iterations", type=int, choices=range(1, 61), metavar="1..60")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_main(args.run_id, 1 if args.once else args.iterations))
    except (BusinessError, AzureError, TimeoutError, HTTPException, httpx.HTTPError):
        logger.error("evidence_worker_stopped: durable processing/failure persistence could not be confirmed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
