"""Gateway routing preserves hosted tools, workflow guards and original deployment identity."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken

from agents.hosted.runtime import account_openai_client, build_agent, knowledge_connection_headers
from agents.hosted.scope import RequestScope, current_scope
from agents.hosted import tools
from agents.tests.test_hosted import (
    DRAFT, TRIAGE, MockProject, function, hosted_request, message, model_events, no_telemetry, settings,
)

GATEWAY = "https://gateway.example/default/models/openai/v1/"
ALIAS = "northwind/baseline"
GATEWAY_ENV = {
    "ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY,
    "ORDERS_MODEL_GATEWAY_MODEL": ALIAS,
    "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "baseline-deployment",
}


def gateway_settings(role="triage", **overrides):
    return settings(role, **{**GATEWAY_ENV, **overrides})


@pytest.mark.parametrize("role", ["manager", "triage", "orderlookup", "policylookup", "resolutiondraft"])
def test_gateway_model_alias_keeps_canonical_tools_and_original_deployment(role, monkeypatch):
    async def run():
        async def persist(url, workflow, draft):
            return {"status": "pending_approval", "workflow_id": workflow, "hitl_id": "unit-hitl", "draft": draft}
        monkeypatch.setattr(tools, "call_proposal_mcp", AsyncMock(side_effect=persist))
        turn = 0

        def inference(name, body):
            nonlocal turn
            turn += 1
            assert name is None
            assert body["model"] == ALIAS
            assert body["store"] is False
            if role == "triage":
                return model_events([message(json.dumps(TRIAGE))], model="provider-baseline")
            if role in ("orderlookup", "policylookup"):
                native = body["tools"][0]
                tool_name = "lookup_order" if role == "orderlookup" else "knowledge_base_retrieve"
                assert native["allowed_tools"] == [tool_name]
                if role == "policylookup":
                    assert "project_connection_id" not in native
                    assert native["headers"] == {"api-key": "unit-test-only"}
                return model_events([
                    {"id": "mcp_gateway", "type": "mcp_call", "name": tool_name,
                     "server_label": native["server_label"], "arguments": '{"queries":["shipping"]}',
                     "status": "completed", "output": '{"answer":"Seven days","references":[{"title":"Shipping"}]}'},
                    message("Seven days [Shipping]."),
                ], model="provider-baseline")
            output = [function("propose_resolution", {"draft": DRAFT})] if (
                role == "resolutiondraft" and turn == 1
            ) else [message("OK" if role == "manager" else "Awaiting human approval.")]
            return model_events(output, model="provider-baseline")

        project = MockProject(inference)
        config = gateway_settings(role)
        result = await hosted_request(config, project, {
            "stream": True, "metadata": {"workflow_id": "wf-owned"},
        })
        final = result[-1]["response"]
        assert final["status"] == "completed", final.get("error")
        assert final["model"] == "baseline-deployment"
        assert final["metadata"]["model_deployment"] == "baseline-deployment"
        assert final["metadata"]["model_gateway_model"] == ALIAS
        assert final["metadata"]["model_gateway_auth"] == "entra_id"
        assert final["metadata"]["model_endpoint_kind"].startswith("gateway_")
        expected_provider = "provider-" + ALIAS if role == "triage" else "provider-baseline"
        assert final["metadata"]["provider_model"] == expected_provider
        assert "unit-test-only" not in json.dumps(final["metadata"])
    asyncio.run(run())


def test_gateway_client_uses_bearer_scope_and_cannot_accept_a_caller_model_override():
    async def run():
        calls = []
        config = gateway_settings()

        def respond(request):
            calls.append(request)
            body = json.loads(request.content)
            assert str(request.url) == GATEWAY + "chat/completions"
            assert request.headers["authorization"] == "Bearer " + "unit-gateway-token"
            assert "api-key" not in request.headers
            assert body["model"] == ALIAS
            assert body["store"] is False
            chunk = {"id": "gateway_chat", "created": 1, "model": "provider-baseline",
                     "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": json.dumps(TRIAGE)}, "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        credential = SimpleNamespace(
            get_token=AsyncMock(return_value=AccessToken("unit-gateway-token", int(time.time()) + 3600)),
        )
        client = account_openai_client(
            config, credential, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        agent = build_agent(config, None, account_client=client)
        token = current_scope.set(RequestScope())
        try:
            updates = [u async for u in agent.run("Classify.", stream=True,
                                                  options={"model": "unapproved-direct-model"})]
            text = "".join(c.text or "" for u in updates for c in u.contents if c.type == "text")
            assert json.loads(text) == TRIAGE
            assert len(calls) == 1
            assert credential.get_token.call_args.args == ("https://azure-api.net/aigateway/.default",)
        finally:
            current_scope.reset(token)
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("overrides,match", [
    ({"ORDERS_MODEL_GATEWAY_MODEL": ""}, "MODEL_GATEWAY_MODEL"),
    ({"ORDERS_MODEL_GATEWAY_ENDPOINT": ""}, "MODEL_GATEWAY_ENDPOINT"),
    ({"ORDERS_MODEL_GATEWAY_ENDPOINT": "http://unsafe.example/openai/v1/"}, "HTTPS"),
    ({"ORDERS_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/"}, "openai/v1"),
    ({"ORDERS_MODEL_GATEWAY_DEPLOYMENT": "other-model"}, "must match"),
    ({"TRIAGE_MODEL_DEPLOYMENT": "fine-tuned-model"}, "triage override"),
])
def test_gateway_configuration_fails_closed_on_partial_or_wrong_model_binding(overrides, match):
    with pytest.raises(ValueError, match=match):
        gateway_settings(**overrides)


@pytest.mark.parametrize("role", ["manager", "triage", "orderlookup", "policylookup", "resolutiondraft"])
def test_gateway_roles_cannot_fall_back_to_project_inference(role):
    with pytest.raises(ValueError, match="gateway client"):
        build_agent(gateway_settings(role), None)


def test_policy_connection_credential_is_validated_and_not_added_to_settings():
    config = gateway_settings("policylookup")
    connection = {"type": "RemoteTool", "target": config.knowledge_spec()["url"],
                  "credentials": {"type": "CustomKeys", "api-key": "unit-search-credential"}}
    assert knowledge_connection_headers(config, connection) == {"api-key": "unit-search-credential"}
    assert "unit-search-credential" not in json.dumps(config.metadata)
    assert "unit-search-credential" not in json.dumps(dict(config.env))
    for changed in (
        {**connection, "type": "Other"},
        {**connection, "target": "https://wrong.example/mcp"},
        {**connection, "credentials": {"type": "CustomKeys", "api-key": ""}},
        {**connection, "credentials": None},
    ):
        with pytest.raises(ValueError):
            knowledge_connection_headers(config, changed)


def test_gateway_policy_cannot_run_without_its_connection_credential():
    project = MockProject(lambda *_: [])
    client = project.get_openai_client()
    try:
        with pytest.raises(ValueError, match="knowledge connection headers"):
            build_agent(gateway_settings("policylookup"), project, account_client=client)
    finally:
        asyncio.run(project.close())


def test_gateway_resolution_still_requires_workflow_authority_before_inference():
    async def run():
        project = MockProject(lambda *_: pytest.fail("No gateway inference without workflow authority"))
        result = await hosted_request(gateway_settings("resolutiondraft"), project, {
            "stream": False, "input": "[workflow_id: wf-forged] refund",
        })
        assert result["status"] == "failed"
        assert not project.requests
    asyncio.run(run())
