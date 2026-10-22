"""Gateway routing preserves hosted tools, workflow guards and original deployment identity."""
import asyncio
import importlib.util
import json
import sys
import time
import types
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken

from agents.hosted.config import AGENTS_DIR
from agents.hosted.runtime import account_openai_client, build_agent, knowledge_connection_headers
from agents.hosted.scope import RequestScope, current_scope
from agents.hosted import tools
from agents.tests.test_hosted import (
    BASE, DRAFT, TRIAGE, MockProject, function, hosted_request, message, model_events, no_telemetry, settings,
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


ROLES = ["manager", "triage", "orderlookup", "policylookup", "resolutiondraft"]
REQUIRED = ["true", "TRUE", "True", "1", "yes", "YES", " yes "]
NOT_REQUIRED = [None, "", "  ", "false", "FALSE", "0", "no", "off"]


def required_without_gateway(role):
    """from_env refuses this combination; replace() builds it to test the runtime guards on their own."""
    config = settings(role)
    return replace(config, env={**config.env, "AI_GATEWAY_REQUIRED": "true"})


@pytest.mark.parametrize("value", REQUIRED)
@pytest.mark.parametrize("role", ROLES)
def test_required_gateway_demands_the_complete_model_binding_for_every_role(role, value):
    for missing in ("ORDERS_MODEL_GATEWAY_ENDPOINT", "ORDERS_MODEL_GATEWAY_MODEL", "ORDERS_MODEL_GATEWAY_DEPLOYMENT"):
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
    ({"ORDERS_MODEL_GATEWAY_ENDPOINT": "http://unsafe.example/openai/v1/"}, "HTTPS"),
    ({"ORDERS_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/"}, "openai/v1"),
    ({"ORDERS_MODEL_GATEWAY_DEPLOYMENT": "other-model"}, "must match"),
    ({"TRIAGE_MODEL_DEPLOYMENT": "fine-tuned-model"}, "triage override"),
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


def test_flag_off_still_builds_the_direct_triage_client_and_the_project_manager():
    client = account_openai_client(settings("triage", AI_GATEWAY_REQUIRED="false"), None)
    project = MockProject(lambda *_: [])
    try:
        assert str(client.base_url) == BASE["ORDERS_OPENAI_ENDPOINT"]
        assert build_agent(settings("manager", AI_GATEWAY_REQUIRED="no"), project)
        assert build_agent(settings("triage"), project, account_client=client)
    finally:
        asyncio.run(client.close())
        asyncio.run(project.close())


def test_the_requirement_is_recorded_in_metadata_only_when_set():
    assert "model_gateway_required" not in gateway_settings("manager").metadata
    assert "model_gateway_required" not in gateway_settings("manager", AI_GATEWAY_REQUIRED="false").metadata
    assert gateway_settings("manager", AI_GATEWAY_REQUIRED="1").metadata["model_gateway_required"] == "true"


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
    assert config.env["ORDERS_OPENAI_ENDPOINT"] == BASE["ORDERS_OPENAI_ENDPOINT"]
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
        assert config.env["ORDERS_OPENAI_ENDPOINT"] == BASE["ORDERS_OPENAI_ENDPOINT"]
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


@pytest.mark.parametrize("role", ["manager", "triage"])
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


LEGACY_ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://legacy.services.ai.azure.com/api/projects/northwind",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "legacy-manager",
}
LEGACY_GATEWAY = {
    "ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY,
    "ORDERS_MODEL_GATEWAY_MODEL": ALIAS,
    "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "legacy-manager",
}


