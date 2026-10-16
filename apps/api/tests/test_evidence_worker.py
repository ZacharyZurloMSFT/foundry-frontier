import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError

from app.business.evidence_models import JobCommand
from app.business.evidence_provider import CUEvidenceProvider, ProviderFailure, project_fields
from app.business.evidence_worker import EvidenceWorker, LeaseLost
from app.business.models import BusinessError
from app.content_understanding import ANALYZER_ID, API_VERSION, OWNER, CUConfig, CUService
from test_evidence import evidence as evidence_fixture, register, review_command

evidence = evidence_fixture

RESULT = {
    "analyzerId": ANALYZER_ID,
    "contents": [{
        "markdown": "Private original extracted text, not a confirmed field.",
        "fields": {
            "claim_id": {"type": "string", "valueString": "CLM-2001", "confidence": 0.98,
                         "source": "D(1,0.1,0.2,0.3,0.4)"},
            "policy_id": {"type": "string", "valueString": "POL-1001"},
            "policyholder_id": {"type": "string", "valueString": "PH-999"},
            "estimate_total_usd": {"type": "number", "valueNumber": 1234.5, "confidence": 0.65,
                                   "spans": [{"offset": 42, "length": 6}],
                                   "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 2, 3]}]},
        },
    }],
}


class Provider:
    def __init__(self):
        self.submissions = 0
        self.polls = 0
        self.result = RESULT
        self.poll_failure = None
        self.submit_failure = None
        self.preflight_failure = None
        self.submitting = asyncio.Event()
        self.block_submit = None
        self.after_poll = None

    async def preflight(self):
        if self.preflight_failure:
            raise ProviderFailure(self.preflight_failure)
        return {"endpoint": "https://unit.cognitiveservices.azure.com",
                "analyzer_id": ANALYZER_ID, "model": "gpt-4.1", "api_version": API_VERSION}

    async def submit(self, data, filename, media_type):
        self.submissions += 1
        self.submitting.set()
        if self.block_submit:
            await self.block_submit.wait()
        if self.submit_failure:
            raise ProviderFailure(self.submit_failure)
        return f"https://unit.cognitiveservices.azure.com/contentunderstanding/analyzerResults/test?api-version={API_VERSION}"

    async def poll(self, location, provenance):
        self.polls += 1
        if self.poll_failure:
            raise ProviderFailure(self.poll_failure)
        if self.after_poll:
            await self.after_poll()
        return self.result


def worker(evidence, provider=None, **kwargs):
    service, repository, blobs = evidence
    config = service.settings
    config.evidence_worker_enabled = config.evidence_extraction_enabled = config.cu_enabled = True
    config.cu_endpoint = "https://unit.cognitiveservices.azure.com"
    config.cu_completion_deployment = "existing-completion"
    config.evidence_worker_run_ids = "run-a"
    provider = provider or Provider()
    return EvidenceWorker(repository, blobs, provider, config, poll_interval=0, poll_limit=1, **kwargs), provider


async def state_job(evidence, result):
    state = (await evidence[1].get("run-a")).state
    return state, state.evidence.jobs[result.job.job_id], state.evidence.documents[result.document.document_id]


@pytest.mark.asyncio
async def test_worker_completion_private_raw_projection_and_review_separation(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    assert await instance.run_once("run-a") == 1
    state, job, document = await state_job(evidence, result)
    assert job.status == "completed" and job.phase == "done" and job.attempts == 1
    assert job.lease_id is None and provider.submissions == provider.polls == 1
    assert document.version == 2 and document.review_version == 0
    assert set(document.fields) == {
        "claim_id", "policy_id", "policyholder_id", "loss_date", "estimate_total_usd", "invoice_total_usd",
    }
    assert document.fields["invoice_total_usd"].missing
    amount = document.fields["estimate_total_usd"]
    assert amount.value == 1234.5 and amount.confidence == 0.65
    assert amount.source_spans == [
        {"spans": [{"offset": 42, "length": 6}]},
        {"boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 2, 3]}]},
    ]
    raw = await evidence[2].get(document.raw_result)
    assert json.loads(raw)["result"] == RESULT
    assert document.confirmed_fields == {}
    assert state.claims["CLM-2001"].version == 2  # Only registration changes case history/version.
    assert await instance.run_once("run-a") == 0
    service = evidence[0]
    reviewed = await service.review("run-a", "employee-a", document.document_id, review_command(document, [
        {"name": "estimate_total_usd", "decision": "accept"},
        {"name": "invoice_total_usd", "decision": "correct", "value": 500},
    ]))
    assert reviewed.confirmed_fields["estimate_total_usd"].value == 1234.5
    assert reviewed.confirmed_fields["invoice_total_usd"].extraction_sha256 == document.raw_result.sha256
    assert reviewed.fields["invoice_total_usd"].value is None
    assert await evidence[2].get(document.raw_result) == raw
    with pytest.raises(BusinessError) as error:
        await service.review("run-a", "employee-a", document.document_id, review_command(reviewed, [
            {"name": "policyholder_id", "decision": "accept"},
        ]))
    assert error.value.code == "evidence_reference_conflict"
    public = (await service.get_job("run-a", "customer-a", job.job_id)).model_dump_json()
    assert "https://" not in public and "lease" not in public and "operation" not in public


@pytest.mark.asyncio
async def test_two_worker_race_has_one_owner_and_one_submission(evidence):
    result = await register(evidence)
    provider = Provider()
    provider.block_submit = asyncio.Event()
    first, _ = worker(evidence, provider)
    second, _ = worker(evidence, provider)
    pending = asyncio.create_task(first.run_once("run-a"))
    await provider.submitting.wait()
    assert await second.run_once("run-a") == 0
    provider.block_submit.set()
    assert await pending == 1
    _, job, _ = await state_job(evidence, result)
    assert job.attempts == provider.submissions == 1 and job.status == "completed"


@pytest.mark.asyncio
async def test_restart_polls_persisted_operation_without_second_submission(evidence):
    result = await register(evidence)
    first, provider = worker(evidence)
    provider.result = None
    await first.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "running" and job.phase == "polling" and job.operation_location and job.lease_until == 0
    second, _ = worker(evidence, provider)
    provider.result = RESULT
    await second.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "completed" and job.attempts == 2
    assert provider.submissions == 1 and provider.polls == 2


@pytest.mark.asyncio
async def test_pending_operation_exhausts_attempts_without_exceeding_bound_or_resubmitting(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.result = None
    for _ in range(3):
        await instance.run_once("run-a")
    await instance.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.attempts == job.max_attempts == 3 and job.status == "failed"
    assert job.error_code == "cu_attempts_exhausted" and provider.submissions == 1


@pytest.mark.asyncio
async def test_run_aggregate_enforces_two_active_worker_leases(evidence):
    first = await register(evidence)
    second = await register(evidence, key="second", version=2)
    third = await register(evidence, key="third", version=3)
    instance, provider = worker(evidence)
    assert await instance._acquire("run-a", first.job.job_id)
    assert await instance._acquire("run-a", second.job.job_id)
    assert await instance._acquire("run-a", third.job.job_id) is None
    assert provider.submissions == 0


@pytest.mark.asyncio
async def test_restart_never_completes_when_original_source_is_missing(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.result = None
    await instance.run_once("run-a")
    _, _, document = await state_job(evidence, result)
    del evidence[2].items[document.source_blob.key]
    provider.result = RESULT
    await instance.run_once("run-a")
    _, job, document = await state_job(evidence, result)
    assert job.status == "failed" and job.error_code == "evidence_integrity"
    assert document.raw_result is None and provider.submissions == 1


@pytest.mark.asyncio
async def test_uncertain_post_is_durable_terminal_and_manual_retry_refused(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.submit_failure = "cu_submission_uncertain"
    await instance.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "failed" and job.submission_uncertain and job.phase == "submitting"
    assert job.error_code == "cu_submission_uncertain"
    assert provider.submissions == 1 and provider.polls == 0
    assert await instance.run_once("run-a") == 0
    with pytest.raises(BusinessError) as error:
        await evidence[0].job_command("run-a", "employee-a", job.job_id, JobCommand(expected_version=job.version), cancel=False)
    assert error.value.code == "cu_submission_uncertain"


@pytest.mark.asyncio
async def test_process_death_between_post_and_operation_save_never_reposts(evidence):
    result = await register(evidence)
    now = [1000.0]
    provider = Provider()
    provider.block_submit = asyncio.Event()
    first, _ = worker(evidence, provider, clock=lambda: now[0])
    pending = asyncio.create_task(first.run_once("run-a"))
    await provider.submitting.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    _, job, _ = await state_job(evidence, result)
    assert job.phase == "submitting" and job.operation_location is None
    now[0] += 61
    second, _ = worker(evidence, provider, clock=lambda: now[0])
    await second.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "failed" and job.error_code == "cu_submission_uncertain"
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_poll_failure_retry_resumes_operation_and_is_attempt_bounded(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.poll_failure = "cu_poll_unavailable"
    for attempt in range(1, 4):
        assert await instance.run_once("run-a") == 1
        _, job, _ = await state_job(evidence, result)
        assert job.status == "failed" and job.attempts == attempt
        assert job.error_code == "cu_poll_unavailable"
        if attempt < 3:
            await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                          JobCommand(expected_version=job.version), cancel=False)
    assert provider.submissions == 1
    with pytest.raises(BusinessError) as error:
        await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                      JobCommand(expected_version=job.version), cancel=False)
    assert error.value.code == "invalid_job_state"


@pytest.mark.asyncio
async def test_failed_preflight_can_retry_without_ever_crossing_paid_boundary(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.preflight_failure = "cu_analyzer_unavailable"
    await instance.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "failed" and job.phase == "ready" and not job.submission_uncertain
    assert provider.submissions == 0
    provider.preflight_failure = None
    await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                  JobCommand(expected_version=job.version), cancel=False)
    await instance.run_once("run-a")
    assert (await state_job(evidence, result))[1].status == "completed"
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_cancel_during_poll_prevents_raw_attachment_and_further_polls(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)

    async def cancel():
        _, job, _ = await state_job(evidence, result)
        await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                      JobCommand(expected_version=job.version), cancel=True)
    provider.after_poll = cancel
    await instance.run_once("run-a")
    _, job, document = await state_job(evidence, result)
    assert job.status == "cancelled" and document.raw_result is None and document.fields == {}
    assert len(evidence[2].items) == 1 and provider.polls == 1
    assert await instance.run_once("run-a") == 0


@pytest.mark.asyncio
async def test_cancel_after_result_blob_write_leaves_safe_orphan_not_completed(evidence):
    result = await register(evidence)
    instance, _ = worker(evidence)

    async def cancel(reference):
        if reference.kind == "result":
            _, job, _ = await state_job(evidence, result)
            await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                          JobCommand(expected_version=job.version), cancel=True)
    evidence[2].after_put = cancel
    await instance.run_once("run-a")
    _, job, document = await state_job(evidence, result)
    assert job.status == "cancelled" and document.raw_result is None
    assert len(evidence[2].items) == 2  # Never delete an immutable object after a losing CAS.


@pytest.mark.asyncio
async def test_stale_lease_cannot_overwrite_newer_owner(evidence):
    result = await register(evidence)
    now = [1000.0]
    first, provider = worker(evidence, clock=lambda: now[0])
    old_lease = await first._acquire("run-a", result.job.job_id)
    now[0] += 61
    second, _ = worker(evidence, provider, clock=lambda: now[0])
    new_lease = await second._acquire("run-a", result.job.job_id)
    assert new_lease != old_lease
    with pytest.raises(LeaseLost):
        await first._change("run-a", result.job.job_id, old_lease, lambda *_: pytest.fail("Do not mutate stale state"))
    await first._process("run-a", result.job.job_id, old_lease)
    assert provider.submissions == 0
    await second._process("run-a", result.job.job_id, new_lease)
    assert (await state_job(evidence, result))[1].status == "completed"


@pytest.mark.asyncio
async def test_result_blob_failure_retains_operation_for_manual_poll_resume(evidence, monkeypatch):
    result = await register(evidence)
    instance, provider = worker(evidence)
    put = evidence[2].put

    async def failing_put(key, data, kind):
        if kind == "result":
            raise ServiceRequestError("private result storage failure")
        return await put(key, data, kind)
    monkeypatch.setattr(evidence[2], "put", failing_put)
    await instance.run_once("run-a")
    _, job, document = await state_job(evidence, result)
    assert job.status == "failed" and job.operation_location and not job.submission_uncertain
    assert job.error_code == "evidence_dependency_unavailable" and document.raw_result is None
    assert "private" not in job.error_message
    monkeypatch.setattr(evidence[2], "put", put)
    await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                  JobCommand(expected_version=job.version), cancel=False)
    await instance.run_once("run-a")
    assert (await state_job(evidence, result))[1].status == "completed" and provider.submissions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["polling", "done"])
async def test_uncertain_cosmos_response_reconciles_saved_operation_or_completion(evidence, monkeypatch, phase):
    result = await register(evidence)
    instance, provider = worker(evidence)
    repository = evidence[1]
    replace = repository.replace
    failed = False

    async def response_lost(state, etag):
        nonlocal failed
        response = await replace(state, etag)
        if state.evidence.jobs[result.job.job_id].phase == phase and not failed:
            failed = True
            raise ServiceRequestError("commit succeeded but response lost")
        return response
    monkeypatch.setattr(repository, "replace", response_lost)
    await instance.run_once("run-a")
    _, job, document = await state_job(evidence, result)
    assert failed
    if phase == "polling":
        assert job.status == "failed" and job.operation_location and not job.submission_uncertain
        await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                      JobCommand(expected_version=job.version), cancel=False)
        await instance.run_once("run-a")
        _, job, document = await state_job(evidence, result)
    assert job.status == "completed" and document.raw_result and provider.submissions == 1


