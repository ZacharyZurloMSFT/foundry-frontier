"""Versioned manual business APIs. Legacy demo routes remain separate."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from ..settings import get_settings

from .auth import Principal, authenticated_principal
from .evidence_blobs import AzureEvidenceBlobs, EvidenceBlobs
from .evidence_models import (
    EvidenceDocumentView, EvidenceJobView, EvidenceResult, EvidenceSampleRequest,
    EvidenceStatus, JobCommand, ReviewCommand,
)
from .evidence_service import MAX_UPLOAD_BYTES, EvidenceService
from .models import (
    AccountContext, AssignAdjuster, BusinessError, ClaimRecord, ClaimStatus,
    CompleteTask, CreateNote, CreateTask, ErrorResponse, LossReport, LossResult, LossSubmission,
    Page, PageRequest, PolicySnapshot, TimelineEvent, TransitionCommand,
)
from .service import InsuranceBusinessService
from .store import CosmosBusinessRepository

logger = logging.getLogger(__name__)


def error_response(error: BusinessError, correlation_id: str) -> JSONResponse:
    logger.warning("insurance_business_failed code=%s correlation_id=%s", error.code, correlation_id)
    headers = {"X-Correlation-ID": correlation_id, "Cache-Control": "no-store"}
    if error.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error.status_code == 429:
        headers["Retry-After"] = "1"
    return JSONResponse(
        {"error": {"code": error.code, "message": str(error), "correlation_id": correlation_id}},
        status_code=error.status_code, headers=headers,
    )


class BusinessRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            correlation_id = str(uuid.uuid4())
            try:
                response = await original(request)
            except BusinessError as exc:
                return error_response(exc, correlation_id)
            except RequestValidationError:
                return error_response(BusinessError(
                    "invalid_request", "Check required fields, identifiers and supported values.", 422,
                ), correlation_id)
            except CosmosHttpResponseError as exc:
                if exc.status_code == 429:
                    return error_response(BusinessError(
                        "storage_throttled", "Storage is busy. Retry the original command after waiting.", 429,
                    ), correlation_id)
                return error_response(BusinessError(
                    "storage_unavailable", "Storage did not confirm this operation. Refresh before retrying a change.", 503,
                ), correlation_id)
            except (AzureError, TimeoutError):
                return error_response(BusinessError(
                    "dependency_unavailable", "A required dependency is unavailable. No successful outcome was confirmed.", 503,
                ), correlation_id)
            except ValidationError:
                return error_response(BusinessError(
                    "stored_record_invalid", "The stored record could not be validated; no fallback record was used.", 500,
                ), correlation_id)
            response.headers["X-Correlation-ID"] = correlation_id
            response.headers["Cache-Control"] = "no-store"
            return response
        return handle


router = APIRouter(
    prefix="/api/v1", tags=["insurance-business"], route_class=BusinessRoute,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429, 500, 503)},
)


@dataclass(frozen=True)
class BusinessContext:
    run_id: str
    subject_id: str
    service: InsuranceBusinessService


def business_service() -> InsuranceBusinessService:
    return InsuranceBusinessService(CosmosBusinessRepository())


async def context(
    principal: Annotated[Principal, Depends(authenticated_principal)],
    run_id: Annotated[str, Header(alias="X-Demo-Run", pattern=r"^[A-Za-z0-9_-]{1,64}$")],
    service: Annotated[InsuranceBusinessService, Depends(business_service)],
) -> BusinessContext:
    return BusinessContext(run_id, principal.subject_id, service)


Context = Annotated[BusinessContext, Depends(context)]
ClaimId = Annotated[str, Path(pattern=r"^CLM-\d+$")]
PolicyId = Annotated[str, Path(pattern=r"^POL-\d+$")]


def page_request(
    offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageRequest:
    return PageRequest(offset=offset, limit=limit)


Pagination = Annotated[PageRequest, Depends(page_request)]


def claim_headers(response: Response, claim: ClaimRecord) -> None:
    response.headers["ETag"] = f'"{claim.version}"'


@router.get("/me")
async def me(ctx: Context) -> AccountContext:
    return await ctx.service.account_context(ctx.run_id, ctx.subject_id)


@router.get("/me/policies")
async def policies(ctx: Context, page: Pagination) -> Page[PolicySnapshot]:
    return await ctx.service.list_policies(ctx.run_id, ctx.subject_id, page)


@router.get("/policies/{policy_id}")
async def policy(policy_id: PolicyId, ctx: Context) -> PolicySnapshot:
    return await ctx.service.get_policy(ctx.run_id, ctx.subject_id, policy_id)


@router.get("/me/claims")
@router.get("/claims")
async def claims(ctx: Context, page: Pagination, status: ClaimStatus | None = None,
                 assigned_to: Annotated[str | None, Query(min_length=1, max_length=200)] = None) -> Page[ClaimRecord]:
    return await ctx.service.list_claims(ctx.run_id, ctx.subject_id, page, status, assigned_to)


@router.get("/claims/{claim_id}")
async def claim(claim_id: ClaimId, ctx: Context, response: Response) -> ClaimRecord:
    result = await ctx.service.get_claim(ctx.run_id, ctx.subject_id, claim_id)
    claim_headers(response, result)
    return result


@router.post("/claims", status_code=201, responses={200: {"model": LossResult, "description": "Original submission replayed"}})
async def report_loss(
    report: LossReport, ctx: Context, response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")],
) -> LossResult:
    result = await ctx.service.report_loss(
        ctx.run_id, ctx.subject_id, LossSubmission(idempotency_key=idempotency_key, report=report),
    )
    response.status_code = 200 if result.replayed else 201
    response.headers["Location"] = f"/api/v1/claims/{result.claim.claim_id}"
    claim_headers(response, result.claim)
    return result


@router.get("/claims/{claim_id}/timeline")
async def timeline(claim_id: ClaimId, ctx: Context, page: Pagination) -> Page[TimelineEvent]:
    result = await ctx.service.get_claim(ctx.run_id, ctx.subject_id, claim_id)
    end = page.offset + page.limit
    return Page(items=result.timeline[page.offset:end], next_offset=end if end < len(result.timeline) else None)


@router.post("/claims/{claim_id}/transitions")
async def transition(claim_id: ClaimId, command: TransitionCommand, ctx: Context, response: Response) -> ClaimRecord:
    result = await ctx.service.transition_claim(ctx.run_id, ctx.subject_id, claim_id, command)
    claim_headers(response, result)
    return result


@router.post("/claims/{claim_id}/tasks")
async def create_task(claim_id: ClaimId, command: CreateTask, ctx: Context, response: Response) -> ClaimRecord:
    result = await ctx.service.create_task(ctx.run_id, ctx.subject_id, claim_id, command)
    claim_headers(response, result)
    return result


@router.patch("/claims/{claim_id}/tasks/{task_id}")
async def complete_task(
    claim_id: ClaimId, task_id: Annotated[str, Path(pattern=r"^task-[0-9a-f-]{36}$")],
    command: CompleteTask, ctx: Context, response: Response,
) -> ClaimRecord:
    result = await ctx.service.complete_task(ctx.run_id, ctx.subject_id, claim_id, task_id, command)
    claim_headers(response, result)
    return result


@router.post("/claims/{claim_id}/notes")
async def create_note(claim_id: ClaimId, command: CreateNote, ctx: Context, response: Response) -> ClaimRecord:
    result = await ctx.service.create_note(ctx.run_id, ctx.subject_id, claim_id, command)
    claim_headers(response, result)
    return result


@router.post("/claims/{claim_id}/assignment")
async def assign_adjuster(claim_id: ClaimId, command: AssignAdjuster, ctx: Context, response: Response) -> ClaimRecord:
    result = await ctx.service.assign_adjuster(ctx.run_id, ctx.subject_id, claim_id, command)
    claim_headers(response, result)
    return result


def evidence_blobs() -> EvidenceBlobs:
    return AzureEvidenceBlobs(get_settings())


def evidence_service(
    ctx: Context, blobs: Annotated[EvidenceBlobs, Depends(evidence_blobs)],
) -> EvidenceService:
    return EvidenceService(ctx.service.repository, blobs, get_settings())


Evidence = Annotated[EvidenceService, Depends(evidence_service)]
DocumentId = Annotated[str, Path(pattern=r"^doc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
JobId = Annotated[str, Path(pattern=r"^job-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
EvidenceKey = Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


@router.get("/evidence/status")
async def evidence_status(ctx: Context, evidence: Evidence) -> EvidenceStatus:
    return await evidence.status(ctx.run_id, ctx.subject_id)


@router.get("/evidence/samples")
async def evidence_samples(ctx: Context, evidence: Evidence) -> dict:
    return await evidence.samples(ctx.run_id, ctx.subject_id)


@router.get("/claims/{claim_id}/documents")
async def evidence_documents(claim_id: ClaimId, ctx: Context, evidence: Evidence,
                             page: Pagination) -> Page[EvidenceDocumentView]:
    return await evidence.list_documents(ctx.run_id, ctx.subject_id, claim_id, page)


def evidence_location(response: Response, result: EvidenceResult) -> None:
    response.headers["Location"] = f"/api/v1/jobs/{result.job.job_id}"


@router.post("/claims/{claim_id}/documents/samples", status_code=202)
async def evidence_sample(
    claim_id: ClaimId, command: EvidenceSampleRequest, ctx: Context, evidence: Evidence,
    response: Response, idempotency_key: EvidenceKey,
) -> EvidenceResult:
    result = await evidence.register_sample(
        ctx.run_id, ctx.subject_id, claim_id, command.filename, command.expected_version,
        idempotency_key, command.synthetic_acknowledged,
    )
    evidence_location(response, result)
    return result


@router.post("/claims/{claim_id}/documents", status_code=202)
async def evidence_upload(
    claim_id: ClaimId, ctx: Context, evidence: Evidence, request: Request,
    response: Response, idempotency_key: EvidenceKey,
    filename: Annotated[str, Query(min_length=1, max_length=160)],
    expected_version: Annotated[int, Query(ge=1)],
    synthetic_data: Annotated[str | None, Header(alias="X-Synthetic-Data")] = None,
) -> EvidenceResult:
    # Authenticate the server-owned case before consuming even the first body chunk.
    await evidence.authorise(ctx.run_id, ctx.subject_id, claim_id, write=True)
    evidence.require_uploads(sample=False)
    if synthetic_data != "true":
        raise BusinessError("synthetic_acknowledgement_required", "Send X-Synthetic-Data: true for synthetic data only.", 422)
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit() or len(length) > 12:
            raise BusinessError("invalid_content_length", "Invalid Content-Length.", 400)
        if int(length) > MAX_UPLOAD_BYTES:
            raise BusinessError("document_too_large", "Document limit is 10485760 bytes.", 413)
    data = bytearray()
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                    raise BusinessError("document_too_large", "Document limit is 10485760 bytes.", 413)
                data.extend(chunk)
    except (TimeoutError, ClientDisconnect):
        raise BusinessError("upload_incomplete", "Upload did not complete; no evidence was registered.", 408) from None
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    result = await evidence.register(
        ctx.run_id, ctx.subject_id, claim_id, bytes(data), filename, media_type,
        expected_version, idempotency_key, True,
    )
    evidence_location(response, result)
    return result


@router.get("/documents/{document_id}")
async def evidence_document(document_id: DocumentId, ctx: Context, evidence: Evidence) -> EvidenceDocumentView:
    return await evidence.get_document(ctx.run_id, ctx.subject_id, document_id)


@router.get("/documents/{document_id}/content")
async def evidence_content(document_id: DocumentId, ctx: Context, evidence: Evidence) -> Response:
    document, data = await evidence.source(ctx.run_id, ctx.subject_id, document_id)
    return Response(data, media_type=document.media_type, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{document.filename}"',
    })


@router.get("/jobs/{job_id}")
async def evidence_job(job_id: JobId, ctx: Context, evidence: Evidence) -> EvidenceJobView:
    return await evidence.get_job(ctx.run_id, ctx.subject_id, job_id)


@router.post("/jobs/{job_id}/retry")
async def evidence_retry(job_id: JobId, command: JobCommand, ctx: Context, evidence: Evidence) -> EvidenceJobView:
    return await evidence.job_command(ctx.run_id, ctx.subject_id, job_id, command, cancel=False)


@router.post("/jobs/{job_id}/cancel")
async def evidence_cancel(job_id: JobId, command: JobCommand, ctx: Context, evidence: Evidence) -> EvidenceJobView:
    return await evidence.job_command(ctx.run_id, ctx.subject_id, job_id, command, cancel=True)


@router.post("/documents/{document_id}/reviews")
async def evidence_review(document_id: DocumentId, command: ReviewCommand,
                          ctx: Context, evidence: Evidence) -> EvidenceDocumentView:
    return await evidence.review(ctx.run_id, ctx.subject_id, document_id, command)
