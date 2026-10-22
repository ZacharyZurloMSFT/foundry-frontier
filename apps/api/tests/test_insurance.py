import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import insurance, main, orchestrator_client, repositories
from app.insurance_domain import ClaimActionDraft, InsuranceValidationError, fixtures, lookup_claim_record, lookup_policy_record
from app.settings import Settings
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
DRAFT = {
    "claim_id": "CLM-2001", "policy_id": "POL-1001", "policyholder_id": "PH-1001",
    "action_type": "request_evidence", "summary": "Request the missing damage photos.",
    "policy_refs": ["NW-AUTO-WA-2026-v1#duties-after-loss"],
}


@pytest.fixture
def insurance_config(monkeypatch):
    monkeypatch.setenv("NORTHWIND_BUSINESS_DOMAIN", "insurance")
    settings = Settings(_env_file=None, NORTHWIND_BUSINESS_DOMAIN="insurance",
                        COSMOS_ENDPOINT="https://cosmos.invalid",
                        NORTHWIND_INSURANCE_AGENTS_READY=False)
    for module in (insurance, main, orchestrator_client, repositories):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


def load_mcp(service):
    path = ROOT / "apps" / service / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("insurance_test_" + service.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verified_records_and_applicable_wording():
    result = lookup_claim_record("CLM-2001", "POL-1001", "PH-1001")
    assert result["synthetic"] and result["policy_wording"]["state"] == "WA"
    assert result["policy"]["coverages"][0]["deductible_usd"] == 500
    assert result["citations"][0]["version"] == "1"
    rental = result["policy"]["endorsements"][0]
    assert rental["review_only"] and rental["synthetic"] and rental["limit_usd"] == 900
    assert rental["policy_ref"] in {c["policy_ref"] for c in result["citations"]}
    glass = lookup_claim_record("CLM-2002")["policy"]["endorsements"][0]
    assert glass["deductible_usd"] == 100 and glass["state"] == "CO" and glass["review_only"]
    assert "fictional" in result["disclaimer"]
    with pytest.raises(InsuranceValidationError, match="Unknown"):
        lookup_claim_record("CLM-9999")
    with pytest.raises(InsuranceValidationError, match="do not match"):
        lookup_claim_record("CLM-2001", "POL-1002")
    with pytest.raises(InsuranceValidationError, match="do not match"):
        lookup_policy_record("POL-1001", "PH-1002")
    with pytest.raises(InsuranceValidationError, match="jurisdiction"):
        lookup_policy_record("POL-1001", state="CO")
    with pytest.raises(InsuranceValidationError, match="effective"):
        lookup_policy_record("POL-1001", loss_date="2025-12-31")


@pytest.mark.parametrize("changes", [
    {"action_type": "approve_claim"}, {"action_type": "deny_coverage"}, {"action_type": "payment"},
    {"policy_id": "POL-1002"}, {"claim_id": "CLM-9999"}, {"policyholder_id": "PH-1002"},
    {"policy_refs": ["NW-AUTO-CO-2026-v1#comprehensive"]},
    {"amount_usd": 100}, {"action_type": "settlement_recommendation"},
    {"action_type": "settlement_recommendation", "amount_usd": float("nan")},
    {"action_type": "settlement_recommendation", "amount_usd": True},
    {"action_type": "settlement_recommendation", "amount_usd": 901,
     "policy_refs": ["NW-AUTO-WA-2026-v1#rental-reimbursement"]},
])
def test_unsafe_or_unverified_drafts_rejected(changes):
    with pytest.raises(ValidationError):
        ClaimActionDraft.model_validate({**DRAFT, **changes})


@pytest.mark.asyncio
async def test_lookup_mcp_schema_and_validation(insurance_config):
    module = load_mcp("mcp-tech-lookup")
    tools = {t.name: t.inputSchema for t in await module.mcp.list_tools()}
    assert {"lookup_order", "lookup_policy", "lookup_claim"} <= tools.keys()
    assert tools["lookup_policy"]["required"] == ["policy_id"]
    assert {"policyholder_id", "loss_date", "state"} <= tools["lookup_policy"]["properties"].keys()
    assert tools["lookup_claim"]["required"] == ["claim_id"]
    assert json.loads(module.lookup_claim("CLM-2001"))["claim"]["status"] == "awaiting_evidence"
    with pytest.raises(ValueError, match="do not match"):
        module.lookup_claim("CLM-2001", "POL-1002")
    assert json.loads(module.lookup_order("NW-40021"))[0]["order_id"] == "NW-40021"


@pytest.mark.asyncio
async def test_mcp_persistence_binding_and_approval_only(insurance_config, database, monkeypatch):
    module = load_mcp("mcp-work-order")
    queue = database["hitl_queue"]
    monkeypatch.setattr(module, "_cosmos_container", lambda: queue)
    schemas = {t.name: t.inputSchema for t in await module.mcp.list_tools()}
    assert {"propose_resolution", "draft_claim_action"} <= schemas.keys()
    schema = schemas["draft_claim_action"]
    assert "draft" in schema["required"] and "workflow_id" in schema["required"]
    encoded = json.dumps(schema)
    assert "settlement_recommendation" in encoded and "policy_refs" in encoded
    assert "approve_claim" not in encoded and "payment" not in encoded
    workflow = "wf-insurance-test"
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={"x-orders-workflow": workflow})))
    draft = module.ClaimActionDraft.model_validate(DRAFT)
    first = json.loads(await module.draft_claim_action(workflow, draft, ctx))
    assert first == json.loads(await module.draft_claim_action(workflow, draft, ctx))
    assert first["status"] == "pending_approval" and first["execution_status"] == "not_executed"
    assert (await queue.read_item(first["hitl_id"], workflow))["sessionId"] == "test"
    assert len(queue.items) == 1
    with pytest.raises(ValueError, match="application-bound"):
        await module.draft_claim_action("wf-other", draft, ctx)

    async def container(_):
        return queue
    monkeypatch.setattr(repositories, "_container", container)
    before = fixtures()
    approved = await repositories.resolve_hitl(first["hitl_id"], workflow, "approve", "Human draft review")
    assert approved["status"] == "approved" and approved["execution_status"] == "not_executed"
    assert approved["review_scope"] == "draft_only" and before == fixtures()
    assert (await repositories.resolve_hitl(first["hitl_id"], workflow, "approve"))["_decision_replayed"]
    with pytest.raises(ValueError, match="already been decided"):
        await repositories.resolve_hitl(first["hitl_id"], workflow, "reject")
    with pytest.raises(ValueError, match="decided draft"):
        await module.draft_claim_action(workflow, draft, ctx)
    monkeypatch.setattr(module, "_cosmos_container", lambda: None)
    with pytest.raises(RuntimeError, match="no draft was saved"):
        await module.draft_claim_action(workflow, draft, ctx)


