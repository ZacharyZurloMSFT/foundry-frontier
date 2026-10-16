import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from azure.core.exceptions import ResourceExistsError, ServiceRequestError
from pydantic import ValidationError

from app.business import evidence_capacity, evidence_service, routes, store
from app.business.evidence_blobs import AzureEvidenceBlobs, blob_key, integrity_error
from app.business.evidence_models import (
    BlobReference, EvidenceSampleRequest, FieldReview, ReviewCommand,
)
from app.business.evidence_service import EvidenceService
from app.business.models import BusinessError, ConcurrentChange
from app.business.service import seed_run
from app.settings import Settings
from test_business_api import api as api_fixture, identity as identity_fixture
from test_business_foundation import ACTORS

PDF = b"%PDF-1.4\nsynthetic source\n%%EOF"
api, identity = api_fixture, identity_fixture


class MemoryBlobs:
    """Test-only implementation with immutable metadata and byte integrity."""
    def __init__(self):
        self.items = {}
        self.put_calls = 0
        self.fail = False
        self.after_put = None

    async def put(self, key, data, kind):
        if self.fail:
            raise ServiceRequestError("private storage detail")
        self.put_calls += 1
        reference = BlobReference(
            key=key, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
            etag="immutable-v1", kind=kind,
        )
        old = self.items.get(key)
        if old is not None and old != (reference, data):
            raise integrity_error()
        self.items[key] = reference, data
        if self.after_put:
            await self.after_put(reference)
        return reference

    async def get(self, reference):
        if self.fail:
            raise ServiceRequestError("private storage detail")
        item = self.items.get(reference.key)
        if item is None or item[0] != reference or hashlib.sha256(item[1]).hexdigest() != reference.sha256:
            raise integrity_error()
        return item[1]


def settings(**changes):
    return Settings(
        _env_file=None, NORTHWIND_EVIDENCE_ENABLED=True, STORAGE_ACCOUNT="privateunit",
        NORTHWIND_EVIDENCE_UPLOADS_ENABLED=True,
        **changes,
    )


