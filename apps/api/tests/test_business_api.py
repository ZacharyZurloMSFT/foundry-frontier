import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
import pytest_asyncio
from azure.cosmos.exceptions import CosmosHttpResponseError
from cryptography.hazmat.primitives.asymmetric import rsa

from app import main
from app.business import auth, routes, service, store
from app.business.models import ActorMapping, BusinessError
from app.business.service import InsuranceBusinessService, seed_run
from app.settings import Settings

TENANT = "11111111-1111-4111-8111-111111111111"
AUDIENCE = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
CUSTOMER_A = "44444444-4444-4444-8444-444444444444"
CUSTOMER_B = "55555555-5555-4555-8555-555555555555"
EMPLOYEE = "66666666-6666-4666-8666-666666666666"
PRESENTER = "77777777-7777-4777-8777-777777777777"
ROOT = Path(__file__).resolve().parents[3]
REPORT = {
    "policy_id": "POL-1001", "vehicle_id": "SYNTHETIC-AUTO-1001",
    "incident_type": "auto_collision", "loss_date": "2026-10-15", "state": "WA",
    "description": "Synthetic low-speed collision with bumper damage.",
    "synthetic_acknowledged": True,
}


@pytest.fixture
def identity(monkeypatch):
    settings = Settings(
        _env_file=None, NORTHWIND_BUSINESS_DOMAIN="insurance", NORTHWIND_BUSINESS_API_ENABLED=True,
        INSURANCE_AUTH_TENANT_ID=TENANT, INSURANCE_AUTH_AUDIENCE=AUDIENCE,
        INSURANCE_AUTH_CLIENT_IDS=CLIENT, COSMOS_ENDPOINT="https://storage.invalid",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))
    monkeypatch.setattr(auth, "signing_client", lambda _: signing)

    def token(oid=CUSTOMER_A, *, missing=None, header=None, **changes):
        now = int(time.time())
        claims = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE,
            "exp": now + 600, "iat": now, "nbf": now - 1,
            "tid": TENANT, "oid": oid, "azp": CLIENT, "scp": "access_as_user", "ver": "2.0",
            **changes,
        }
        if missing:
            del claims[missing]
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key", **(header or {})})
    return settings, token, signing


@pytest_asyncio.fixture
async def api(identity, database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(store, "_container", container)
    mappings = {
        auth.subject_id(TENANT, CUSTOMER_A): ActorMapping(role="policyholder", policyholder_id="PH-1001"),
        auth.subject_id(TENANT, CUSTOMER_B): ActorMapping(role="policyholder", policyholder_id="PH-1002"),
        auth.subject_id(TENANT, EMPLOYEE): ActorMapping(role="adjuster"),
        auth.subject_id(TENANT, PRESENTER): ActorMapping(role="presenter"),
    }
    repository = store.CosmosBusinessRepository()
    await repository.create(seed_run("run-a", mappings))
    await repository.create(seed_run("run-b", {
        auth.subject_id(TENANT, CUSTOMER_B): ActorMapping(role="policyholder", policyholder_id="PH-1001"),
    }))
    instance = InsuranceBusinessService(repository)
    monkeypatch.setitem(main.app.dependency_overrides, routes.business_service, lambda: instance)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://unit.test",
        headers={"Authorization": f"Bearer {identity[1]()}", "X-Demo-Run": "run-a"},
    ) as client:
        yield client, repository, identity


