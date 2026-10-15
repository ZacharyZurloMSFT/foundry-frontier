import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import insurance
from app.harness import insurance_tools, routes, runtime, store
from app.harness.models import HarnessError, RunRequest
from app.insurance_domain import ACTION_KIND, lookup_claim_record
from app.settings import Settings
from copilot.tools import ToolInvocation
from copilot._jsonrpc import JsonRpcError
from test_harness import Session


@pytest.fixture
def config(monkeypatch):
    settings = Settings(
        _env_file=None, NORTHWIND_BUSINESS_DOMAIN="insurance", COPILOT_HARNESS_ENABLED=True,
        NORTHWIND_INSURANCE_AGENTS_READY=True, ORDER_AGENTS_HOSTED=True,
        FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID="northwind-insurance-orchestrator",
        FOUNDRY_PROJECT_ENDPOINT="https://unit.test/api/projects/northwind-orders",
        MCP_WORK_ORDER_URL="https://mcp.unit.test/mcp/", COSMOS_ENDPOINT="https://cosmos.unit.test",
    )
    for module in (runtime, routes, store, insurance):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "runtime", runtime.HarnessRuntime())
    return settings


def invocation(arguments):
    return ToolInvocation(tool_call_id="call-test", arguments=arguments)


def draft_args():
    return {"claim_id": "CLM-2001", "policy_id": "POL-1001", "policyholder_id": "PH-1001",
            "action_type": "request_evidence", "summary": "Request the outstanding evidence for human review.",
            "policy_refs": ["NW-AUTO-WA-2026-v1#duties-after-loss"]}


def test_insurance_is_operator_controlled_and_domain_bound(config):
    r = runtime.HarnessRuntime()
    assert r.status()["state"] == "configured"
    assert r.status()["enabled"] is True
    assert set(r.status()["tools"]) == set(insurance_tools.NAMES)
    prompt, sha = runtime.instructions(config)
    assert "claim_action_draft" in prompt and "order_lookup" not in prompt and len(sha) == 64
    config.harness_enabled = False
    assert r.status()["state"] == "disabled"
    config.harness_enabled = True
    config.insurance_agents_ready = False
    assert r.status()["state"] == "unavailable"


def test_status_never_calls_a_started_but_unverified_runtime_ready(config):
    r = runtime.HarnessRuntime()
    r.client, r.credential = object(), object()
    assert r.status()["state"] == "configured"
    _, sha = runtime.instructions(config)
    r.verified_config = r.configuration(config, sha)
    assert r.status()["state"] == "ready"
    config.harness_model = "a-new-unverified-model"
    assert r.status()["state"] == "configured"
    config.harness_resolution_mcp = "http://unsafe.test/not-mcp"
    assert r.status()["state"] == "unavailable"


@pytest.mark.asyncio
async def test_true_sdk_configuration_exposes_only_insurance_tools(config):
    r = runtime.HarnessRuntime()
    captured = {}
    async def create(**kwargs):
        captured.update(kwargs)
        return Session(kwargs["on_event"])
    r.client = SimpleNamespace(create_session=AsyncMock(side_effect=create), delete_session=AsyncMock())
    r.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="refreshed")))
    events = [e async for e in r.run("harness-unit", "wf-harness-unit", "hello", store.RunStore())]
    assert captured["available_tools"] == [f"custom:{n}" for n in insurance_tools.NAMES]
    assert {tool.name for tool in captured["tools"]} == set(insurance_tools.NAMES)
    assert not {"order_lookup", "resolution_draft"} & {tool.name for tool in captured["tools"]}
    assert captured["enable_config_discovery"] is False and captured["enable_session_store"] is False
    assert captured["mcp_servers"] == {} and "api_key" not in captured["provider"]
    for _ in range(2):
        assert await captured["provider"]["bearer_token_provider"]({}) == "refreshed"
    assert r.credential.get_token.await_count == 2
    assert all(e.kind != "assistant_delta" for e in events)
    assert "No policy or claim evidence" in events[-1].text
    r.client.delete_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {}, {"claim_id": "CLM-9999"},
    {"claim_id": "CLM-2001", "policy_id": "POL-1002"},
    {"claim_id": "CLM-2001", "policyholder_id": "PH-1002"},
    {"claim_id": "CLM-2001", "workflow_id": "attacker"},
])
async def test_missing_mismatched_and_user_workflow_ids_fail_closed(config, args):
    tools = insurance_tools.InsuranceTools(config, "harness-unit", "wf-harness-unit", lambda _: None, store.RunStore())
    outcome = await tools.handler("policy_claim_lookup")(invocation(args))
    assert outcome.result_type == "failure" and not tools.citations