@pytest_asyncio.fixture
async def evidence(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(store, "_container", container)
    repository = store.CosmosBusinessRepository()
    await repository.create(seed_run("run-a", ACTORS))
    await repository.create(seed_run("run-b", {"customer-b": ACTORS["customer-a"]}))
    blobs = MemoryBlobs()
    return EvidenceService(repository, blobs, settings()), repository, blobs


async def register(evidence, *, key="evidence-one", data=PDF, version=1, actor="customer-a", **changes):
    return await evidence[0].register(
        "run-a", actor, "CLM-2001", data, changes.pop("filename", "source.pdf"),
        changes.pop("media_type", "application/pdf"), version, key,
        changes.pop("acknowledged", True), **changes,
    )


def review_command(document, fields):
    return ReviewCommand(
        expected_review_version=document.review_version, expected_document_version=document.version,
        fields=fields, synthetic_acknowledged=True,
    )


@pytest.mark.asyncio
async def test_registration_is_atomic_private_and_does_not_change_workflow(evidence):
    service, repository, blobs = evidence
    original = (await repository.get("run-a")).state.claims["CLM-2001"]
    receipt = await register(evidence)
    assert not receipt.replayed and receipt.job.status == "queued"
    assert receipt.document.fields == receipt.document.confirmed_fields == {}
    assert receipt.document.uploaded_by is None
    assert receipt.document.source_mode == "synthetic_upload"
    state = (await repository.get("run-a")).state
    claim = state.claims["CLM-2001"]
    assert claim.version == 2 and len(claim.timeline) == len(original.timeline) + 1
    assert claim.status == original.status and claim.tasks == original.tasks
    assert len(state.evidence.documents) == len(state.evidence.jobs) == len(state.evidence.receipts) == 1
    assert state.evidence.audit[0].actor_id == "customer-a"
    assert blobs.put_calls == 1
    public = receipt.model_dump_json()
    for private in ("source_blob", "operation_location", "lease_id", "privateunit", "raw_result", "customer-a"):
        assert private not in public
    employee = await service.get_document("run-a", "employee-a", receipt.document.document_id)
    assert employee.uploaded_by == "customer-a"


@pytest.mark.asyncio
async def test_concurrent_replays_ignore_later_claim_version_and_key_conflicts(evidence, monkeypatch):
    service, repository, blobs = evidence
    replace = repository.replace
    waiting = 0
    gate = asyncio.Event()

    async def race(state, etag):
        nonlocal waiting
        if etag == "1":
            waiting += 1
            if waiting == 2:
                gate.set()
            await gate.wait()
        return await replace(state, etag)
    # The run-a ETag is 1; run-b is a separate aggregate with ETag 2.
    monkeypatch.setattr(repository, "replace", race)
    first, second = await asyncio.gather(register(evidence), register(evidence))
    assert first.document.document_id == second.document.document_id
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert (await register(evidence, version=999)).replayed
    state = (await repository.get("run-a")).state
    assert len(state.evidence.audit) == 1 and state.claims["CLM-2001"].version == 2
    with pytest.raises(BusinessError) as error:
        await register(evidence, data=PDF.replace(b"source", b"different"), version=999)
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(ConcurrentChange):
        await register(evidence, key="new-key")
    assert len(blobs.items) == 1


@pytest.mark.asyncio
async def test_replay_never_succeeds_without_confirmed_original_blob(evidence):
    _, repository, blobs = evidence
    result = await register(evidence)
    state = (await repository.get("run-a")).state
    source = state.evidence.documents[result.document.document_id].source_blob
    blobs.items.pop(source.key)
    with pytest.raises(BusinessError) as error:
        await register(evidence)
    assert error.value.code == "evidence_integrity"
    assert len((await repository.get("run-a")).state.evidence.documents) == 1


@pytest.mark.asyncio
async def test_blob_failure_never_registers_complete_evidence(evidence):
    _, repository, blobs = evidence
    before = (await repository.get("run-a")).state
    blobs.fail = True
    with pytest.raises(ServiceRequestError):
        await register(evidence)
    assert (await repository.get("run-a")).state == before


@pytest.mark.asyncio
async def test_cas_orphan_is_reconciled_without_deleting_shared_blob(evidence, monkeypatch):
    _, repository, blobs = evidence
    replace = repository.replace
    calls = 0

    async def lose_response(state, etag):
        nonlocal calls
        calls += 1
        result = await replace(state, etag)
        if calls == 1:
            raise ServiceRequestError("response lost after commit")
        return result
    monkeypatch.setattr(repository, "replace", lose_response)
    with pytest.raises(ServiceRequestError):
        await register(evidence)
    retry = await register(evidence, version=99)
    assert retry.replayed and len(blobs.items) == 1
    assert len((await repository.get("run-a")).state.evidence.audit) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,code", [
    ({"filename": "../source.pdf"}, "invalid_filename"),
    ({"data": b""}, "empty_document"),
    ({"data": b"not a PDF"}, "document_type_mismatch"),
    ({"media_type": "image/png"}, "unsupported_document"),
    ({"acknowledged": False}, "synthetic_acknowledgement_required"),
    ({"data": b"%PDF-" + b"x" * 10_485_760 + b"%%EOF"}, "document_too_large"),
    ({"key": "bad key"}, "invalid_idempotency_key"),
])
async def test_source_validation_and_acknowledgement(evidence, changes, code):
    with pytest.raises(BusinessError) as error:
        await register(evidence, **changes)
    assert error.value.code == code
    assert evidence[2].put_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,status", [("customer-b", 404), ("presenter-a", 403), ("unmapped", 403)])
async def test_authorisation_precedes_source_writes(evidence, actor, status):
    with pytest.raises(BusinessError) as error:
        await register(evidence, actor=actor)
    assert error.value.status_code == status and evidence[2].put_calls == 0


