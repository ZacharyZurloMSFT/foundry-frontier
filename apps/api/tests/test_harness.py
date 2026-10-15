import asyncio
import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.harness import routes, runtime, store, tools
from app.harness.models import (
    EVENT_ADAPTER,
    Error,
    HarnessError,
    RunRequest,
    SpecialistInput,
    Text,
    redact,
)
from app.settings import Settings
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from fastapi import FastAPI
from pydantic import ValidationError


class Container:
    def __init__(self):
        self.items = {}
        self.etag = 0

    async def create_item(self, item):
        pk = item.get("workflowId") or item["sessionId"]
        key = (pk, item["id"])
        if key in self.items:
            raise CosmosResourceExistsError(status_code=409)
        self.etag += 1
        self.items[key] = copy.deepcopy({**item, "_etag": str(self.etag)})
        return copy.deepcopy(self.items[key])

    async def read_item(self, item, partition_key):
        if (partition_key, item) not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return copy.deepcopy(self.items[(partition_key, item)])

    async def replace_item(self, name, item, *, etag, match_condition):
        key = (item["workflowId"], name)
        if self.items[key]["_etag"] != etag:
            raise CosmosHttpResponseError(status_code=412)
        self.etag += 1
        self.items[key] = copy.deepcopy({**item, "_etag": str(self.etag)})
        return copy.deepcopy(self.items[key])

    def query_items(self, **kwargs):
        async def items():
            rows = [v for (pk, _), v in self.items.items() if pk == kwargs["partition_key"]]
            for row in sorted(rows, key=lambda v: v.get("seq", 0)):
                if row.get("record_kind") == "harness_event":
                    yield copy.deepcopy(row)
        return items()


@pytest.fixture
def config(monkeypatch):
    settings = Settings(_env_file=None, COPILOT_HARNESS_ENABLED=True,
                        FOUNDRY_PROJECT_ENDPOINT="https://unit.test/api/projects/northwind",
                        MCP_WORK_ORDER_URL="https://mcp.unit.test/mcp/",
                        COSMOS_ENDPOINT="https://unit.test/cosmos")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "runtime", runtime.HarnessRuntime())
    return settings


def test_contract_and_prompt(config):
    with pytest.raises(ValidationError):
        RunRequest(request_id=uuid.uuid4(), message=" ")
    with pytest.raises(ValidationError):
        SpecialistInput(prompt="look up an order", workflow_id="attacker")
    with pytest.raises(ValidationError):
        RunRequest(request_id=uuid.uuid4(), message="x" * 8001)
    prompt, sha = runtime.instructions(config)
    assert "resolution_draft" in prompt and len(sha) == 64
    assert EVENT_ADAPTER.validate_python(Error(code="test", message="test").model_dump()).kind == "error"
    assert "secret" not in redact("Bearer secret")


def test_runtime_environment_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-inherit")
    monkeypatch.setenv("FOUNDRY_API_KEY", "do-not-inherit")
    monkeypatch.setenv("PATH", "test-path")
    assert runtime.runtime_environment()["PATH"] == "test-path"
    assert "GITHUB_TOKEN" not in runtime.runtime_environment()
    assert "FOUNDRY_API_KEY" not in runtime.runtime_environment()
    assert runtime.deny_permission(None, None).kind == "reject"


@pytest.mark.asyncio
async def test_idempotency_and_replica_reads(database):
    request = RunRequest(request_id=uuid.uuid4(), message="order status")
    first, created = await store.RunStore().create(request, "model", "sha", 60)
    again, replay = await store.RunStore().create(request, "model", "sha", 60)
    assert created and not replay
    assert first["sessionId"] == again["sessionId"]
    with pytest.raises(HarnessError, match="different input"):
        await store.RunStore().create(request.model_copy(update={"message": "different"}), "model", "sha", 60)
    event = Text(kind="assistant_message", text="done", run_id=first["sessionId"], seq=1)
    await store.RunStore().append(first["sessionId"], event)
    await store.RunStore().finish(first["sessionId"], "succeeded", "done")
    result = await store.RunStore().snapshot(first["sessionId"])
    assert result["status"] == "succeeded" and result["events"][0]["text"] == "done"
    await store.RunStore().finish(first["sessionId"], "failed", "must not overwrite")
    assert (await store.RunStore().snapshot(first["sessionId"]))["output"] == "done"


@pytest.mark.asyncio
async def test_expired_worker_reconciles_without_replay(database):
    saved, _ = await store.RunStore().create(RunRequest(request_id=uuid.uuid4(), message="hi"), "model", "sha", -1)
    result = await store.RunStore().snapshot(saved["sessionId"])
    assert result["status"] == "interrupted"
    assert "deadline" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_routes_stream_and_save(config, database, monkeypatch, fail):
    async def fake_run(*_):
        yield Text(kind="assistant_delta", text="real event")
        if fail:
            raise HarnessError("provider_failed", "Provider failed")
        yield Text(kind="assistant_message", text="complete answer")
    monkeypatch.setattr(routes.runtime, "run", fake_run)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {"request_id": str(uuid.uuid4()), "message": "status"}
        response = await client.post("/harness/runs", json=payload)
        assert response.status_code == 200
        events = [EVENT_ADAPTER.validate_json(line) for line in response.text.splitlines()]
        assert events[0].kind == "run_started"
        assert events[-1].kind == "done"
        assert events[-1].status == ("failed" if fail else "succeeded")
        saved = (await client.get(f"/harness/runs/{events[0].run_id}")).json()
        assert saved["output"] == ("real event" if fail else "complete answer")
        assert (await client.post("/harness/runs", json=payload)).status_code == 409
        assert routes.runtime.active == 0
        config.harness_enabled = False
        assert (await client.post("/harness/runs", json=payload)).status_code == 503
        assert (await client.get(f"/harness/runs/{events[0].run_id}")).status_code == 200


