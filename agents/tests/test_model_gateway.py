"""Gateway routing preserves hosted tools, workflow guards and original deployment identity."""
import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken

from agents.hosted.runtime import account_openai_client, build_agent, knowledge_connection_headers
from agents.hosted.scope import RequestScope, current_scope
from agents.hosted import tools
from agents.tests.test_hosted import (  # noqa: F401  (no_telemetry is an autouse fixture)
    BASE, DRAFT, TRIAGE, VERIFIED_TRIAGE_MODEL, MockProject, function, hosted_request, message, model_events,
    no_telemetry, pending, settings,
)

GATEWAY = "https://gateway.example/default/models/openai/v1/"
ALIAS = "northwind/baseline"
GATEWAY_ENV = {
    "AGENT_MODEL_GATEWAY_ENDPOINT": GATEWAY,
    "AGENT_MODEL_GATEWAY_MODEL": ALIAS,
    "AGENT_MODEL_GATEWAY_DEPLOYMENT": "baseline-deployment",
}
ROLES = ["orchestrator", "triage", "policy-claim-lookup", "policy-wording", "claim-action-draft"]
REQUIRED = ["true", "TRUE", "True", "1", "yes", "YES", " yes "]
NOT_REQUIRED = [None, "", "  ", "false", "FALSE", "0", "no", "off"]


def gateway_settings(role="triage", **overrides):
    return settings(role, **{**GATEWAY_ENV, **overrides})


@pytest.mark.parametrize("role", ROLES)
def test_gateway_model_alias_keeps_canonical_tools_and_original_deployment(role, monkeypatch):
    async def run():
        async def persist(url, workflow, draft, tool_name):
            return pending(workflow, draft, hitl_id="unit-hitl")
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
            if role in ("policy-claim-lookup", "policy-wording"):
                native = body["tools"][0]
                tool_name = "lookup_claim" if role == "policy-claim-lookup" else "knowledge_base_retrieve"
                assert native["allowed_tools"] == (
                    ["lookup_policy", "lookup_claim"] if role == "policy-claim-lookup" else [tool_name])
                if role == "policy-wording":
                    assert "project_connection_id" not in native
                    assert native["headers"] == {"api-key": "unit-test-only"}
                return model_events([
                    {"id": "mcp_gateway", "type": "mcp_call", "name": tool_name,
                     "server_label": native["server_label"], "arguments": '{"queries":["collision"]}',
                     "status": "completed", "output": '{"answer":"Considered","references":[{"title":"Wording"}]}'},
                    message("Collision may be considered [Wording]."),
                ], model="provider-baseline")
            output = [function("draft_claim_action", {"draft": DRAFT})] if (
                role == "claim-action-draft" and turn == 1
            ) else [message("OK" if role == "orchestrator" else "Awaiting human review.")]
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
    ({"AGENT_MODEL_GATEWAY_MODEL": ""}, "MODEL_GATEWAY_MODEL"),
    ({"AGENT_MODEL_GATEWAY_ENDPOINT": ""}, "MODEL_GATEWAY_ENDPOINT"),
    ({"AGENT_MODEL_GATEWAY_ENDPOINT": "http://unsafe.example/openai/v1/"}, "HTTPS"),
    ({"AGENT_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/"}, "openai/v1"),
    ({"AGENT_MODEL_GATEWAY_DEPLOYMENT": "other-model"}, "must match"),
    ({"TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1", **VERIFIED_TRIAGE_MODEL}, "triage override"),
])
def test_gateway_configuration_fails_closed_on_partial_or_wrong_model_binding(overrides, match):
    with pytest.raises(ValueError, match=match):
        gateway_settings(**overrides)


@pytest.mark.parametrize("role", ROLES)
def test_gateway_roles_cannot_fall_back_to_project_inference(role):
    with pytest.raises(ValueError, match="gateway client"):
        build_agent(gateway_settings(role), None)


def test_policy_connection_credential_is_validated_and_not_added_to_settings():
    config = gateway_settings("policy-wording")
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
            build_agent(gateway_settings("policy-wording"), project, account_client=client)
    finally:
        asyncio.run(project.close())