@pytest.mark.asyncio
async def test_worker_cannot_hide_failure_when_storage_will_not_persist_it(evidence, monkeypatch):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.poll_failure = "cu_poll_unavailable"
    replace = evidence[1].replace

    async def cannot_fail(state, etag):
        if state.evidence.jobs[result.job.job_id].status == "failed":
            raise ServiceRequestError("failure write unavailable")
        return await replace(state, etag)
    monkeypatch.setattr(evidence[1], "replace", cannot_fail)
    with pytest.raises(ServiceRequestError):
        await instance.run_once("run-a")


@pytest.mark.asyncio
async def test_worker_gate_is_default_off_allowlisted_run_scoped_and_uses_no_discovery(evidence, monkeypatch):
    result = await register(evidence)
    provider = Provider()
    instance = EvidenceWorker(evidence[1], evidence[2], provider, evidence[0].settings)
    with pytest.raises(BusinessError) as error:
        await instance.run_once("run-a")
    assert error.value.code == "evidence_worker_disabled"
    instance, _ = worker(evidence, provider)
    for run_id in ("run-b", "missing", "*"):
        with pytest.raises(BusinessError):
            await instance.run_once(run_id)
    assert provider.submissions == 0
    assert (await state_job(evidence, result))[1].status == "queued"
    instance.settings.cu_enabled = False
    with pytest.raises(BusinessError):
        await instance.run_once("run-a")