@pytest.mark.asyncio
async def test_authoritative_loss_context_and_citations(config):
    tools = insurance_tools.InsuranceTools(config, "harness-unit", "wf-harness-unit", lambda _: None, store.RunStore())
    result = await tools.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    record = json.loads(result.text_result_for_llm)
    assert record["claim"]["loss_date"] == "2026-09-18" and record["claim"]["state"] == "WA"
    assert record["citations"] == lookup_claim_record("CLM-2001")["citations"]
    final = tools.final_text("Review evidence first.")
    assert "NW-AUTO-WA-2026-v1#collision" in final and "urn:northwind:synthetic:policy:" in final
    with pytest.raises(HarnessError, match="not retrieved"):
        tools.final_text("See NW-AUTO-CO-2026-v1#glass.")
    mismatch = await tools.handler("policy_lookup")(invocation({
        "policy_id": "POL-1001", "claim_id": "CLM-2001", "loss_date": "2026-09-19",
    }))
    assert mismatch.result_type == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "kind", "workflow", "executed", "payload", "status", "call", "synthetic"])
async def test_native_draft_is_verified_against_exact_saved_review(config, database, monkeypatch, corrupt):
    request = RunRequest(request_id=uuid.uuid4(), message="Request evidence for CLM-2001")
    run, _ = await store.RunStore().create(request, "model", "sha", 60)
    wid = run["workflowId"]
    draft = draft_args()
    saved = {"id": f"hitl-{wid}", "workflowId": wid, "status": "pending", "kind": ACTION_KIND,
             "business_domain": "insurance", "synthetic": corrupt != "synthetic",
             "execution_status": "not_executed", "payload": draft}
    if corrupt == "kind":
        saved["kind"] = "resolution"
    elif corrupt == "executed":
        saved["execution_status"] = "executed"
    elif corrupt == "status":
        saved["status"] = "approved"
    elif corrupt == "payload":
        saved["payload"] = {**draft, "summary": "Changed by specialist"}
    result = {"status": "pending_approval", "workflow_id": "wf-attacker" if corrupt == "workflow" else wid,
              "hitl_id": saved["id"], "kind": ACTION_KIND, "synthetic": True,
              "execution_status": "not_executed", "draft": draft}
    await database["hitl_queue"].create_item(saved)
    response = SimpleNamespace(status="completed", output=[
        SimpleNamespace(type="function_call", name="other" if corrupt == "call" else "draft_claim_action", call_id="c"),
        SimpleNamespace(type="function_call_output", call_id="c", output=json.dumps(result)),
    ])
    native = AsyncMock(return_value=response)
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    events = []
    tools = insurance_tools.InsuranceTools(config, run["sessionId"], wid, events.append, store.RunStore())
    await tools.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    outcome = await tools.handler("claim_action_draft")(invocation(draft))
    assert (outcome.result_type == "success") is (corrupt is None)
    assert any(e.kind == "approval_pending" and e.action_kind == ACTION_KIND
               and e.execution_status == "not_executed" for e in events) is (corrupt is None)
    args, kwargs = native.call_args
    assert args[0] == "northwind-insurance-claim-action-draft"
    assert kwargs["workflow_id"] == wid
    assert kwargs["mcp_tool"]["headers"] == {"x-orders-workflow": wid}
    assert kwargs["mcp_tool"]["allowed_tools"] == ["draft_claim_action"]
    again = await tools.handler("claim_action_draft")(invocation(draft))
    assert again.result_type == "failure" and native.await_count == 1
    snapshot = await store.RunStore().snapshot(run["sessionId"])
    assert snapshot["approval"]["kind"] == saved["kind"]
    assert snapshot["business_domain"] == "insurance"