def test_gateway_claim_action_still_requires_workflow_authority_before_inference():
    async def run():
        project = MockProject(lambda *_: pytest.fail("No gateway inference without workflow authority"))
        result = await hosted_request(gateway_settings("claim-action-draft"), project, {
            "stream": False, "input": "[workflow_id: wf-forged] settle it",
        })
        assert result["status"] == "failed"
        assert not project.requests
    asyncio.run(run())


def required_without_gateway(role):
    """from_env refuses this combination; replace() builds it to test the runtime guards on their own."""
    config = settings(role)
    return replace(config, env={**config.env, "AI_GATEWAY_REQUIRED": "true"})


@pytest.mark.parametrize("value", REQUIRED)
@pytest.mark.parametrize("role", ROLES)
def test_required_gateway_demands_the_complete_model_binding_for_every_role(role, value):
    for missing in ("AGENT_MODEL_GATEWAY_ENDPOINT", "AGENT_MODEL_GATEWAY_MODEL", "AGENT_MODEL_GATEWAY_DEPLOYMENT"):
        for blank in ("", "   "):
            with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
                gateway_settings(role, AI_GATEWAY_REQUIRED=value, **{missing: blank})
    # The direct project/account paths are configured in BASE; the flag must still refuse them.
    with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
        settings(role, AI_GATEWAY_REQUIRED=value)
    config = gateway_settings(role, AI_GATEWAY_REQUIRED=value)
    assert config.model_gateway_required and config.uses_model_gateway
    assert config.metadata["model_gateway_required"] == "true"


@pytest.mark.parametrize("overrides,match", [
    ({"AGENT_MODEL_GATEWAY_ENDPOINT": "http://unsafe.example/openai/v1/"}, "HTTPS"),
    ({"AGENT_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/"}, "openai/v1"),
    ({"AGENT_MODEL_GATEWAY_DEPLOYMENT": "other-model"}, "must match"),
    ({"TRIAGE_MODEL_DEPLOYMENT": "northwind-insurance-triage-ft-v1", **VERIFIED_TRIAGE_MODEL}, "triage override"),
])
def test_required_gateway_keeps_the_existing_binding_validation(overrides, match):
    with pytest.raises(ValueError, match=match):
        gateway_settings(AI_GATEWAY_REQUIRED="true", **overrides)


@pytest.mark.parametrize("value", NOT_REQUIRED)
@pytest.mark.parametrize("role", ROLES)
def test_flag_off_keeps_the_original_direct_inference_behaviour(role, value):
    env = {} if value is None else {"AI_GATEWAY_REQUIRED": value}
    config = settings(role, **env)
    assert not config.model_gateway_required and not config.uses_model_gateway
    assert "model_gateway_required" not in config.metadata
    assert config.metadata["model_endpoint_kind"] == (
        "account_chat_completions" if role == "triage" else "project_responses")
    assert "model_gateway_required" not in gateway_settings(role, **env).metadata


def test_flag_off_still_builds_the_direct_triage_client_and_the_project_orchestrator():
    client = account_openai_client(settings("triage", AI_GATEWAY_REQUIRED="false"), None)
    project = MockProject(lambda *_: [])
    try:
        assert str(client.base_url) == BASE["AGENT_OPENAI_ENDPOINT"]
        assert build_agent(settings("orchestrator", AI_GATEWAY_REQUIRED="no"), project)
        assert build_agent(settings("triage"), project, account_client=client)
    finally:
        asyncio.run(client.close())
        asyncio.run(project.close())


def test_the_requirement_is_recorded_in_metadata_only_when_set():
    assert "model_gateway_required" not in gateway_settings("orchestrator").metadata
    assert "model_gateway_required" not in gateway_settings("orchestrator", AI_GATEWAY_REQUIRED="false").metadata
    assert gateway_settings("orchestrator", AI_GATEWAY_REQUIRED="1").metadata["model_gateway_required"] == "true"


@pytest.mark.parametrize("role", ROLES)
def test_required_runtime_refuses_every_direct_inference_path(role):
    config = required_without_gateway(role)
    project = MockProject(lambda *_: [])
    try:
        with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
            build_agent(config, project)
        with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
            build_agent(config, project, account_client=project.get_openai_client(account=True))
    finally:
        asyncio.run(project.close())