@pytest.mark.asyncio
async def test_source_and_job_reads_are_claim_and_run_scoped(evidence):
    service, _, blobs = evidence
    result = await register(evidence)
    doc_id, job_id = result.document.document_id, result.job.job_id
    view, source = await service.source("run-a", "customer-a", doc_id)
    assert source == PDF and view.document_id == doc_id
    for actor, run in (("customer-b", "run-a"), ("customer-b", "run-b")):
        for action in (
            lambda: service.source(run, actor, doc_id),
            lambda: service.get_document(run, actor, doc_id),
            lambda: service.get_job(run, actor, job_id),
        ):
            with pytest.raises(BusinessError) as error:
                await action()
            assert error.value.status_code == 404
    blobs.fail = True
    with pytest.raises(BusinessError) as error:
        await service.source("run-a", "customer-b", doc_id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_samples_use_allowlist_without_cu_and_raw_upload_is_separate_opt_in(evidence):
    service, _, _ = evidence
    service.settings.evidence_uploads_enabled = False
    status = await service.status("run-a", "customer-a")
    assert status.enabled and not status.uploads_enabled
    assert not status.worker_enabled and not status.extraction_enabled
    assert "not been probed" in status.notice
    with pytest.raises(BusinessError, match="not enabled"):
        await register(evidence)
    catalog = await service.samples("run-a", "customer-a")
    assert catalog["items"] and set(catalog["items"][0]) == {"filename", "title", "media_type"}
    result = await service.register_sample(
        "run-a", "customer-a", "CLM-2001", catalog["items"][0]["filename"], 1, "sample-1", True,
    )
    assert result.document.source_mode == "synthetic_sample" and result.job.status == "queued"
    for filename in ("https://other.invalid/source.pdf", "not-listed.pdf"):
        with pytest.raises(BusinessError):
            await service.register_sample("run-a", "customer-a", "CLM-2001", filename, 2, "unknown", True)


@pytest.mark.asyncio
async def test_manual_review_works_offline_and_never_changes_raw_or_claim(evidence):
    service, repository, blobs = evidence
    result = await register(evidence)
    before = (await repository.get("run-a")).state.claims["CLM-2001"]
    service.settings.evidence_enabled = False
    blobs.fail = True
    document = await service.review("run-a", "employee-a", result.document.document_id, review_command(result.document, [
        {"name": "estimate_total_usd", "decision": "correct", "value": 1425.25},
        {"name": "claim_id", "decision": "correct", "value": "CLM-2001"},
        {"name": "loss_date", "decision": "correct", "value": "2026-10-15"},
    ]))
    assert document.review_version == 1 and document.version == 2 and document.fields == {}
    assert document.confirmed_fields["estimate_total_usd"].extraction_sha256 is None
    stored = (await repository.get("run-a")).state
    assert stored.claims["CLM-2001"] == before
    assert stored.evidence.documents[document.document_id].raw_result is None
    assert [event.actor_id for event in stored.evidence.audit[1:]] == ["employee-a"] * 3
    customer = await service.get_document("run-a", "customer-a", document.document_id)
    assert all(field.reviewed_by is None for field in customer.confirmed_fields.values())
    assert "employee-a" not in customer.model_dump_json()
    cleared = await service.review("run-a", "employee-a", document.document_id, review_command(document, [
        {"name": "estimate_total_usd", "decision": "clear"},
    ]))
    assert cleared.confirmed_fields["estimate_total_usd"].value is None
    audit = (await repository.get("run-a")).state.evidence.audit[-1]
    assert audit.before.value == 1425.25 and audit.after.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value,code", [
    ("claim_id", "CLM-999", "evidence_reference_conflict"),
    ("policy_id", "POL-999", "evidence_reference_conflict"),
    ("policyholder_id", "PH-999", "evidence_reference_conflict"),
    ("loss_date", "2026-02-30", "invalid_loss_date"),
    ("loss_date", "20261015", "invalid_loss_date"),
    ("estimate_total_usd", -1, "invalid_amount"),
    ("invoice_total_usd", "123", "invalid_amount"),
])
async def test_invalid_review_values_never_rewrite_case_references(evidence, name, value, code):
    service, repository, _ = evidence
    result = await register(evidence)
    before = (await repository.get("run-a")).state
    with pytest.raises(BusinessError) as error:
        await service.review("run-a", "employee-a", result.document.document_id, review_command(result.document, [
            {"name": name, "decision": "correct", "value": value},
        ]))
    assert error.value.code == code
    assert (await repository.get("run-a")).state == before


