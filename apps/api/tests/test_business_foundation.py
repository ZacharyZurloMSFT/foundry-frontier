import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from azure.cosmos.exceptions import CosmosHttpResponseError
from pydantic import ValidationError

from app.business import service, store
from app.business.models import (
    ActorMapping, BusinessError, ClaimRecord, ConcurrentChange, LossSubmission,
    PageRequest, TimelineEvent, TransitionCommand,
)
from app.business.service import InsuranceBusinessService, seed_run
from app.business.store import CosmosBusinessRepository

ACTORS = {
    "customer-a": ActorMapping(role="policyholder", policyholder_id="PH-1001"),
    "customer-b": ActorMapping(role="policyholder", policyholder_id="PH-1002"),
    "employee-a": ActorMapping(role="adjuster"),
    "presenter-a": ActorMapping(role="presenter"),
}
REPORT = {
    "policy_id": "POL-1001", "vehicle_id": "SYNTHETIC-AUTO-1001",
    "incident_type": "auto_collision", "loss_date": "2026-10-15", "state": "WA",
    "description": "Synthetic low-speed collision with bumper damage.",
    "synthetic_acknowledged": True,
}


def submission(key="first-loss", **changes):
    return LossSubmission(idempotency_key=key, report={**REPORT, **changes})


@pytest_asyncio.fixture
async def business(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(store, "_container", container)
    repository = CosmosBusinessRepository()
    await repository.create(seed_run("run-a", ACTORS))
    return InsuranceBusinessService(repository), repository, database["workflow_state"]


@pytest.mark.asyncio
async def test_seed_is_persistent_versioned_and_never_overwrites_a_run(business, monkeypatch):
    api, repository, container = business
    first = await api.get_policy("run-a", "customer-a", "POL-1001")
    assert first.source_version == "insurance-synthetic-v2"
    assert first.policy_wording["version"] == "1"
    assert first.policy["coverages"][0]["deductible_usd"] == 500
    original = (await repository.get("run-a")).state
    with pytest.raises(BusinessError, match="not reseeded") as error:
        await repository.create(seed_run("run-a", ACTORS))
    assert error.value.status_code == 409
    assert len(container.items) == 1
    assert (await repository.get("run-a")).state == original

    monkeypatch.setattr(service, "fixtures", lambda: pytest.fail("Fixtures are not live record storage"))
    monkeypatch.setattr(service, "lookup_policy_record", lambda *_: pytest.fail("Read the pinned snapshot"))
    replica = InsuranceBusinessService(CosmosBusinessRepository())
    assert await replica.get_policy("run-a", "customer-a", "POL-1001") == first
    result = await replica.report_loss("run-a", "customer-a", submission())
    assert result.claim.policy_snapshot == first
    first.policy["coverages"][0]["deductible_usd"] = 1
    assert (await replica.get_claim("run-a", "customer-a", result.claim.claim_id)).policy_snapshot.policy["coverages"][0]["deductible_usd"] == 500


@pytest.mark.asyncio
async def test_fnol_receipt_claim_and_history_persist_together_without_ai(business):
    api, repository, container = business
    result = await api.report_loss("run-a", "customer-a", submission())
    claim = result.claim
    assert claim.claim_id.startswith("CLM-") and not result.replayed
    assert claim.status == "reported" and claim.version == 1
    assert claim.policyholder_id == "PH-1001" and claim.review_flags == []
    assert claim.synthetic and claim.timeline[0].before_version == 0
    assert claim.timeline[0].after_version == 1 and claim.timeline[0].actor_id is None
    persisted = (await repository.get("run-a")).state
    assert len(persisted.claims) == 3 and len(persisted.loss_receipts) == 1
    assert persisted.claims[claim.claim_id].timeline[0].actor_id == "customer-a"
    replica = InsuranceBusinessService(CosmosBusinessRepository())
    assert await replica.get_claim("run-a", "customer-a", claim.claim_id) == claim
    assert len(container.items) == 1
    document = next(iter(container.items.values()))
    assert document["workflowId"] == document["id"] == "insurance-run-run-a"
    assert document["record_kind"] == store.RECORD_KIND
    assert "first-loss" not in json.dumps(document)


@pytest.mark.asyncio
async def test_retry_returns_original_result_even_after_the_case_changes(business):
    api, repository, _ = business
    original = await api.report_loss("run-a", "customer-a", submission())
    await api.transition_claim("run-a", "employee-a", original.claim.claim_id, TransitionCommand(
        target="evidence_requested", expected_version=1, reason="Internal review note.",
    ))
    replay = await api.report_loss("run-a", "customer-a", submission())
    assert replay.replayed and replay.claim == original.claim
    current = await api.get_claim("run-a", "customer-a", original.claim.claim_id)
    assert current.status == "evidence_requested" and current.version == 2
    assert len(current.timeline) == 2 and all(e.internal_note is None for e in current.timeline)
    employee_view = await api.get_claim("run-a", "employee-a", original.claim.claim_id)
    assert employee_view.timeline[-1].internal_note == "Internal review note."
    assert employee_view.timeline[-1].actor_id == "employee-a"
    before = (await repository.get("run-a")).state
    with pytest.raises(BusinessError, match="different loss details") as error:
        await api.report_loss("run-a", "customer-a", submission(description="A different synthetic incident."))
    assert error.value.code == "idempotency_conflict" and error.value.status_code == 409
    assert (await repository.get("run-a")).state == before


@pytest.mark.asyncio
async def test_policy_changes_do_not_rewrite_the_recorded_loss_snapshot(business):
    api, repository, _ = business
    original = await api.report_loss("run-a", "customer-a", submission())
    stored = await repository.get("run-a")
    current_policy = stored.state.policies["POL-1001"]
    current_policy.source_version = "insurance-synthetic-next"
    current_policy.policy["coverages"][0]["deductible_usd"] = 750
    current_policy.policy_wording["version"] = "2"
    await repository.replace(stored.state, stored.etag)
    assert (await api.get_policy("run-a", "customer-a", "POL-1001")).policy_wording["version"] == "2"
    recorded = await api.get_claim("run-a", "customer-a", original.claim.claim_id)
    assert recorded.policy_snapshot.policy_wording["version"] == "1"
    assert recorded.policy_snapshot.policy["coverages"][0]["deductible_usd"] == 500
    assert (await api.report_loss("run-a", "customer-a", submission())).claim == original.claim


@pytest.mark.asyncio
async def test_customer_history_excludes_internal_events_and_employee_identity(business):
    api, repository, _ = business
    stored = await repository.get("run-a")
    claim = stored.state.claims["CLM-2001"]
    claim.timeline.append(TimelineEvent(
        event_id="internal-event", command_id="internal-command",
        actor_id="employee-a", actor_role="adjuster", occurred_at=claim.updated_at,
        summary="Internal-only synthetic review details.",
        internal_note="Internal-only note.", visibility="internal",
        before_version=1, after_version=2,
    ))
    claim.version = 2
    await repository.replace(stored.state, stored.etag)
    customer = await api.get_claim("run-a", "customer-a", "CLM-2001")
    employee = await api.get_claim("run-a", "employee-a", "CLM-2001")
    assert len(customer.timeline) == 1 and len(employee.timeline) == 2
    assert all(e.actor_id is None for e in customer.timeline)
    assert "Internal-only" not in customer.model_dump_json()
    assert employee.timeline[-1].actor_id == "employee-a"


@pytest.mark.asyncio
async def test_role_customer_run_scope_and_visibility_safe_reads(business):
    api, repository, _ = business
    for subject, claim_id, policy_id in (
        ("customer-a", "CLM-2002", "POL-1002"),
        ("customer-b", "CLM-2001", "POL-1001"),
    ):
        for read, identifier in ((api.get_claim, claim_id), (api.get_policy, policy_id)):
            with pytest.raises(BusinessError) as error:
                await read("run-a", subject, identifier)
            assert error.value.status_code == 404
    with pytest.raises(BusinessError) as error:
        await api.report_loss("run-a", "customer-b", submission())
    assert error.value.status_code == 404
    for subject in ("employee-a", "presenter-a", "not-mapped"):
        with pytest.raises(BusinessError) as error:
            await api.report_loss("run-a", subject, submission())
        assert error.value.status_code == 403
    await repository.create(seed_run("run-b", {
        "other-customer": ActorMapping(role="policyholder", policyholder_id="PH-1001"),
    }))
    with pytest.raises(BusinessError) as error:
        await api.get_claim("run-b", "customer-a", "CLM-2001")
    assert error.value.status_code == 403
    created = await api.report_loss("run-a", "customer-a", submission())
    with pytest.raises(BusinessError) as error:
        await api.get_claim("run-b", "other-customer", created.claim.claim_id)
    assert error.value.status_code == 404
    assert len((await api.list_claims("run-a", "customer-a")).items) == 2
    assert len((await api.list_claims("run-a", "customer-b")).items) == 1
    policies = await api.list_policies("run-a", "customer-a")
    assert [p.policy["policy_id"] for p in policies.items] == ["POL-1001"]
    first = await api.list_claims("run-a", "employee-a", PageRequest(limit=1))
    assert len(first.items) == 1 and first.next_offset == 1
    second = await api.list_claims("run-a", "employee-a", PageRequest(offset=first.next_offset, limit=1))
    assert first.items[0].claim_id != second.items[0].claim_id


@pytest.mark.asyncio
async def test_idempotency_keys_are_bound_to_the_actor_and_run(business):
    api, repository, _ = business
    first = await api.report_loss("run-a", "customer-a", submission())
    second = await api.report_loss("run-a", "customer-b", submission(
        policy_id="POL-1002", vehicle_id="SYNTHETIC-AUTO-1002", state="CO",
    ))
    await repository.create(seed_run("run-b", ACTORS))
    third = await api.report_loss("run-b", "customer-a", submission())
    assert len({first.claim.claim_id, second.claim.claim_id, third.claim.claim_id}) == 3
    assert all(not r.replayed for r in (first, second, third))


@pytest.mark.parametrize("changes", [
    {"description": " "}, {"loss_date": "not-a-date"}, {"state": "Washington"},
    {"synthetic_acknowledged": False}, {"synthetic_acknowledged": 1},
    {"policyholder_id": "PH-1002"}, {"actor_role": "adjuster"},
    {"run_id": "another-run"}, {"status": "approved"},
])
def test_form_contract_rejects_invalid_or_client_owned_authority(changes):
    with pytest.raises(ValidationError):
        submission(**changes)


@pytest.mark.asyncio
async def test_vehicle_relationship_is_validated_without_a_partial_claim(business):
    api, repository, _ = business
    before = (await repository.get("run-a")).state
    with pytest.raises(BusinessError) as error:
        await api.report_loss("run-a", "customer-a", submission(vehicle_id="SYNTHETIC-AUTO-1002"))
    assert error.value.code == "vehicle_mismatch"
    assert (await repository.get("run-a")).state == before


@pytest.mark.asyncio
async def test_date_and_jurisdiction_mismatches_are_preserved_for_human_review(business):
    api, _, _ = business
    result = await api.report_loss("run-a", "customer-a", submission(loss_date="2025-12-31", state="CO"))
    claim = result.claim
    assert claim.loss_date.isoformat() == "2025-12-31" and claim.state == "CO"
    assert claim.status == "reported" and claim.policy_snapshot.policy["state"] == "WA"
    assert set(claim.review_flags) == {
        "loss_outside_policy_period", "loss_jurisdiction_mismatch",
        "loss_outside_wording_period", "loss_outside_endorsement_period",
    }


class RacingRepository(CosmosBusinessRepository):
    def __init__(self):
        self.reads = 0
        self.ready = asyncio.Event()

    async def get(self, run_id):
        snapshot = await super().get(run_id)
        if self.reads < 2:
            self.reads += 1
            if self.reads == 2:
                self.ready.set()
            await self.ready.wait()
        return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_form_submissions_reconcile_without_lost_or_duplicate_claims(business, same_key):
    _, repository, _ = business
    api = InsuranceBusinessService(RacingRepository())
    first, second = await asyncio.gather(
        api.report_loss("run-a", "customer-a", submission()),
        api.report_loss("run-a", "customer-a", submission("first-loss" if same_key else "second-loss")),
    )
    state = (await repository.get("run-a")).state
    assert len(state.claims) == (3 if same_key else 4)
    assert len(state.loss_receipts) == (1 if same_key else 2)
    if same_key:
        assert first.claim == second.claim and first.replayed != second.replayed
    else:
        assert first.claim.claim_id != second.claim.claim_id


@pytest.mark.asyncio
async def test_explicit_workflow_rejects_invalid_stale_and_unprivileged_changes(business):
    api, repository, _ = business
    original = (await repository.get("run-a")).state
    for subject in ("customer-a", "presenter-a"):
        with pytest.raises(BusinessError) as error:
            await api.transition_claim("run-a", subject, "CLM-2001", TransitionCommand(
                target="evidence_received", expected_version=1, reason="An attempted update.",
            ))
        assert error.value.status_code == 403
    with pytest.raises(BusinessError) as error:
        await api.transition_claim("run-a", "employee-a", "CLM-2001", TransitionCommand(
            target="customer_update_prepared", expected_version=1, reason="Skip review.",
        ))
    assert error.value.code == "invalid_transition"
    assert (await repository.get("run-a")).state == original
    changed = await api.transition_claim("run-a", "employee-a", "CLM-2001", TransitionCommand(
        target="evidence_received", expected_version=1, reason="Manual evidence intake.",
    ))
    assert changed.version == 2 and changed.timeline[-1].before_version == 1
    with pytest.raises(ConcurrentChange):
        await api.transition_claim("run-a", "employee-a", "CLM-2001", TransitionCommand(
            target="adjuster_review", expected_version=1, reason="Stale browser.",
        ))
    assert (await api.get_claim("run-a", "employee-a", "CLM-2001")).version == 2


@pytest.mark.parametrize("target", ["approved", "denied", "settled", "payment_sent"])
def test_no_adjudication_or_financial_workflow_commands(target):
    with pytest.raises(ValidationError):
        TransitionCommand(target=target, expected_version=1, reason="Not permitted.")


@pytest.mark.asyncio
async def test_concurrent_workflow_updates_surface_a_conflict_and_keep_one_audit_event(business):
    _, repository, _ = business
    api = InsuranceBusinessService(RacingRepository())
    command = TransitionCommand(target="evidence_received", expected_version=1, reason="Manual intake.")
    results = await asyncio.gather(
        api.transition_claim("run-a", "employee-a", "CLM-2001", command),
        api.transition_claim("run-a", "employee-a", "CLM-2001", command),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ClaimRecord) for r in results) == 1
    assert sum(isinstance(r, ConcurrentChange) for r in results) == 1
    claim = (await repository.get("run-a")).state.claims["CLM-2001"]
    assert claim.version == 2 and len(claim.timeline) == 2