@pytest.fixture
def legacy(monkeypatch):
    """Load the legacy manager module afresh with a hermetic environment and no .env file."""
    loaded = []

    def load(**env):
        for name in ("ORDERS_MODEL_GATEWAY_ENDPOINT", "ORDERS_MODEL_GATEWAY_MODEL", "ORDERS_MODEL_GATEWAY_DEPLOYMENT",
                     "AI_GATEWAY_REQUIRED", "AZURE_SEARCH_ENDPOINT"):
            monkeypatch.delenv(name, raising=False)
        for name, value in {**LEGACY_ENV, **env}.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *_, **__: False))
        spec = importlib.util.spec_from_file_location(
            "legacy_orchestrator_under_test", AGENTS_DIR / "legacyorchestrator" / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
        return module

    yield load
    for module in loaded:
        asyncio.run(module._search_credential.close())
        module._credential.close()


class RecordingClient:
    def __init__(self, label):
        self.label = label

    def __call__(self, **options):
        self.options = options
        return SimpleNamespace(label=self.label, options=options)


def build_legacy(module, monkeypatch):
    gateway, foundry = RecordingClient("gateway"), RecordingClient("foundry")
    monkeypatch.setattr(module, "OpenAIChatClient", gateway)
    monkeypatch.setattr(module, "FoundryChatClient", foundry)
    monkeypatch.setattr(module, "Agent", lambda **options: SimpleNamespace(**options))
    return module._build_agent().client, gateway, foundry


@pytest.mark.parametrize("env", [
    {},
    {"ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY},
    {"ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY, "ORDERS_MODEL_GATEWAY_MODEL": ALIAS},
    {"ORDERS_MODEL_GATEWAY_MODEL": ALIAS, "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "legacy-manager"},
    {**LEGACY_GATEWAY, "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "  "},
    {**LEGACY_GATEWAY, "ORDERS_MODEL_GATEWAY_ENDPOINT": "  "},
])
def test_legacy_manager_fails_closed_at_import_without_the_complete_gateway_binding(legacy, env):
    with pytest.raises(RuntimeError, match="AI_GATEWAY_REQUIRED"):
        legacy(AI_GATEWAY_REQUIRED="true", **env)


def test_legacy_manager_keeps_the_existing_binding_validation_when_required(legacy):
    with pytest.raises(RuntimeError, match="Gateway alias must bind"):
        legacy(AI_GATEWAY_REQUIRED="yes", **{**LEGACY_GATEWAY, "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "another-model"})
    with pytest.raises(RuntimeError, match="credential-free HTTPS"):
        legacy(AI_GATEWAY_REQUIRED="1", **{**LEGACY_GATEWAY, "ORDERS_MODEL_GATEWAY_ENDPOINT": "http://gateway.example/openai/v1/"})


@pytest.mark.parametrize("value", ["maybe", "enabled", "on"])
def test_legacy_manager_rejects_a_mistyped_requirement_flag(legacy, value):
    with pytest.raises(RuntimeError, match="AI_GATEWAY_REQUIRED"):
        legacy(AI_GATEWAY_REQUIRED=value, **LEGACY_GATEWAY)


@pytest.mark.parametrize("value", REQUIRED)
def test_required_legacy_manager_only_ever_builds_the_gateway_client(legacy, monkeypatch, value):
    module = legacy(AI_GATEWAY_REQUIRED=value, **LEGACY_GATEWAY)
    client, gateway, foundry = build_legacy(module, monkeypatch)
    assert client.label == "gateway" and not hasattr(foundry, "options")
    assert gateway.options["base_url"] == GATEWAY and gateway.options["model"] == ALIAS


def test_legacy_build_refuses_a_direct_client_even_if_the_binding_is_lost_after_import(legacy, monkeypatch):
    module = legacy(AI_GATEWAY_REQUIRED="true", **LEGACY_GATEWAY)
    monkeypatch.setattr(module, "MODEL_GATEWAY_ENDPOINT", "")
    monkeypatch.setattr(module, "OpenAIChatClient", lambda **_: pytest.fail("No gateway client without a gateway"))
    monkeypatch.setattr(module, "FoundryChatClient", lambda **_: pytest.fail("Direct Foundry inference is forbidden"))
    with pytest.raises(RuntimeError, match="AI_GATEWAY_REQUIRED"):
        module._build_agent()


@pytest.mark.parametrize("value", NOT_REQUIRED)
def test_legacy_manager_flag_off_keeps_the_original_direct_foundry_client(legacy, monkeypatch, value):
    env = {} if value is None else {"AI_GATEWAY_REQUIRED": value}
    module = legacy(**env)
    client, gateway, foundry = build_legacy(module, monkeypatch)
    assert client.label == "foundry" and not hasattr(gateway, "options")
    assert foundry.options["model"] == "legacy-manager"
    assert foundry.options["project_endpoint"] == LEGACY_ENV["FOUNDRY_PROJECT_ENDPOINT"]


def test_legacy_manager_flag_off_keeps_the_optional_gateway(legacy, monkeypatch):
    module = legacy(AI_GATEWAY_REQUIRED="false", **LEGACY_GATEWAY)
    client, gateway, foundry = build_legacy(module, monkeypatch)
    assert client.label == "gateway" and not hasattr(foundry, "options")
    assert gateway.options["model"] == ALIAS
