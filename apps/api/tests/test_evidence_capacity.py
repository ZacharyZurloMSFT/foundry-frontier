import asyncio
import copy

import pytest

from app.business import evidence_capacity, evidence_service, store
from app.business.evidence_capacity import encoded_size, reservations, worker_audit_slots
from app.business.evidence_models import EvidenceJob, FieldReview, JobCommand, MAX_PROJECTED_FIELDS_BYTES
from app.business.models import BusinessError, CreateNote, CreateTask
from test_business_foundation import submission
from test_evidence import PDF, evidence as evidence_fixture, register, review_command
from test_evidence_worker import Provider, RESULT, state_job, worker

evidence = evidence_fixture


async def admit_forty_attempts(evidence):
    service, repository, blobs = evidence
    admitted = []
    for claim_id in ("CLM-2001", "CLM-2002"):
        for index in range(20):
            state = (await repository.get("run-a")).state
            calls = blobs.put_calls
            try:
                admitted.append(await service.register(
                    "run-a", "employee-a", claim_id,
                    PDF.replace(b"source", f"source-{claim_id}-{index}".encode()),
                    "source.pdf", "application/pdf", state.claims[claim_id].version,
                    f"capacity-{claim_id}-{index}", True,
                ))
            except BusinessError as exc:
                assert exc.status_code == 409 and exc.code in ("evidence_capacity", "run_capacity")
                assert blobs.put_calls == calls  # Admission fails before private source writes.
    assert len(admitted) == evidence_service.MAX_DOCUMENTS == 8
    assert evidence_service.MAX_CLAIM_DOCUMENTS == 4
    assert len((await repository.get("run-a")).state.evidence.jobs) == len(admitted)
    return admitted


def corrections(document, state):
    claim = state.claims[document.claim_id]
    return [
        {"name": "claim_id", "decision": "correct", "value": claim.claim_id},
        {"name": "policy_id", "decision": "correct", "value": claim.policy_id},
        {"name": "policyholder_id", "decision": "correct", "value": claim.policyholder_id},
        {"name": "loss_date", "decision": "correct", "value": claim.loss_date.isoformat()},
        {"name": "estimate_total_usd", "decision": "correct", "value": 1234.5},
        {"name": "invoice_total_usd", "decision": "correct", "value": 1234.5},
    ]


async def saturate_repeated_reviews(evidence, document_id):
    service, repository, _ = evidence
    for index in range(evidence_capacity.MAX_EVIDENCE_AUDIT + 1):
        state = (await repository.get("run-a")).state
        document = state.evidence.documents[document_id]
        try:
            await service.review("run-a", "employee-a", document_id, review_command(document, [
                {"name": "invoice_total_usd", "decision": "correct", "value": index},
            ]))
        except BusinessError as exc:
            assert exc.status_code == 409 and exc.code in ("evidence_capacity", "run_capacity")
            assert (await repository.get("run-a")).state == state
            return exc.code
    pytest.fail("Repeated reviews must not consume reserved worker capacity")


async def drain(instance, evidence):
    for _ in range(8):
        jobs = (await evidence[1].get("run-a")).state.evidence.jobs.values()
        if not any(job.status in ("queued", "running") for job in jobs):
            return
        await instance.run_once("run-a")
    pytest.fail("Bounded jobs did not reach durable terminal states")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "cancel", "uncertain"])