@pytest.mark.parametrize("field", [
    {"name": "unknown", "decision": "clear"},
    {"name": "claim_id", "decision": "correct", "value": True},
    {"name": "invoice_total_usd", "decision": "correct", "value": float("nan")},
    {"name": "invoice_total_usd", "decision": "correct", "value": float("inf")},
    {"name": "invoice_total_usd", "decision": "correct", "value": {}},
    {"name": "claim_id", "decision": "correct"},
    {"name": "claim_id", "decision": "accept", "value": "CLM-2001"},
    {"name": "claim_id", "decision": "clear", "value": None},
])
def test_malformed_review_fields_rejected(field):
    with pytest.raises(ValidationError):
        FieldReview.model_validate(field)


@pytest.mark.parametrize("ack", [False, 1, "true", None])
def test_review_and_sample_require_strict_true_acknowledgement(ack):
    with pytest.raises(ValidationError):
        EvidenceSampleRequest(filename="source.pdf", expected_version=1, synthetic_acknowledged=ack)
    with pytest.raises(ValidationError):
        ReviewCommand(expected_review_version=0, expected_document_version=1, synthetic_acknowledged=ack,
                      fields=[{"name": "claim_id", "decision": "clear"}])


@pytest.mark.asyncio
async def test_review_role_versions_duplicates_and_missing_extraction(evidence):
    service, _, _ = evidence
    result = await register(evidence)
    correction = review_command(result.document, [{"name": "invoice_total_usd", "decision": "correct", "value": 300}])
    for actor in ("customer-a", "presenter-a"):
        with pytest.raises(BusinessError) as error:
            await service.review("run-a", actor, result.document.document_id, correction)
        assert error.value.status_code == 403
    with pytest.raises(ValidationError):
        review_command(result.document, [{"name": "claim_id", "decision": "clear"}] * 2)
    with pytest.raises(BusinessError) as error:
        await service.review("run-a", "employee-a", result.document.document_id, review_command(result.document, [
            {"name": "claim_id", "decision": "accept"},
        ]))
    assert error.value.code == "missing_extracted_value"
    outcomes = await asyncio.gather(*(
        service.review("run-a", "employee-a", result.document.document_id, correction) for _ in range(2)
    ), return_exceptions=True)
    assert sum(isinstance(outcome, ConcurrentChange) for outcome in outcomes) == 1
    with pytest.raises(ConcurrentChange):
        await service.review("run-a", "employee-a", result.document.document_id, correction)


@pytest.mark.asyncio
async def test_bounded_documents_audit_and_aggregate(evidence, monkeypatch):
    _, repository, blobs = evidence
    monkeypatch.setattr(evidence_service, "MAX_CLAIM_DOCUMENTS", 1)
    await register(evidence)
    with pytest.raises(BusinessError) as error:
        await register(evidence, key="second", version=2)
    assert error.value.code == "evidence_capacity"
    assert blobs.put_calls == 1
    monkeypatch.setattr(evidence_service, "MAX_CLAIM_DOCUMENTS", 20)
    monkeypatch.setattr(evidence_capacity, "MAX_EVIDENCE_AUDIT", 7)
    with pytest.raises(BusinessError) as error:
        await register(evidence, key="second", version=2)
    assert error.value.code == "evidence_capacity"
    stored = await repository.get("run-a")
    stored.state.claims["CLM-2001"].description = "x" * 1_500_000
    with pytest.raises(BusinessError) as error:
        await repository.replace(stored.state, stored.etag)
    assert error.value.code == "run_capacity"