@pytest.mark.asyncio
async def test_api_fixture_validation_persistence_and_readiness(insurance_config, database, monkeypatch):
    async def container(_):
        return database["hitl_queue"]
    monkeypatch.setattr(repositories, "_container", container)
    monkeypatch.setattr(insurance, "run_turn", AsyncMock(side_effect=AssertionError("No model call allowed")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local.test") as client:
        data = (await client.get("/insurance/fixtures")).json()
        assert data["business_domain"] == "insurance" and data["agent_runtime_ready"] is False
        assert data["policyholders"][0]["policyholder_id"] == "PH-1001"
        assert (await client.get("/insurance/claims/CLM-9999")).status_code == 404
        assert (await client.get("/insurance/claims/CLM-2001?policy_id=POL-1002")).status_code == 422
        assert (await client.get("/insurance/policies/POL-1001?state=CO")).status_code == 422
        response = await client.post("/insurance/actions/draft", json=DRAFT)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "pending_approval"
        saved = await database["hitl_queue"].read_item(result["hitl_id"], result["workflow_id"])
        assert saved["kind"] == "insurance_claim_action" and saved["status"] == "pending"
        assert (await client.post("/insurance/actions/draft", json={**DRAFT, "policy_id": "POL-1002"})).status_code == 422
        for route in ("/insurance/chat", "/chat", "/qa", "/agent/manual/chat", "/agent/claims/chat",
                      "/handoff", "/voice/session"):
            body = {"message": "Claim help", "question": "Policy help", "summary": "Claim help"}
            blocked = await client.post(route, json=body)
            assert blocked.status_code == 503, (route, blocked.text)
            assert "insurance" in blocked.text.lower()


@pytest.mark.asyncio
async def test_human_api_decision_never_claims_payment(insurance_config, database, monkeypatch):
    queue = database["hitl_queue"]
    async def container(_):
        return queue
    monkeypatch.setattr(repositories, "_container", container)
    monkeypatch.setattr(main, "append_message", AsyncMock())
    draft = ClaimActionDraft.model_validate({
        **DRAFT, "action_type": "settlement_recommendation", "amount_usd": 1200,
        "policy_refs": ["NW-AUTO-WA-2026-v1#collision"],
    })
    saved = await repositories.enqueue_hitl("wf-insurance-human", "insurance_claim_action", draft.model_dump(exclude_none=True))
    assert saved["sessionId"] is None
    standalone = await main.hitl_decide(main.HitlDecision(hitl_id=saved["id"], workflow_id=saved["workflowId"], decision="approve"))
    assert standalone["chat_message"] is None and standalone["session_id"] is None
    row = await queue.read_item(saved["id"], saved["workflowId"])
    row["sessionId"] = "human"
    await queue.replace_item(saved["id"], row, etag=row["_etag"], match_condition=None)
    result = await main.hitl_decide(main.HitlDecision(hitl_id=saved["id"], workflow_id=saved["workflowId"], decision="approve"))
    assert result["status"] == "approved" and result["execution_status"] == "not_executed"
    assert "no coverage decision" in result["chat_message"] and "payment was executed" in result["chat_message"]
    assert result["payload"]["amount_usd"] == 1200
    assert result["session_id"] == "human"


def test_ready_requires_separate_agent_config_and_never_retail(insurance_config):
    assert not insurance.insurance_runtime_ready()
    insurance_config.insurance_agents_ready = True
    insurance_config.order_agents_hosted = True
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind-orders"
    insurance_config.insurance_orchestrator_agent_id = "northwind-orchestrator"
    assert not insurance.insurance_runtime_ready()
    insurance_config.insurance_orchestrator_agent_id = "northwind-insurance-orchestrator"
    assert insurance.insurance_runtime_ready()
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind"
    assert not insurance.insurance_runtime_ready()
    insurance_config.order_project_name = "northwind"
    assert not insurance.insurance_runtime_ready()


def test_ready_accepts_the_retained_insurance_project_only_with_a_matching_endpoint(insurance_config):
    insurance_config.insurance_agents_ready = True
    insurance_config.order_agents_hosted = True
    insurance_config.insurance_orchestrator_agent_id = "northwind-insurance-orchestrator"
    insurance_config.order_project_name = "nw-insurance-claims"
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/nw-insurance-claims"
    assert insurance.insurance_runtime_ready()
    assert insurance.INSURANCE_PROJECTS == {"northwind-orders", "nw-insurance-claims"}
    # The endpoint must name the configured project, and neither the legacy web project nor a look-alike is accepted.
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind-orders"
    assert not insurance.insurance_runtime_ready()
    for other in ("northwind", "nw-insurance-claims-old", "NW-INSURANCE-CLAIMS", ""):
        insurance_config.order_project_name = other
        insurance_config.foundry_project_endpoint = f"https://existing.invalid/api/projects/{other}"
        assert not insurance.insurance_runtime_ready()
    insurance_config.order_project_name = "nw-insurance-claims"
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/nw-insurance-claims"
    insurance_config.insurance_agents_ready = False
    assert not insurance.insurance_runtime_ready(), "readiness still needs the explicit flag"


@pytest.mark.asyncio
async def test_configured_insurance_inference_cannot_call_retail_agents(insurance_config, monkeypatch):
    insurance_config.insurance_agents_ready = True
    insurance_config.order_agents_hosted = True
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind-orders"
    insurance_config.insurance_orchestrator_agent_id = "northwind-insurance-orchestrator"
    called = AsyncMock(side_effect=AssertionError("Must never resolve a retail endpoint"))
    monkeypatch.setattr(orchestrator_client, "_project_client", called)
    with pytest.raises(ValueError, match="Retail agents"):
        async for _ in orchestrator_client.run_agent_turn("session", "Claim help", "northwind-orchestrator"):
            pass
    called.assert_not_called()
    monkeypatch.setattr(orchestrator_client, "_cached_agent_name", "northwind-orchestrator")
    assert await orchestrator_client.resolve_orchestrator_agent_id() == "northwind-insurance-orchestrator"


@pytest.mark.asyncio
async def test_missing_approval_storage_never_returns_success(insurance_config):
    insurance_config.cosmos_endpoint = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local.test") as client:
        response = await client.post("/insurance/actions/draft", json=DRAFT)
        assert response.status_code == 503 and "no draft was saved" in response.text


@pytest.mark.asyncio
async def test_verified_chat_uses_insurance_stream_contract(insurance_config, monkeypatch):
    insurance_config.insurance_agents_ready = True
    insurance_config.order_agents_hosted = True
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind-orders"
    insurance_config.insurance_orchestrator_agent_id = "northwind-insurance-orchestrator"
    calls = []
    async def run(session, message):
        calls.append((session, message))
        yield {"kind": "done", "workflow_id": "wf-insurance-unit"}
    monkeypatch.setattr(insurance, "run_turn", run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local.test") as client:
        response = await client.post("/insurance/chat", json={"session_id": "unit", "message": "Claim status"})
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0] == {"kind": "session", "session_id": "unit", "business_domain": "insurance"}
        assert events[-1]["kind"] == "done" and calls == [("unit", "Claim status")]


@pytest.mark.asyncio
async def test_insurance_voice_handoff_validates_linked_ids(insurance_config, monkeypatch):
    insurance_config.insurance_agents_ready = True
    insurance_config.order_agents_hosted = True
    insurance_config.foundry_project_endpoint = "https://existing.invalid/api/projects/northwind-orders"
    insurance_config.insurance_orchestrator_agent_id = "northwind-insurance-orchestrator"
    calls = []
    async def run(session, message):
        calls.append(message)
        yield {"kind": "done"}
    monkeypatch.setattr(main, "run_turn", run)
    payload = {"summary": "Please request missing photos.", "claim_id": "CLM-2001",
               "policy_id": "POL-1001", "policyholder_id": "PH-1001", "state": "WA"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local.test") as client:
        assert (await client.post("/handoff", json={**payload, "policy_id": "POL-1002"})).status_code == 422
        assert (await client.post("/handoff", json={**payload, "state": "CO"})).status_code == 422
        response = await client.post("/handoff", json=payload)
        assert response.status_code == 200 and "Claim: CLM-2001" in calls[0]


@pytest.mark.asyncio
async def test_live_cu_disabled_never_substitutes_authored_extraction(insurance_config, monkeypatch):
    from app import content_understanding
    monkeypatch.setattr(content_understanding, "get_settings", lambda: insurance_config)
    assert "/claims/extract" in main.app.openapi()["paths"]
    assert not hasattr(main, "_load_claims_extractions")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://local.test") as client:
        response = await client.post("/claims/extract", json={"filename": "nw-fnol-demo-2001.pdf"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "cu_disabled"
    assert response.json()["detail"]["extraction_mode"] == "unavailable"


@pytest.mark.parametrize("agents_ready", [False, True])
def test_insurance_harness_status_never_exposes_retail_tools(insurance_config, monkeypatch, agents_ready):
    from app.harness import runtime
    insurance_config.harness_enabled = True
    insurance_config.insurance_agents_ready = agents_ready
    monkeypatch.setattr(runtime, "get_settings", lambda: insurance_config)
    status = runtime.HarnessRuntime().status()
    assert status["enabled"] is True and status["state"] == "unavailable"
    assert status["business_domain"] == "insurance"
    assert "policy_claim_lookup" in status["tools"] and "claim_action_draft" in status["tools"]
    assert "order_lookup" not in status["tools"] and "resolution_draft" not in status["tools"]
    assert "Verified insurance agent configuration is required" in status["message"]