async def test_max_admission_with_six_corrections_saturated_reviews_and_terminal_job_safety(evidence, ending):
    admitted = await admit_forty_attempts(evidence)
    service, repository, _ = evidence
    state = (await repository.get("run-a")).state
    first = state.evidence.documents[admitted[0].document.document_id]
    await service.review("run-a", "employee-a", first.document_id,
                         review_command(first, corrections(first, state)))
    assert await saturate_repeated_reviews(evidence, first.document_id) == "evidence_capacity"
    state = (await repository.get("run-a")).state
    slots, _ = reservations(state)
    assert len(state.evidence.audit) + slots == evidence_capacity.MAX_EVIDENCE_AUDIT
    assert worker_audit_slots(state.evidence.jobs[first.job_id]) > 6
    original_events = [event.event_id for event in state.evidence.audit]
    instance, provider = worker(evidence)
    if ending == "cancel":
        job = state.evidence.jobs[first.job_id]
        await service.job_command("run-a", "employee-a", job.job_id,
                                  JobCommand(expected_version=job.version), cancel=True)
    elif ending == "uncertain":
        provider.submit_failure = "cu_submission_uncertain"
    await drain(instance, evidence)
    state = (await repository.get("run-a")).state
    if ending == "uncertain":
        assert await saturate_repeated_reviews(evidence, first.document_id) == "evidence_capacity"
        state = (await repository.get("run-a")).state
        for job in list(state.evidence.jobs.values()):
            assert job.status == "failed" and job.submission_uncertain
            assert job.error_code == "cu_submission_uncertain" and job.operation_location is None
            await service.job_command("run-a", "employee-a", job.job_id,
                                      JobCommand(expected_version=job.version), cancel=True)
    state = (await repository.get("run-a")).state
    assert [event.event_id for event in state.evidence.audit[:len(original_events)]] == original_events
    assert len(state.evidence.audit) <= evidence_capacity.MAX_EVIDENCE_AUDIT
    for job in state.evidence.jobs.values():
        assert job.status in ("completed", "cancelled")
        if job.status == "completed":
            assert job.operation_location and state.evidence.documents[job.document_id].raw_result
    # The first review of every remaining field still fits after all worker outcomes.
    for document in list(state.evidence.documents.values()):
        fresh = (await repository.get("run-a")).state
        current = fresh.evidence.documents[document.document_id]
        fields = [field for field in corrections(current, fresh) if field["name"] not in current.confirmed_fields]
        if fields:
            await service.review("run-a", "employee-a", current.document_id, review_command(current, fields))
    final = (await repository.get("run-a")).state
    assert all(len(document.confirmed_fields) == 6 for document in final.evidence.documents.values())
    assert len(final.evidence.audit) <= evidence_capacity.MAX_EVIDENCE_AUDIT


@pytest.mark.asyncio
async def test_run_bytes_reserved_before_source_admission(evidence):
    _, repository, blobs = evidence
    stored = await repository.get("run-a")
    stored.state.claims["CLM-2001"].description = "x" * 1_400_000
    await repository.replace(stored.state, stored.etag)
    with pytest.raises(BusinessError) as error:
        await register(evidence)
    assert error.value.code == "run_capacity" and blobs.put_calls == 0
    assert not (await repository.get("run-a")).state.evidence.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_nearly_full_run_keeps_bytes_for_projection_operation_and_cancellation(evidence, cancel):
    admitted = await admit_forty_attempts(evidence)
    service, repository, _ = evidence
    stored = await repository.get("run-a")
    _, future_bytes = reservations(stored.state)
    current_bytes = encoded_size(store._document(stored.state))
    padding = store.MAX_RUN_BYTES - current_bytes - future_bytes - 64
    assert padding > 0
    stored.state.claims["CLM-2001"].description += "x" * padding
    await repository.replace(stored.state, stored.etag)
    assert await saturate_repeated_reviews(evidence, admitted[0].document.document_id) == "run_capacity"
    provider = Provider()
    large = copy.deepcopy(RESULT)
    for name in ("claim_id", "policy_id", "policyholder_id", "loss_date",
                 "estimate_total_usd", "invoice_total_usd"):
        field = {"type": "number" if name.endswith("_usd") else "string"}
        field["valueNumber" if name.endswith("_usd") else "valueString"] = 1200 if name.endswith("_usd") else "é" * 200
        for key in ("source", "spans", "sourceSpans", "boundingRegions"):
            field[key] = ["x" * 1900]
        large["contents"][0]["fields"][name] = field
    from app.business.evidence_provider import project_fields
    projected_bytes = encoded_size({name: field.model_dump(mode="json") for name, field in project_fields(large).items()})
    assert 48_000 < projected_bytes <= MAX_PROJECTED_FIELDS_BYTES
    provider.result = large
    instance, _ = worker(evidence, provider)
    # Cancellation also fits when no general-purpose byte capacity remains.
    if cancel:
        state = (await repository.get("run-a")).state
        last = state.evidence.jobs[admitted[-1].job.job_id]
        await service.job_command("run-a", "employee-a", last.job_id,
                                  JobCommand(expected_version=last.version), cancel=True)
    await drain(instance, evidence)
    state = (await repository.get("run-a")).state
    assert sum(job.status == "completed" for job in state.evidence.jobs.values()) == len(admitted) - int(cancel)
    for document_id in state.evidence.documents:
        fresh = (await repository.get("run-a")).state
        current = fresh.evidence.documents[document_id]
        fields = [field for field in corrections(current, fresh) if field["name"] not in current.confirmed_fields]
        if fields:
            await service.review("run-a", "employee-a", document_id, review_command(current, fields))
    state = (await repository.get("run-a")).state
    assert all(len(document.confirmed_fields) == 6 for document in state.evidence.documents.values())
    assert encoded_size(store._document(state)) <= store.MAX_RUN_BYTES
    assert all(job.status != "running" for job in state.evidence.jobs.values())