@pytest.mark.asyncio
async def test_azure_blob_adapter_is_create_only_and_verifies_metadata_etag_hash(monkeypatch):
    data = PDF
    sha = hashlib.sha256(data).hexdigest()
    properties = SimpleNamespace(metadata={"sha256": sha, "size_bytes": str(len(data)), "kind": "source"},
                                 size=len(data), etag="etag-1")
    downloaded = SimpleNamespace()

    async def chunks():
        yield data
    downloaded.chunks = chunks
    client = SimpleNamespace(
        upload_blob=AsyncMock(), get_blob_properties=AsyncMock(return_value=properties),
        download_blob=AsyncMock(return_value=downloaded),
    )
    @asynccontextmanager
    async def private_client(_):
        yield client
    adapter = AzureEvidenceBlobs(settings())
    monkeypatch.setattr(adapter, "_client", private_client)
    key = blob_key("run-a", data, "source")
    reference = await adapter.put(key, data, "source")
    assert client.upload_blob.call_args.kwargs["overwrite"] is False
    assert await adapter.get(reference) == data
    client.upload_blob.side_effect = ResourceExistsError()
    assert await adapter.put(key, data, "source") == reference
    properties.metadata["sha256"] = "corrupt"
    with pytest.raises(BusinessError):
        await adapter.put(key, data, "source")
    properties.metadata["sha256"] = sha
    properties.etag = "changed"
    with pytest.raises(BusinessError):
        await adapter.get(reference)
    properties.etag = "etag-1"
    data = b"corrupt"
    with pytest.raises(BusinessError):
        await adapter.get(reference)
    assert not hasattr(client, "create_container") and not hasattr(client, "delete_blob")


@pytest_asyncio.fixture
async def evidence_api(api, monkeypatch):
    client, repository, identity = api
    config = identity[0]
    config.evidence_enabled = config.evidence_uploads_enabled = True
    config.storage_account = "privateunit"
    blobs = MemoryBlobs()
    monkeypatch.setattr(routes, "get_settings", lambda: config)
    from app import main
    monkeypatch.setitem(main.app.dependency_overrides, routes.evidence_blobs, lambda: blobs)
    return client, repository, identity, blobs


async def post_document(client, **changes):
    return await client.post(
        "/api/v1/claims/CLM-2001/documents",
        params={"filename": changes.pop("filename", "source.pdf"), "expected_version": changes.pop("version", 1)},
        content=changes.pop("data", PDF),
        headers={"Idempotency-Key": "source-one", "X-Synthetic-Data": "true",
                 "Content-Type": "application/pdf", **changes},
    )


@pytest.mark.asyncio
async def test_http_evidence_contract_private_download_pagination_jobs_and_reviews(evidence_api):
    client, _, identity, _ = evidence_api
    status = await client.get("/api/v1/evidence/status")
    assert status.status_code == 200 and status.json()["max_upload_bytes"] == 10_485_760
    upload = await post_document(client)
    assert upload.status_code == 202
    result = upload.json()
    job_id, doc_id = result["job"]["job_id"], result["document"]["document_id"]
    assert upload.headers["Location"] == f"/api/v1/jobs/{job_id}"
    page = (await client.get("/api/v1/claims/CLM-2001/documents")).json()
    assert page == {"items": [result["document"]], "next_offset": None}
    assert (await post_document(client, version=99)).json()["replayed"]
    content = await client.get(f"/api/v1/documents/{doc_id}/content")
    assert content.content == PDF and content.headers["Cache-Control"] == "no-store"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert content.headers["Content-Disposition"] == 'attachment; filename="source.pdf"'
    assert (await client.get(f"/api/v1/jobs/{job_id}")).json() == result["job"]
    assert (await client.get(f"/api/v1/documents/{doc_id}")).json() == result["document"]
    command = {"expected_review_version": 0, "expected_document_version": 1,
               "synthetic_acknowledged": True,
               "fields": [{"name": "invoice_total_usd", "decision": "correct", "value": 600}]}
    assert (await client.post(f"/api/v1/documents/{doc_id}/reviews", json=command)).status_code == 403
    from test_business_api import EMPLOYEE
    headers = {"Authorization": f"Bearer {identity[1](EMPLOYEE)}"}
    reviewed = await client.post(f"/api/v1/documents/{doc_id}/reviews", json=command, headers=headers)
    assert reviewed.status_code == 200 and reviewed.json()["review_version"] == 1
    assert (await client.post(f"/api/v1/documents/{doc_id}/reviews", json=command, headers=headers)).status_code == 409
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel", json={"expected_version": 1})).status_code == 403
    cancelled = await client.post(f"/api/v1/jobs/{job_id}/cancel", json={"expected_version": 1}, headers=headers)
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_http_rejects_unauthorised_body_before_streaming(evidence_api, monkeypatch):
    client, _, identity, blobs = evidence_api
    from starlette.requests import Request
    from test_business_api import CUSTOMER_B, PRESENTER

    async def unread(_):
        pytest.fail("Unauthorised request body must not be read")
        yield b""
    monkeypatch.setattr(Request, "stream", unread)
    for actor, expected in ((CUSTOMER_B, 404), (PRESENTER, 403)):
        response = await post_document(client, Authorization=f"Bearer {identity[1](actor)}")
        assert response.status_code == expected
    response = await post_document(client, **{"X-Demo-Run": "run-b"})
    assert response.status_code == 403
    assert blobs.put_calls == 0