def test_projection_bounds_and_untrusted_values_never_bloat_aggregate_or_expose_urls():
    result = {"contents": [{"fields": {
        "claim_id": {"type": "string", "valueString": "x" * 100_000,
                     "source": "https://provider.invalid/private?token=sensitive"},
        "estimate_total_usd": {"type": "number", "valueNumber": True,
                              "confidence": float("nan"), "spans": ["x" * 100_000]},
        "invoice_total_usd": {"type": "number", "valueNumber": 10 ** 400},
    }}]}
    fields = project_fields(result)
    assert all(field.missing for field in fields.values())
    assert len(json.dumps({name: field.model_dump() for name, field in fields.items()})) < 2000
    assert "provider.invalid" not in str(fields)


@pytest.mark.asyncio
async def test_large_raw_extraction_stays_in_blob_not_cosmos(evidence):
    result = await register(evidence)
    instance, provider = worker(evidence)
    provider.result = {**RESULT, "fullText": "x" * 2_000_000}
    await instance.run_once("run-a")
    state, job, document = await state_job(evidence, result)
    assert job.status == "completed" and document.raw_result.size_bytes > 2_000_000
    assert len(state.model_dump_json()) < 100_000


@pytest.mark.asyncio
async def test_native_cu_adapter_reuses_analyzer_mapping_guards_and_base64_request():
    calls = []
    location = f"https://unit.cognitiveservices.azure.com/contentunderstanding/analyzerResults/native?api-version={API_VERSION}"
    from app.content_understanding import REQUIRED_FIELD_TYPES
    analyzer = {
        "analyzerId": ANALYZER_ID, "status": "ready",
        "tags": {"owner": OWNER, "schema": "insurance-claims-v1"},
        "fieldSchema": {"name": "NorthwindInsuranceClaimsV1",
                        "fields": {name: {"type": kind} for name, kind in REQUIRED_FIELD_TYPES.items()}},
        "models": {"completion": "gpt-4.1"},
    }

    async def transport(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={}, headers={"Operation-Location": location})
        if "/analyzers/" in str(request.url):
            return httpx.Response(200, json=analyzer)
        return httpx.Response(200, json={"status": "succeeded", "result": RESULT})
    config = CUConfig(enabled=True, endpoint="https://unit.cognitiveservices.azure.com",
                      completion_deployment="existing-model", api_key="test-not-real")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = CUEvidenceProvider(CUService(config, client))
        provenance = await adapter.preflight()
        operation = await adapter.submit(b"source bytes", "source.pdf", "application/pdf")
        assert operation == location and await adapter.poll(operation, provenance) == RESULT
        post = json.loads(calls[1].content)
        assert post["inputs"] == [{"name": "source.pdf", "mimeType": "application/pdf", "data": "c291cmNlIGJ5dGVz"}]
        assert post["modelDeployments"] == {"gpt-4.1": "existing-model"}
        assert "url" not in post["inputs"][0]
        with pytest.raises(ProviderFailure):
            await adapter.poll("https://evil.invalid/token", provenance)
        with pytest.raises(ProviderFailure) as error:
            await adapter.poll(location, {**provenance, "endpoint": "https://different.invalid"})
        assert error.value.code == "cu_configuration_changed"