@pytest.mark.asyncio
async def test_remaining_retry_attempts_and_failure_cancellation_are_reserved(evidence):
    result = await register(evidence)
    await saturate_repeated_reviews(evidence, result.document.document_id)
    instance, provider = worker(evidence)
    provider.poll_failure = "cu_poll_unavailable"
    for attempt in range(1, 4):
        await instance.run_once("run-a")
        _, job, _ = await state_job(evidence, result)
        assert job.status == "failed" and job.attempts == attempt and job.operation_location
        if attempt < job.max_attempts:
            await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                          JobCommand(expected_version=job.version), cancel=False)
    await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                  JobCommand(expected_version=job.version), cancel=True)
    assert (await state_job(evidence, result))[1].status == "cancelled"
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_max_attempt_configuration_admits_only_jobs_with_full_run_byte_reserve(evidence):
    service, repository, blobs = evidence
    service.settings.evidence_worker_max_attempts = 5
    admitted = []
    for claim_id in ("CLM-2001", "CLM-2002"):
        for index in range(evidence_service.MAX_CLAIM_DOCUMENTS):
            state = (await repository.get("run-a")).state
            calls = blobs.put_calls
            try:
                admitted.append(await service.register(
                    "run-a", "employee-a", claim_id, PDF, "source.pdf", "application/pdf",
                    state.claims[claim_id].version, f"attempt-five-{claim_id}-{index}", True,
                ))
            except BusinessError as exc:
                assert exc.code in ("run_capacity", "evidence_capacity") and blobs.put_calls == calls
    assert 1 <= len(admitted) < evidence_service.MAX_DOCUMENTS
    instance, provider = worker(evidence)
    await saturate_repeated_reviews(evidence, admitted[0].document.document_id)
    await drain(instance, evidence)
    jobs = (await repository.get("run-a")).state.evidence.jobs.values()
    assert all(job.status == "completed" and job.max_attempts == 5 for job in jobs)
    assert provider.submissions == len(admitted)


def test_all_worker_audit_transitions_consume_their_reserved_slot():
    def job(status, phase, attempts, maximum):
        return EvidenceJob(job_id="job-test", document_id="doc-test", claim_id="CLM-2001",
                           created_at="now", updated_at="now", status=status, phase=phase,
                           attempts=attempts, max_attempts=maximum)
    for maximum in range(1, 6):
        for attempts in range(maximum + 1):
            for phase in ("ready", "submitting", "polling"):
                current = job("running", phase, attempts, maximum)
                failed = current.model_copy(update={"status": "failed",
                    "submission_uncertain": phase == "submitting"})
                cancelled = current.model_copy(update={"status": "cancelled"})
                assert worker_audit_slots(failed) + 1 <= worker_audit_slots(current)
                assert worker_audit_slots(cancelled) + 1 <= worker_audit_slots(current)
                if phase == "ready":
                    submitted = current.model_copy(update={"phase": "submitting"})
                    assert worker_audit_slots(submitted) + 1 <= worker_audit_slots(current)
                if phase == "submitting":
                    polling = current.model_copy(update={"phase": "polling"})
                    assert worker_audit_slots(polling) + 1 <= worker_audit_slots(current)
                if attempts < maximum and phase in ("ready", "polling"):
                    queued = current.model_copy(update={"status": "queued"})
                    acquired = current.model_copy(update={"attempts": attempts + 1})
                    assert worker_audit_slots(acquired) + 1 <= worker_audit_slots(queued)
                    retry = failed.model_copy(update={"status": "queued"})
                    assert worker_audit_slots(retry) + 1 <= worker_audit_slots(failed)