@pytest.mark.asyncio
async def test_verified_token_resolves_server_owned_account_context(api):
    client, _, identity = api
    response = await client.get("/api/v1/me")
    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-a", "role": "policyholder", "policyholder_id": "PH-1001",
        "source_version": "insurance-synthetic-v2", "synthetic": True, "adjusters": [],
    }
    employee = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {identity[1](EMPLOYEE)}"})
    assert employee.json()["role"] == "adjuster"
    assert employee.json()["adjusters"] == [auth.subject_id(TENANT, EMPLOYEE)]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Correlation-ID"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"aud": CLIENT}, {"iss": "https://other.invalid/v2.0"}, {"tid": CLIENT},
    {"azp": AUDIENCE}, {"scp": "something_else"}, {"scp": ["access_as_user"]},
    {"ver": "1.0"}, {"idtyp": "app"}, {"oid": "not-an-object-id"},
    {"exp": 1}, {"nbf": 9999999999}, {"iat": 9999999999},
])
async def test_invalid_token_claims_fail_closed(identity, change):
    with pytest.raises(BusinessError) as error:
        await auth.verify_access_token(identity[1](**change), auth.configuration())
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "scp", "azp", "ver"])
async def test_required_token_claims_cannot_be_omitted(identity, claim):
    with pytest.raises(BusinessError) as error:
        await auth.verify_access_token(identity[1](missing=claim), auth.configuration())
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_bad_signature_algorithm_header_and_identity_outage(identity, monkeypatch):
    configuration = auth.configuration()
    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(identity[1](), options={"verify_signature": False})
    bad_signature = jwt.encode(claims, second_key, algorithm="RS256", headers={"kid": "test-key"})
    for token in (
        bad_signature, "not-a-token", identity[1](header={"kid": ""}),
        jwt.encode(claims, "x" * 64, algorithm="HS256", headers={"kid": "test-key"}),
        "x" * 16385,
    ):
        with pytest.raises(BusinessError) as error:
            await auth.verify_access_token(token, configuration)
        assert error.value.status_code == 401
    def unavailable(_):
        raise jwt.PyJWKClientConnectionError("Private provider details must not be exposed")
    monkeypatch.setattr(identity[2], "get_signing_key_from_jwt", unavailable)
    with pytest.raises(BusinessError) as error:
        await auth.verify_access_token(identity[1](), configuration)
    assert error.value.status_code == 503 and "Private" not in str(error.value)


@pytest.mark.asyncio
async def test_disabled_missing_configuration_and_auth_never_touch_business_storage(api, monkeypatch):
    client, repository, identity = api
    storage_read = AsyncMock(side_effect=AssertionError("Do not read protected storage"))
    monkeypatch.setattr(repository, "get", storage_read)
    identity[0].business_api_enabled = False
    response = await client.get("/api/v1/me")
    assert response.status_code == 503 and response.json()["error"]["code"] == "business_api_disabled"
    identity[0].business_api_enabled = True
    identity[0].business_auth_audience = ""
    assert (await client.get("/api/v1/me")).status_code == 503
    identity[0].business_auth_audience = AUDIENCE
    response = await client.get("/api/v1/me", headers={"Authorization": "Basic ignored"})
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["correlation_id"] == response.headers["X-Correlation-ID"]
    storage_read.assert_not_called()


@pytest.mark.asyncio
async def test_scope_cannot_be_changed_by_urls_headers_body_or_token_role(api):
    client, _, identity = api
    assert (await client.get("/api/v1/claims/CLM-2002")).status_code == 404
    assert (await client.get("/api/v1/policies/POL-1002")).status_code == 404
    assert (await client.get("/api/v1/me", headers={"X-Demo-Run": "run-b"})).status_code == 403
    response = await client.post("/api/v1/claims/CLM-2001/tasks", json={
        "expected_version": 1, "title": "Request photos", "requested_evidence": "damage_photos",
    }, headers={"X-Persona": "adjuster", "X-MS-CLIENT-PRINCIPAL-ID": EMPLOYEE,
                "Authorization": f"Bearer {identity[1](roles=['adjuster'])}"})
    assert response.status_code == 403
    assert (await client.get("/api/v1/me/claims", params={"assigned_to": auth.subject_id(TENANT, EMPLOYEE)})).status_code == 403
    response = await client.post("/api/v1/claims", json={**REPORT, "policyholder_id": "PH-1002"},
                                 headers={"Idempotency-Key": "spoof-attempt"})
    assert response.status_code == 422
    response = await client.get("/api/v1/me/claims")
    assert [c["claim_id"] for c in response.json()["items"]] == ["CLM-2001"]


