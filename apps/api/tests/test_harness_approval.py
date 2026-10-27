import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import repositories
from app.harness import store
from app.harness.models import RunRequest
from app.insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload

CLAIM_REVIEW_DIR = "mcp-claim-review"
DRAFT = {
    "claim_id": "CLM-2001", "policy_id": "POL-1001", "policyholder_id": "PH-1001",
    "action_type": "request_evidence", "summary": "Request the outstanding evidence for human review.",
    "policy_refs": ["NW-AUTO-WA-2026-v1#duties-after-loss"],
}


@pytest.fixture
def mcp_module(monkeypatch, database):
    path = Path(__file__).resolve().parents[2] / CLAIM_REVIEW_DIR / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("harness_test_mcp", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_cosmos_container", lambda: database["hitl_queue"])
    class Cosmos:
        def get_database_client(self, _):
            return self
        def get_container_client(self, name):
            return database[name]
    monkeypatch.setattr(module, "_cosmos", Cosmos())
    return module


def bound(workflow_id):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={"x-workflow-id": workflow_id})))


@pytest.mark.asyncio
async def test_mcp_draft_is_idempotent_per_workflow_and_conflicts_fail(mcp_module, database):
    saved, _ = await store.RunStore().create(
        RunRequest(request_id=uuid.uuid4(), message="Request evidence for CLM-2001"), "model", "sha", 60)
    wid = saved["workflowId"]
    ctx = bound(wid)
    first = json.loads(await mcp_module.draft_claim_action(wid, DRAFT, ctx))
    second = json.loads(await mcp_module.draft_claim_action(wid, DRAFT, ctx))
    assert first == second and first["status"] == "pending_approval"
    with pytest.raises(ValueError, match="different or decided"):
        await mcp_module.draft_claim_action(
            wid, {**DRAFT, "summary": "A different request for the same workflow."}, ctx)
    assert len(database["hitl_queue"].items) == 1
    with pytest.raises(ValueError, match="application-bound"):
        await mcp_module.draft_claim_action("wf-harness-" + str(uuid.uuid4()), DRAFT, ctx)
    unbound = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={})))
    with pytest.raises(ValueError, match="application-bound"):
        await mcp_module.draft_claim_action("wf-unbound", DRAFT, unbound)
    assert len(database["hitl_queue"].items) == 1


@pytest.mark.asyncio
async def test_human_decision_cannot_be_overwritten(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(repositories, "_container", container)
    wid = "wf-harness-" + str(uuid.uuid4())
    await database["hitl_queue"].create_item({
        "id": f"hitl-{wid}", "workflowId": wid, "status": "pending", "kind": ACTION_KIND,
        "payload": action_payload(ClaimActionDraft.model_validate(DRAFT)),
    })
    first = await repositories.resolve_hitl(f"hitl-{wid}", wid, "approve")
    again = await repositories.resolve_hitl(f"hitl-{wid}", wid, "approve")
    assert first["status"] == "approved" and again["_decision_replayed"]
    assert first["execution_status"] == "not_executed"
    with pytest.raises(ValueError, match="already been decided"):
        await repositories.resolve_hitl(f"hitl-{wid}", wid, "reject")


@pytest.mark.asyncio
async def test_only_insurance_claim_action_drafts_can_be_decided(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(repositories, "_container", container)
    wid = "wf-harness-" + str(uuid.uuid4())
    await database["hitl_queue"].create_item({
        "id": f"hitl-{wid}", "workflowId": wid, "status": "pending", "kind": "unrelated_kind", "payload": {}})
    with pytest.raises(ValueError, match="insurance claim-action drafts"):
        await repositories.resolve_hitl(f"hitl-{wid}", wid, "approve")
    assert database["hitl_queue"].items[(wid, f"hitl-{wid}")]["status"] == "pending"