@pytest.mark.asyncio
async def test_http_upload_bounds_opt_in_ack_and_safe_storage_failure(evidence_api):
    client, repository, identity, blobs = evidence_api
    for headers, expected in (
        ({"X-Synthetic-Data": "false"}, 422),
        ({"Content-Length": "10485761"}, 413),
        ({"Content-Length": "invalid"}, 400),
    ):
        assert (await post_document(client, **headers)).status_code == expected
    assert (await post_document(client, data=b"not-pdf")).status_code == 415
    identity[0].evidence_uploads_enabled = False
    assert (await post_document(client)).status_code == 503
    identity[0].evidence_uploads_enabled = True
    blobs.fail = True
    response = await post_document(client)
    assert response.status_code == 503 and "private storage detail" not in response.text
    assert (await repository.get("run-a")).state.evidence.documents == {}
    assert (await client.get("/api/v1/evidence/status", headers={"X-Demo-Run": "unknown"})).status_code == 404


@pytest.mark.asyncio
async def test_http_stream_cap_and_timeout_do_not_register_partial_evidence(evidence_api, monkeypatch):
    client, repository, _, blobs = evidence_api
    from starlette.requests import Request
    original_stream = Request.stream

    async def over_limit(_):
        yield b"%PDF-" + b"x" * 6_000_000
        yield b"x" * 6_000_000
    monkeypatch.setattr(Request, "stream", over_limit)
    response = await post_document(client)
    assert response.status_code == 413

    async def timeout(_):
        yield b"%PDF-"
        raise TimeoutError
    monkeypatch.setattr(Request, "stream", timeout)
    response = await post_document(client)
    assert response.status_code == 408
    monkeypatch.setattr(Request, "stream", original_stream)
    assert blobs.put_calls == 0 and (await repository.get("run-a")).state.evidence.documents == {}


@pytest.mark.asyncio
async def test_http_sample_contract_protected_acknowledged_and_available_without_cu(evidence_api):
    client, _, identity, _ = evidence_api
    identity[0].evidence_uploads_enabled = False
    sample = (await client.get("/api/v1/evidence/samples")).json()["items"][0]
    body = {"filename": sample["filename"], "expected_version": 1, "synthetic_acknowledged": True}
    response = await client.post("/api/v1/claims/CLM-2001/documents/samples", json=body,
                                 headers={"Idempotency-Key": "sample"})
    assert response.status_code == 202 and response.json()["document"]["source_mode"] == "synthetic_sample"
    invalid = await client.post("/api/v1/claims/CLM-2001/documents/samples",
                                json={**body, "synthetic_acknowledged": 1},
                                headers={"Idempotency-Key": "sample"})
    assert invalid.status_code == 422
    assert (await client.get("/api/v1/evidence/samples", headers={"Authorization": "invalid"})).status_code == 401


@pytest.mark.asyncio
async def test_old_records_default_evidence_without_fixture_fallback(evidence):
    _, repository, _ = evidence
    raw = store._document((await repository.get("run-a")).state)
    del raw["state"]["evidence"]
    parsed = store._stored({**raw, "_etag": "old"}, "run-a")
    assert parsed.state.evidence.documents == {}
    assert json.loads(parsed.state.model_dump_json())["evidence"]["jobs"] == {}