@pytest.mark.asyncio
async def test_fnol_api_returns_location_etag_and_original_retry_outcome(api):
    client, _, identity = api
    response = await client.post("/api/v1/claims", json=REPORT, headers={"Idempotency-Key": "form-one"})
    assert response.status_code == 201
    result = response.json()
    claim_id = result["claim"]["claim_id"]
    assert response.headers["Location"] == f"/api/v1/claims/{claim_id}"
    assert response.headers["ETag"] == '"1"' and not result["replayed"]
    employee = {"Authorization": f"Bearer {identity[1](EMPLOYEE)}"}
    changed = await client.post(f"/api/v1/claims/{claim_id}/transitions", headers=employee, json={
        "target": "evidence_requested", "expected_version": 1, "reason": "Manual photo request.",
    })
    assert changed.status_code == 200 and changed.headers["ETag"] == '"2"'
    retry = await client.post("/api/v1/claims", json=REPORT, headers={"Idempotency-Key": "form-one"})
    assert retry.status_code == 200 and retry.json()["replayed"]
    assert retry.json()["claim"] == result["claim"]
    current = await client.get(response.headers["Location"])
    assert current.json()["version"] == 2
    conflict = await client.post("/api/v1/claims", json={**REPORT, "description": "A different synthetic loss."},
                                 headers={"Idempotency-Key": "form-one"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    assert (await client.post("/api/v1/claims", json=REPORT)).status_code == 422
    contract = main.app.openapi()["paths"]["/api/v1/claims"]["post"]
    assert {"200", "201", "401", "409", "422", "503"} <= contract["responses"].keys()
    assert contract["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")


@pytest.mark.asyncio
async def test_manual_workbench_and_customer_share_persisted_case_with_filtered_details(api):
    client, _, identity = api
    employee = {"Authorization": f"Bearer {identity[1](EMPLOYEE)}"}
    task = await client.post("/api/v1/claims/CLM-2001/tasks", headers=employee, json={
        "expected_version": 1, "title": "Supply fictional photos", "requested_evidence": "damage_photos",
        "assigned_to": auth.subject_id(TENANT, EMPLOYEE),
    })
    assert task.status_code == 200 and task.json()["version"] == 2
    task_id = task.json()["tasks"][0]["task_id"]
    for expected_version, visibility in ((2, "internal"), (3, "customer")):
        note = await client.post("/api/v1/claims/CLM-2001/notes", headers=employee, json={
            "expected_version": expected_version, "body": f"Fictional {visibility} case note.",
            "visibility": visibility,
        })
        assert note.status_code == 200
    assigned = await client.post("/api/v1/claims/CLM-2001/assignment", headers=employee, json={
        "expected_version": 4, "adjuster_id": auth.subject_id(TENANT, EMPLOYEE),
    })
    assert assigned.status_code == 200 and assigned.json()["version"] == 5
    customer = (await client.get("/api/v1/claims/CLM-2001")).json()
    assert customer["assigned_adjuster_id"] is None
    assert len(customer["notes"]) == 1 and customer["notes"][0]["author_id"] is None
    assert customer["tasks"][0]["created_by"] is None and customer["tasks"][0]["assigned_to"] is None
    assert "Fictional internal" not in json.dumps(customer)
    assert all(event["actor_id"] is None for event in customer["timeline"])
    completed = await client.patch(f"/api/v1/claims/CLM-2001/tasks/{task_id}", headers=employee,
                                   json={"expected_version": 5, "status": "completed"})
    assert completed.status_code == 200 and completed.json()["version"] == 6
    assert completed.json()["tasks"][0]["completed_by"] == auth.subject_id(TENANT, EMPLOYEE)
    assert completed.json()["status"] == "evidence_requested"
    assert "damage_photos" not in completed.json()["outstanding_evidence"]
    stale = await client.patch(f"/api/v1/claims/CLM-2001/tasks/{task_id}", headers=employee,
                               json={"expected_version": 5, "status": "completed"})
    assert stale.status_code == 409
    history = await client.get("/api/v1/claims/CLM-2001/timeline", params={"limit": 1})
    assert len(history.json()["items"]) == 1 and history.json()["next_offset"] == 1
    queue = await client.get("/api/v1/claims", headers=employee,
                             params={"status": "evidence_requested", "assigned_to": auth.subject_id(TENANT, EMPLOYEE)})
    assert [c["claim_id"] for c in queue.json()["items"]] == ["CLM-2001"]


@pytest.mark.asyncio
async def test_task_completion_keeps_other_open_requests_and_requires_permitted_assignees(api):
    client, _, identity = api
    client.headers["Authorization"] = f"Bearer {identity[1](EMPLOYEE)}"
    bad = await client.post("/api/v1/claims/CLM-2001/tasks", json={
        "expected_version": 1, "title": "Photos", "requested_evidence": "damage_photos",
        "assigned_to": auth.subject_id(TENANT, CUSTOMER_A),
    })
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_assignee"
    for version in (1, 2):
        response = await client.post("/api/v1/claims/CLM-2001/tasks", json={
            "expected_version": version, "title": "Photos", "requested_evidence": "damage_photos",
        })
        assert response.status_code == 200
    tasks = response.json()["tasks"]
    for version, task in zip((3, 4), tasks):
        response = await client.patch(f"/api/v1/claims/CLM-2001/tasks/{task['task_id']}",
                                      json={"expected_version": version, "status": "completed"})
        assert response.status_code == 200
        assert ("damage_photos" in response.json()["outstanding_evidence"]) == (version == 3)
    response = await client.post("/api/v1/claims/CLM-2001/assignment", json={
        "expected_version": 5, "adjuster_id": auth.subject_id(TENANT, CUSTOMER_B),
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_case_commands_role_concurrency_limits_and_failed_storage_leave_no_partial_effects(api, monkeypatch):
    client, repository, identity = api
    for actor in (CUSTOMER_A, PRESENTER):
        response = await client.post("/api/v1/claims/CLM-2001/notes", json={
            "expected_version": 1, "body": "Not authorised.",
        }, headers={"Authorization": f"Bearer {identity[1](actor)}"})
        assert response.status_code == 403
    employee = {"Authorization": f"Bearer {identity[1](EMPLOYEE)}"}
    before = await repository.get("run-a")
    monkeypatch.setattr(repository, "replace", AsyncMock(side_effect=CosmosHttpResponseError(status_code=503)))
    response = await client.post("/api/v1/claims/CLM-2001/notes", headers=employee,
                                 json={"expected_version": 1, "body": "Unconfirmed write."})
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
    assert (await repository.get("run-a")).state == before.state
    for setting, path, body in (
        ("MAX_TASKS", "tasks", {"title": "Photos", "requested_evidence": "damage_photos"}),
        ("MAX_NOTES", "notes", {"body": "A fictional note."}),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(service, setting, 0)
            response = await client.post(f"/api/v1/claims/CLM-2001/{path}", headers=employee,
                                         json={"expected_version": 1, **body})
            assert response.status_code == 409 and response.json()["error"]["code"] == "run_capacity"
    assert (await repository.get("run-a")).etag == before.etag


@pytest.mark.asyncio
async def test_validation_and_throttling_are_structured_without_echoing_private_input(api, monkeypatch, caplog):
    client, repository, _ = api
    response = await client.post("/api/v1/claims", json={**REPORT, "description": "raw-private-marker", "role": "spoof"},
                                 headers={"Idempotency-Key": "validation"})
    assert response.status_code == 422 and "raw-private-marker" not in response.text
    assert "raw-private-marker" not in caplog.text
    monkeypatch.setattr(repository, "get", AsyncMock(side_effect=CosmosHttpResponseError(status_code=429)))
    response = await client.get("/api/v1/me")
    assert response.status_code == 429 and response.headers["Retry-After"] == "1"
    assert response.json()["error"]["correlation_id"] == response.headers["X-Correlation-ID"]


@pytest.mark.asyncio
async def test_seed_command_is_preview_only_and_requires_canonical_identity_mappings(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_insurance_run_test", ROOT / "scripts" / "seed-insurance-run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actors = tmp_path / "actors.json"
    actors.write_text(json.dumps({
        auth.subject_id(TENANT, CUSTOMER_A): {"role": "policyholder", "policyholder_id": "PH-1001"},
    }), encoding="utf-8")
    create = AsyncMock(side_effect=AssertionError("Preview must not write"))
    monkeypatch.setattr(module.CosmosBusinessRepository, "create", create)
    summary = await module.seed("run-preview", actors)
    assert summary["mode"] == "preview" and summary["existing_runs_overwritten"] is False
    assert "identity_mappings" in summary and CUSTOMER_A not in json.dumps(summary)
    create.assert_not_called()
    actors.write_text(json.dumps({"customer-a": {"role": "presenter"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical"):
        await module.seed("run-preview", actors)
