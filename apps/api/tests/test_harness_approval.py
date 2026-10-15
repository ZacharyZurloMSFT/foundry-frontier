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


@pytest.fixture
def mcp_module(monkeypatch, database):
    path = Path(__file__).resolve().parents[2] / "mcp-work-order" / "app" / "main.py"
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


@pytest.mark.asyncio
async def test_mcp_harness_idempotency_and_legacy_path(mcp_module, database):
    saved, _ = await store.RunStore().create(RunRequest(request_id=uuid.uuid4(), message="refund"), "model", "sha", 60)
    wid = saved["workflowId"]
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"x-harness-workflow": wid})))
    draft = {"order_id": "NW-40021", "customer_id": "C-8801", "resolution_type": "refund_full",
             "amount_usd": 189, "reason": "Late delivery", "policy_ref": "SHIPPING-SLA-STD-2026", "urgency": "P2"}
    first = json.loads(await mcp_module.propose_resolution(wid, draft, ctx))
    second = json.loads(await mcp_module.propose_resolution(wid, draft, ctx))
    assert first == second and first["status"] == "pending_approval"
    conflict = json.loads(await mcp_module.propose_resolution(wid, {**draft, "amount_usd": 100}, ctx))
    assert conflict["status"] == "error"
    assert len(database["hitl_queue"].items) == 1
    missing = json.loads(await mcp_module.propose_resolution("wf-harness-" + str(uuid.uuid4()), draft, ctx))
    assert missing["status"] == "error"
    legacy_ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={})))
    a = json.loads(await mcp_module.propose_resolution("wf-legacy", draft, legacy_ctx))
    b = json.loads(await mcp_module.propose_resolution("wf-legacy", draft, legacy_ctx))
    assert a["hitl_id"] != b["hitl_id"]


@pytest.mark.asyncio
async def test_human_decision_cannot_be_overwritten(database, monkeypatch):
    async def container(name):
        return database[name]
    monkeypatch.setattr(repositories, "_container", container)
    wid = "wf-harness-" + str(uuid.uuid4())
    await database["hitl_queue"].create_item({"id": f"hitl-{wid}", "workflowId": wid, "status": "pending"})
    first = await repositories.resolve_hitl(f"hitl-{wid}", wid, "approve")
    again = await repositories.resolve_hitl(f"hitl-{wid}", wid, "approve")
    assert first["status"] == "approved" and again["_decision_replayed"]
    with pytest.raises(ValueError, match="already been decided"):
        await repositories.resolve_hitl(f"hitl-{wid}", wid, "reject")