def test_confirmation_scalar_byte_bound_covers_unicode_and_rejects_oversized_integers():
    from pydantic import ValidationError
    assert FieldReview(name="claim_id", decision="correct", value="é" * 200)
    assert FieldReview(name="invoice_total_usd", decision="correct", value=int("9" * 300))
    with pytest.raises(ValidationError):
        FieldReview(name="invoice_total_usd", decision="correct", value=int("9" * 1203))


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["provenance", "location"])
async def test_provider_metadata_bounds_keep_failure_and_cancellation_persistable(evidence, oversized):
    result = await register(evidence)
    await saturate_repeated_reviews(evidence, result.document.document_id)
    instance, provider = worker(evidence)
    if oversized == "provenance":
        async def preflight():
            return {"endpoint": "https://unit.cognitiveservices.azure.com",
                    "analyzer_id": "owned", "model": "gpt-4.1", "unexpected": "x" * 4096}
        provider.preflight = preflight
    else:
        submit = provider.submit
        async def overlong_location(*args):
            return (await submit(*args)) + "x" * 1024
        provider.submit = overlong_location
    await instance.run_once("run-a")
    _, job, _ = await state_job(evidence, result)
    assert job.status == "failed"
    assert provider.submissions == (0 if oversized == "provenance" else 1)
    assert job.submission_uncertain == (oversized == "location")
    await evidence[0].job_command("run-a", "employee-a", job.job_id,
                                  JobCommand(expected_version=job.version), cancel=True)
    assert (await state_job(evidence, result))[1].status == "cancelled"


@pytest.mark.asyncio
async def test_shared_repository_protects_submitting_job_from_fnol_notes_and_tasks(evidence):
    result = await register(evidence)
    service, repository, _ = evidence
    instance, provider = worker(evidence)
    provider.block_submit = asyncio.Event()
    running = asyncio.create_task(instance.run_once("run-a"))
    await provider.submitting.wait()
    try:
        stored = await repository.get("run-a")
        assert stored.state.evidence.jobs[result.job.job_id].phase == "submitting"
        assert not stored.state.evidence.jobs[result.job.job_id].operation_location
        _, future_bytes = reservations(stored.state)
        size_bytes = encoded_size(store._document(stored.state))
        stored.state.claims["CLM-2001"].description += "x" * (
            store.MAX_RUN_BYTES - size_bytes - future_bytes - 64
        )
        await repository.replace(stored.state, stored.etag)
        before = (await repository.get("run-a")).state
        version = before.claims["CLM-2001"].version
        commands = [
            lambda: service.business.report_loss("run-a", "customer-a", submission("competing-fnol")),
            lambda: service.business.create_note("run-a", "employee-a", "CLM-2001",
                CreateNote(expected_version=version, body="Synthetic unrelated note. " * 60)),
            lambda: service.business.create_task("run-a", "employee-a", "CLM-2001",
                CreateTask(expected_version=version, title="Synthetic unrelated task", requested_evidence="invoice")),
        ]
        for command in commands:
            with pytest.raises(BusinessError) as error:
                await command()
            assert error.value.code == "run_capacity"
            assert (await repository.get("run-a")).state == before
    finally:
        provider.block_submit.set()
        await running
    state, job, document = await state_job(evidence, result)
    assert job.status == "completed" and job.operation_location and not job.submission_uncertain
    assert document.raw_result and provider.submissions == 1
    assert encoded_size(store._document(state)) <= store.MAX_RUN_BYTES
    assert any(event.action == "cu_operation_recorded" for event in state.evidence.audit)
    assert any(event.action == "extraction_completed" for event in state.evidence.audit)