@pytest.mark.asyncio
async def test_cancel_releases_capacity_and_saves_state(config, database, monkeypatch):
    closed = False
    async def fake_run(*_):
        nonlocal closed
        try:
            yield Text(kind="assistant_delta", text="working")
            await asyncio.sleep(60)
        finally:
            closed = True
    monkeypatch.setattr(routes.runtime, "run", fake_run)
    response = await routes.run(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
                                RunRequest(request_id=uuid.uuid4(), message="hi"))
    first = json.loads(await anext(response.body_iterator))
    await anext(response.body_iterator)
    with pytest.raises(asyncio.CancelledError):
        await response.body_iterator.athrow(asyncio.CancelledError())
    assert closed and routes.runtime.active == 0
    assert (await store.RunStore().snapshot(first["run_id"]))["status"] == "cancelled"


class Session:
    def __init__(self, callback, fail=False):
        self.callback, self.fail = callback, fail
        self.abort = AsyncMock()
        self.disconnect = AsyncMock()

    async def send_and_wait(self, *_args, **_kwargs):
        self.callback(SimpleNamespace(type=SimpleNamespace(value="assistant.message_delta"),
                                      data=SimpleNamespace(delta_content="hello")))
        if self.fail:
            raise RuntimeError("SDK failed")
        return SimpleNamespace(data=SimpleNamespace(content="hello"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_sdk_sessions_are_restricted_and_cleaned(config, monkeypatch, fail):
    r = runtime.HarnessRuntime()
    captured = {}
    async def create(**kwargs):
        captured.update(kwargs)
        return Session(kwargs["on_event"], fail=fail)
    r.client = SimpleNamespace(create_session=AsyncMock(side_effect=create), delete_session=AsyncMock())
    r.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="test")))
    session = None
    original_create = r.client.create_session
    async def capture(**kwargs):
        nonlocal session
        session = await original_create(**kwargs)
        return session
    r.client.create_session = capture
    if fail:
        with pytest.raises(RuntimeError, match="SDK failed"):
            _ = [e async for e in r.run("harness-test", "wf-harness-test", "hi", store.RunStore())]
    else:
        events = [e async for e in r.run("harness-test", "wf-harness-test", "hi", store.RunStore())]
        assert events[-1].kind == "assistant_message"
    assert captured["available_tools"] == [f"custom:{n}" for n in tools.NAMES]
    assert captured["enable_config_discovery"] is False
    assert captured["enable_session_store"] is False
    assert "api_key" not in captured["provider"]
    assert await captured["provider"]["bearer_token_provider"]({}) == "test"
    assert session.disconnect.await_count == 1
    assert session.abort.await_count == int(fail)
    r.client.delete_session.assert_awaited_once_with("harness-test")


@pytest.mark.asyncio
async def test_specialist_limit_and_real_draft_required(config, monkeypatch):
    from copilot.tools import ToolInvocation
    output = SimpleNamespace(status="completed", output_text="Draft awaiting approval", output=[], usage=None)
    invoke = AsyncMock(return_value=output)
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", invoke)
    events = []
    t = tools.SpecialistTools(config, "harness-test", "wf-harness-test", events.append, store.RunStore())
    result = await t.handler("resolution_draft")(ToolInvocation(tool_call_id="c", arguments={"prompt": "draft"}))
    assert result.result_type == "failure"
    assert not any(e.kind == "approval_pending" for e in events)
    result = await t.handler("resolution_draft")(ToolInvocation(tool_call_id="d", arguments={"prompt": "again"}))
    assert result.result_type == "failure" and invoke.await_count == 1
    t.calls = config.harness_max_tools
    await t.handler("triage")(ToolInvocation(tool_call_id="e", arguments={"prompt": "hi"}))
    assert invoke.await_count == 1


def test_mcp_payload_envelopes():
    expected = {"status": "pending_approval"}
    assert tools.mcp_payload(json.dumps(expected)) == expected
    assert tools.mcp_payload(json.dumps({"content": [{"type": "text", "text": json.dumps(expected)}]})) == expected
    with pytest.raises(HarnessError):
        tools.mcp_payload("[]")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,accepted", [("propose_resolution", True), ("unrelated_tool", False)])
async def test_hosted_draft_evidence_is_bound_to_the_proposal_call(config, monkeypatch, tool_name, accepted):
    from copilot.tools import ToolInvocation
    wid = "wf-harness-test"
    draft = {"order_id": "NW-40021"}
    saved = {"id": "approval-test", "status": "pending", "payload": draft}
    result = {"status": "pending_approval", "workflow_id": wid, "hitl_id": saved["id"], "draft": draft}
    response = SimpleNamespace(status="completed", output_text="Pending human approval", usage=None, output=[
        SimpleNamespace(type="function_call", name=tool_name, call_id="proposal-call"),
        SimpleNamespace(type="function_call_output", call_id="proposal-call", output=json.dumps(result)),
    ])
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", AsyncMock(return_value=response))
    saved_store = SimpleNamespace(approval=AsyncMock(return_value=saved))
    events = []
    specialist = tools.SpecialistTools(config, "harness-test", wid, events.append, saved_store)
    outcome = await specialist.handler("resolution_draft")(
        ToolInvocation(tool_call_id="invocation", arguments={"prompt": "draft"})
    )
    assert (outcome.result_type == "success") is accepted
    assert any(event.kind == "approval_pending" for event in events) is accepted