@pytest.mark.asyncio
async def test_failed_writes_never_return_success_and_uncertain_outcomes_can_be_retried(business, monkeypatch):
    api, repository, container = business
    actual = container.replace_item
    monkeypatch.setattr(container, "replace_item", AsyncMock(side_effect=CosmosHttpResponseError(status_code=503)))
    with pytest.raises(CosmosHttpResponseError):
        await api.report_loss("run-a", "customer-a", submission())
    assert len((await repository.get("run-a")).state.claims) == 2

    async def saved_but_response_lost(*args, **kwargs):
        await actual(*args, **kwargs)
        raise CosmosHttpResponseError(status_code=503)
    monkeypatch.setattr(container, "replace_item", saved_but_response_lost)
    with pytest.raises(CosmosHttpResponseError):
        await api.report_loss("run-a", "customer-a", submission())
    monkeypatch.setattr(container, "replace_item", actual)
    replay = await api.report_loss("run-a", "customer-a", submission())
    assert replay.replayed and len((await repository.get("run-a")).state.claims) == 3


@pytest.mark.asyncio
async def test_bounded_retries_and_run_limits_leave_persisted_state_unchanged(business, monkeypatch):
    api, repository, _ = business
    before = (await repository.get("run-a")).state
    with monkeypatch.context() as patch:
        replacement = AsyncMock(side_effect=ConcurrentChange())
        patch.setattr(repository, "replace", replacement)
        with pytest.raises(ConcurrentChange):
            await api.report_loss("run-a", "customer-a", submission())
        assert replacement.await_count == service.MAX_CREATE_ATTEMPTS
    for module, setting, value in (
        (service, "MAX_CLAIMS", 2), (store, "MAX_RUN_BYTES", 1000),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(module, setting, value)
            with pytest.raises(BusinessError) as error:
                await api.report_loss("run-a", "customer-a", submission())
            assert error.value.code == "run_capacity"
    monkeypatch.setattr(service, "MAX_TIMELINE_EVENTS", 1)
    with pytest.raises(BusinessError) as error:
        await api.transition_claim("run-a", "employee-a", "CLM-2001", TransitionCommand(
            target="evidence_received", expected_version=1, reason="Bounded history.",
        ))
    assert error.value.code == "run_capacity"
    assert (await repository.get("run-a")).state == before


@pytest.mark.asyncio
async def test_missing_runs_and_provider_read_failures_are_not_seed_fallbacks(business, monkeypatch):
    api, _, container = business
    with pytest.raises(BusinessError) as error:
        await api.get_claim("missing-run", "customer-a", "CLM-2001")
    assert error.value.status_code == 404
    monkeypatch.setattr(container, "read_item", AsyncMock(side_effect=CosmosHttpResponseError(status_code=403)))
    with pytest.raises(CosmosHttpResponseError) as error:
        await api.get_claim("run-a", "customer-a", "CLM-2001")
    assert error.value.status_code == 403


def test_seed_mapping_and_pagination_validation():
    for mappings in (
        {}, {" ": ActorMapping(role="presenter")},
        {"customer": ActorMapping(role="policyholder", policyholder_id="PH-9999")},
    ):
        with pytest.raises(BusinessError):
            seed_run("run-a", mappings)
    with pytest.raises(ValidationError):
        seed_run("invalid/run", ACTORS)
    with pytest.raises(ValidationError):
        ActorMapping(role="policyholder")
    with pytest.raises(ValidationError):
        ActorMapping(role="adjuster", policyholder_id="PH-1001")
    for changes in ({"offset": -1}, {"limit": 101}, {"limit": True}):
        with pytest.raises(ValidationError):
            PageRequest(**changes)