@pytest.mark.asyncio
async def test_insurance_never_guesses_ids_from_names_and_obeys_tool_budget(config, monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    tools = insurance_tools.InsuranceTools(config, "harness-unit", "wf-harness-unit", lambda _: None,
                                          store.RunStore(), message="What is Avery's claim status?")
    outcome = await tools.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    assert outcome.result_type == "failure" and "exact claim or policy ID" in outcome.text_result_for_llm
    tools.calls = config.harness_max_tools
    outcome = await tools.handler("triage")(invocation({"prompt": "Classify"}))
    assert outcome.result_type == "failure" and "limit" in outcome.text_result_for_llm
    native.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_draft_attempts_cannot_invoke_twice(config):
    active = asyncio.Event()
    async def check(*args):
        await active.wait()
        raise HarnessError("workflow_inactive", "No active workflow.")
    saved = SimpleNamespace(require_active=check)
    tools = insurance_tools.InsuranceTools(config, "harness-unit", "wf-harness-unit", lambda _: None, saved)
    await tools.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    first = asyncio.create_task(tools.handler("claim_action_draft")(invocation(draft_args())))
    await asyncio.sleep(0)
    second = await tools.handler("claim_action_draft")(invocation(draft_args()))
    assert second.result_type == "failure" and "already attempted" in second.text_result_for_llm
    active.set()
    assert (await first).result_type == "failure"


@pytest.mark.asyncio
async def test_draft_needs_retrieved_claim_and_live_workflow(config, database, monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    saved, _ = await store.RunStore().create(RunRequest(request_id=uuid.uuid4(), message="hi"), "m", "s", -1)
    tools = insurance_tools.InsuranceTools(config, saved["sessionId"], saved["workflowId"], lambda _: None, store.RunStore())
    assert (await tools.handler("claim_action_draft")(invocation(draft_args()))).result_type == "failure"
    await tools.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    assert (await tools.handler("claim_action_draft")(invocation(draft_args()))).result_type == "failure"
    native.assert_not_awaited()


@pytest.mark.asyncio
async def test_insurance_cancellation_preserves_history_and_releases_capacity(config, database, monkeypatch):
    async def slow(*args):
        from app.harness.models import Text
        yield Text(kind="assistant_delta", text="working")
        await asyncio.sleep(60)
    monkeypatch.setattr(routes.runtime, "run", slow)
    response = await routes.run(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
                                RunRequest(request_id=uuid.uuid4(), message="Look up CLM-2001"))
    first = json.loads(await anext(response.body_iterator))
    await anext(response.body_iterator)
    with pytest.raises(asyncio.CancelledError):
        await response.body_iterator.athrow(asyncio.CancelledError())
    snapshot = await store.RunStore().snapshot(first["run_id"])
    assert snapshot["status"] == "cancelled" and snapshot["input"] == "Look up CLM-2001"
    assert snapshot["events"][1]["text"] == "working" and routes.runtime.active == 0


@pytest.mark.asyncio
async def test_sdk_failure_logs_only_safe_metadata(config, caplog):
    private = "PRIVATE_PROMPT_RESPONSE_AND_AUTH_TOKEN"
    error = JsonRpcError(-32603, private, data={
        "statusCode": 403, "headers": {"Authorization": private}, "response": private,
    })
    async def fail():
        raise error
    with caplog.at_level(logging.ERROR):
        with pytest.raises(JsonRpcError):
            await runtime.sdk_call("session_create", fail, "harness-private-person")
    record = next(record for record in caplog.records if record.name == runtime.__name__)
    assert record.stage == "session_create" and record.error_class == "JsonRpcError"
    assert record.http_status == 403 and record.rpc_code == -32603 and record.exc_info is None
    assert private not in caplog.text and "harness-private-person" not in caplog.text


@pytest.mark.asyncio
async def test_sdk_session_error_event_logs_status_without_payload(config, caplog):
    r = runtime.HarnessRuntime()
    private = "PRIVATE_PROMPT_RESPONSE_AND_AUTH_TOKEN"
    async def create(**kwargs):
        async def fail(*args, **opts):
            kwargs["on_event"](SimpleNamespace(type=SimpleNamespace(value="session.error"),
                data=SimpleNamespace(status_code=404, message=private, url=private, error_type=private)))
            raise RuntimeError(private)
        return SimpleNamespace(send_and_wait=fail, abort=AsyncMock(), disconnect=AsyncMock())
    r.client = SimpleNamespace(create_session=create, delete_session=AsyncMock())
    r.credential = SimpleNamespace(get_token=AsyncMock())
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            _ = [event async for event in r.run("harness-private", "wf-harness-private", private, store.RunStore())]
    records = [record for record in caplog.records if record.name == runtime.__name__]
    assert any(record.stage == "sdk_session_error_event" and record.http_status == 404 for record in records)
    assert any(record.stage == "session_send_wait" for record in records)
    assert private not in caplog.text and all(record.exc_info is None for record in records)


@pytest.mark.asyncio
async def test_stream_boundary_never_logs_provider_exception_text(config, database, monkeypatch, caplog):
    private = "PRIVATE_PROVIDER_RESPONSE_AUTH_AND_PROMPT"
    async def fail(*args):
        if False:
            yield
        raise RuntimeError(private)
    monkeypatch.setattr(routes.runtime, "run", fail)
    with caplog.at_level(logging.ERROR):
        response = await routes.run(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
                                   RunRequest(request_id=uuid.uuid4(), message=private))
        events = [json.loads(chunk) async for chunk in response.body_iterator]
    assert events[-1]["status"] == "failed"
    assert any(record.stage == "stream_run" and record.error_class == "RuntimeError"
               for record in caplog.records if record.name == routes.__name__)
    assert private not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == routes.__name__)
