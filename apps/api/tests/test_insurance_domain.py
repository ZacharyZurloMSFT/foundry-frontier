import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.insurance_domain import ClaimActionDraft, InsuranceValidationError, fixtures, lookup_claim_record, lookup_policy_record
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
DRAFT = {
    "claim_id": "CLM-2001", "policy_id": "POL-1001", "policyholder_id": "PH-1001",
    "action_type": "request_evidence", "summary": "Request the missing damage photos.",
    "policy_refs": ["NW-AUTO-WA-2026-v1#duties-after-loss"],
}


@pytest.fixture(autouse=True)
def insurance_domain(monkeypatch):
    monkeypatch.setenv("NORTHWIND_BUSINESS_DOMAIN", "insurance")


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
async def test_lookup_mcp_schema_and_validation():
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
async def test_mcp_persistence_binding_and_approval_only(database, monkeypatch):
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
    before = fixtures()
    first = json.loads(await module.draft_claim_action(workflow, draft, ctx))
    assert first == json.loads(await module.draft_claim_action(workflow, draft, ctx))
    assert first["status"] == "pending_approval" and first["execution_status"] == "not_executed"
    assert (await queue.read_item(first["hitl_id"], workflow))["sessionId"] == "test"
    assert len(queue.items) == 1 and before == fixtures()
    with pytest.raises(ValueError, match="application-bound"):
        await module.draft_claim_action("wf-other", draft, ctx)
    queue.items[(workflow, first["hitl_id"])]["status"] = "approved"
    with pytest.raises(ValueError, match="decided draft"):
        await module.draft_claim_action(workflow, draft, ctx)
    monkeypatch.setattr(module, "_cosmos_container", lambda: None)
    with pytest.raises(RuntimeError, match="no draft was saved"):
        await module.draft_claim_action(workflow, draft, ctx)