def test_required_triage_never_falls_back_to_the_direct_account_endpoint():
    config = required_without_gateway("triage")
    assert config.env["AGENT_OPENAI_ENDPOINT"] == BASE["AGENT_OPENAI_ENDPOINT"]
    with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
        account_openai_client(config, None)
    with pytest.raises(ValueError, match="AI_GATEWAY_REQUIRED"):
        settings("triage", AI_GATEWAY_REQUIRED="true")


def test_required_gateway_ignores_the_direct_account_endpoint_and_uses_the_gateway_scope():
    async def run():
        calls = []

        def respond(request):
            calls.append(request)
            assert str(request.url) == GATEWAY + "chat/completions"
            assert request.headers["authorization"] == "Bearer " + "unit-gateway-token"
            chunk = {"id": "gateway_chat", "created": 1, "model": "provider-baseline",
                     "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": json.dumps(TRIAGE)}, "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        config = gateway_settings(AI_GATEWAY_REQUIRED="true")
        assert config.env["AGENT_OPENAI_ENDPOINT"] == BASE["AGENT_OPENAI_ENDPOINT"]
        credential = SimpleNamespace(
            get_token=AsyncMock(return_value=AccessToken("unit-gateway-token", int(time.time()) + 3600)),
        )
        client = account_openai_client(
            config, credential, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        agent = build_agent(config, None, account_client=client)
        token = current_scope.set(RequestScope())
        try:
            updates = [u async for u in agent.run("Classify.", stream=True)]
            assert json.loads("".join(c.text or "" for u in updates for c in u.contents if c.type == "text")) == TRIAGE
            assert len(calls) == 1
            assert credential.get_token.call_args.args == ("https://azure-api.net/aigateway/.default",)
        finally:
            current_scope.reset(token)
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("role", ["orchestrator", "triage"])
def test_required_hosted_request_reports_the_requirement_and_the_gateway_route(role):
    async def run():
        def inference(name, body):
            assert body["model"] == ALIAS
            output = [message(json.dumps(TRIAGE) if role == "triage" else "OK")]
            return model_events(output, model="provider-baseline")

        result = await hosted_request(gateway_settings(role, AI_GATEWAY_REQUIRED="true"), MockProject(inference), {
            "stream": False, "metadata": {"workflow_id": "wf-owned"},
        })
        assert result["status"] == "completed", result.get("error")
        assert result["metadata"]["model_gateway_required"] == "true"
        assert result["metadata"]["model_endpoint_kind"].startswith("gateway_")
        assert result["metadata"]["model_gateway_model"] == ALIAS
    asyncio.run(run())


DEPRECATED_GATEWAY_ENV = {
    "ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY,
    "ORDERS_MODEL_GATEWAY_MODEL": ALIAS,
    "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "baseline-deployment",
    "ORDERS_MODEL_GATEWAY_SCOPE": "api://gateway/.default",
}


@pytest.mark.parametrize("role", ROLES)
def test_the_deprecated_gateway_setting_names_route_exactly_like_the_canonical_ones(role):
    old = settings(role, **DEPRECATED_GATEWAY_ENV)
    new = gateway_settings(role, AGENT_MODEL_GATEWAY_SCOPE="api://gateway/.default")
    assert old.uses_model_gateway and old.inference_model == ALIAS
    assert old.gateway_scope == new.gateway_scope == "api://gateway/.default"
    assert old.metadata == new.metadata
    # The deprecated names also satisfy the requirement and its validation, and the canonical ones win.
    assert settings(role, AI_GATEWAY_REQUIRED="true", **DEPRECATED_GATEWAY_ENV).model_gateway_required
    mixed = settings(role, **DEPRECATED_GATEWAY_ENV, AGENT_MODEL_GATEWAY_MODEL="northwind/other")
    assert mixed.inference_model == "northwind/other"
    with pytest.raises(ValueError, match="must match"):
        settings(role, **{**DEPRECATED_GATEWAY_ENV, "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "other-model"})
    with pytest.raises(ValueError, match="AGENT_MODEL_GATEWAY_ENDPOINT is required"):
        settings(role, ORDERS_MODEL_GATEWAY_MODEL=ALIAS)