@pytest.mark.asyncio
async def test_native_submit_transport_failure_is_uncertain_not_retryable():
    service = AsyncMock()
    service.endpoint = "https://unit.cognitiveservices.azure.com"
    service.config.analyzer_id = ANALYZER_ID
    service.request.side_effect = httpx.ReadTimeout("credential/provider details")
    adapter = CUEvidenceProvider(service)
    with pytest.raises(ProviderFailure) as error:
        await adapter.submit(b"source", "source.pdf", "application/pdf")
    assert error.value.code == "cu_submission_uncertain"
    assert "credential" not in str(error.value)


def test_all_evidence_operator_switches_are_disabled_by_default():
    from app.settings import Settings
    defaults = Settings(_env_file=None)
    assert not any((
        defaults.evidence_enabled, defaults.evidence_uploads_enabled,
        defaults.evidence_extraction_enabled, defaults.evidence_worker_enabled,
    ))
    assert defaults.evidence_worker_run_ids == ""


def test_cli_requires_explicit_run_and_returns_nonzero_for_unstored_failure(monkeypatch):
    import sys

    from app.business import evidence_worker
    main = AsyncMock(side_effect=ServiceRequestError("private credential details"))
    monkeypatch.setattr(evidence_worker, "_main", main)
    monkeypatch.setattr(sys, "argv", ["evidence_worker", "--run-id", "run-a", "--once"])
    assert evidence_worker.main() == 1
    main.assert_awaited_once_with("run-a", 1)
    monkeypatch.setattr(sys, "argv", ["evidence_worker", "--once"])
    with pytest.raises(SystemExit) as error:
        evidence_worker.main()
    assert error.value.code == 2
