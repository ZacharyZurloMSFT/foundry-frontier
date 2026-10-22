"""The work-order MCP server's retained `propose_resolution` tool.

The retail resolution-draft agent still calls it. The Copilot SDK harness that used to share it was
deleted, so its hook must stay unreachable: nothing creates harness runs any more, and an
unauthenticated caller must not be able to make the server act as if one existed.
"""
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
DRAFT = {"order_id": "NW-40021", "customer_id": "C-8801", "resolution_type": "refund_full", "amount_usd": 189,
         "reason": "Late delivery", "policy_ref": "SHIPPING-SLA-STD-2026", "urgency": "P2"}


def request(headers=None):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers or {})))


def harness_workflow():
    return "wf-harness-" + str(uuid.uuid4())


@pytest.fixture
def module(monkeypatch, database):
    path = ROOT / "apps" / "mcp-work-order" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("resolution_test_mcp", path)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)

    class Cosmos:
        def get_database_client(self, _):
            return self

        def get_container_client(self, name):
            return database[name]

    monkeypatch.setattr(loaded, "_cosmos_container", lambda: database["hitl_queue"])
    monkeypatch.setattr(loaded, "_cosmos", Cosmos())
    return loaded


@pytest.mark.asyncio
async def test_a_draft_is_queued_as_pending_and_never_decided(module, database):
    result = json.loads(await module.propose_resolution("wf-resolution-1", DRAFT, request()))
    assert result["status"] == "pending_approval" and result["workflow_id"] == "wf-resolution-1"
    assert result["draft"] == DRAFT
    queued = await database["hitl_queue"].read_item(result["hitl_id"], "wf-resolution-1")
    assert queued["status"] == "pending" and queued["kind"] == "resolution"
    assert queued["source"] == "mcp-resolution-draft" and queued["payload"] == DRAFT
    assert len(database["hitl_queue"].items) == 1


@pytest.mark.asyncio
async def test_each_call_queues_its_own_item(module, database):
    first = json.loads(await module.propose_resolution("wf-resolution-2", DRAFT, request()))
    second = json.loads(await module.propose_resolution("wf-resolution-2", DRAFT, request()))
    assert first["hitl_id"] != second["hitl_id"] and len(database["hitl_queue"].items) == 2


@pytest.mark.asyncio
async def test_without_cosmos_the_caller_is_told_nothing_was_persisted(module, database, monkeypatch):
    monkeypatch.setattr(module, "_cosmos_container", lambda: None)
    result = json.loads(await module.propose_resolution("wf-resolution-3", DRAFT, request()))
    assert "not persisted" in result["warning"] and not database["hitl_queue"].items


@pytest.mark.asyncio
async def test_a_failed_save_is_an_error_not_a_pending_approval(module, monkeypatch):
    class Broken:
        async def create_item(self, item):
            raise RuntimeError("cosmos is down")

    monkeypatch.setattr(module, "_cosmos_container", lambda: Broken())
    assert json.loads(await module.propose_resolution("wf-resolution-4", DRAFT, request()))["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"x-harness-workflow": "wf-harness-someone-else"}])
async def test_a_harness_workflow_id_needs_a_matching_application_binding(module, database, headers):
    result = json.loads(await module.propose_resolution(harness_workflow(), DRAFT, request(headers)))
    assert result["status"] == "error" and not database["hitl_queue"].items


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [
    None,
    {"record_kind": "insurance_run", "status": "running", "deadline": time.time() + 60},
    {"record_kind": "harness_run", "status": "completed", "deadline": time.time() + 60},
    {"record_kind": "harness_run", "status": "running", "deadline": time.time() - 60},
])
async def test_a_bound_harness_workflow_needs_an_active_harness_run(module, database, run):
    workflow = harness_workflow()
    if run is not None:
        await database["workflow_state"].create_item({"id": workflow, "workflowId": workflow, **run})
    result = json.loads(await module.propose_resolution(workflow, DRAFT, request({"x-harness-workflow": workflow})))
    assert result["status"] == "error" and not database["hitl_queue"].items
