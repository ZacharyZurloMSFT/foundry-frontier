"""Opt-in API gateway authentication for hosted MCP callers (real framework, mocked inference and MCP)."""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from functools import partial
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import httpx
import pytest

from agents.hosted import main as hosted_main
from agents.hosted import tools
from agents.hosted.config import ENV_KEYS, Settings
from agents.hosted.runtime import GATEWAY_KEY_HEADER, build_agent, mcp_gateway_headers
from agents.hosted.scope import RequestScope, current_scope
from agents.tests import test_hosted as hosted_tests
from agents.tests.test_hosted import (  # noqa: F401  (no_telemetry is an autouse fixture)
    BASE, DRAFT, MockProject, function, hosted_request, message, model_events, no_telemetry, pending, settings,
)
from agents.tests.test_model_gateway import GATEWAY_ENV

HEADER = "Ocp-Apim-Subscription-Key"
KEY = "unit-apim-subscription-key-0123456789"
CONNECTION = "northwind-apim-gateway"
GATEWAY_ORIGIN = "https://apim-northwind.azure-api.net"
# APIM exposes the MCP servers without the backend's trailing /mcp.
LOOKUP_URL = GATEWAY_ORIGIN + "/insurance/mcp/tech-lookup"
REVIEW_URL = GATEWAY_ORIGIN + "/insurance/mcp/work-order"
GATED_ROLES = ["policy-claim-lookup", "claim-action-draft"]


def gated(role, **overrides):
    return settings(role, MCP_POLICY_LOOKUP_URL=LOOKUP_URL, MCP_CLAIM_REVIEW_URL=REVIEW_URL,
                    MCP_GATEWAY_CONNECTION=CONNECTION, **overrides)


def connection(config, **overrides):
    return {"type": "RemoteTool", "target": config.mcp_spec()["server_url"],
            "credentials": {"type": "CustomKeys", HEADER: KEY}, **overrides}


def credentials(**keys):
    return {"credentials": {"type": "CustomKeys", **keys}}


def no_leak(*values):
    assert all(KEY not in str(value) for value in values)


def test_header_name_is_the_apim_subscription_key_header():
    assert GATEWAY_KEY_HEADER == HEADER


@pytest.mark.parametrize("role", GATED_ROLES)
def test_connection_credential_is_validated_against_the_mcp_endpoint(role):
    config = gated(role)
    assert mcp_gateway_headers(config, connection(config)) == {HEADER: KEY}
    # Foundry may return custom key names lower-cased; the canonical header is always what is sent.
    assert mcp_gateway_headers(config, connection(config, **credentials(**{HEADER.lower(): KEY}))) == {HEADER: KEY}
    no_leak(config, config.metadata, dict(config.env), repr(config))


@pytest.mark.parametrize("change,match", [
    ({"type": "CustomKeys"}, "RemoteTool connection"),
    ({"type": None}, "RemoteTool connection"),
    ({"target": "https://other.example/mcp"}, "MCP endpoint or its gateway base URL"),
    ({"target": None}, "MCP endpoint or its gateway base URL"),
    ({"target": 42}, "MCP endpoint or its gateway base URL"),
    ({"credentials": None}, "CustomKeys"),
    ({"credentials": "api-key"}, "CustomKeys"),
    ({"credentials": {"type": "ApiKey", "key": KEY}}, "CustomKeys"),
    (credentials(), "did not supply"),
    (credentials(**{HEADER: ""}), "did not supply"),
    (credentials(**{HEADER: None}), "did not supply"),
    (credentials(**{HEADER: 12345}), "did not supply"),
    (credentials(**{"api-key": KEY}), "did not supply"),
    (credentials(**{HEADER: KEY, HEADER.lower(): KEY}), "did not supply"),
    (credentials(**{HEADER: "   "}), "invalid"),
    (credentials(**{HEADER: KEY + " "}), "invalid"),
    (credentials(**{HEADER: KEY + "\r\nx-injected: 1"}), "invalid"),
    (credentials(**{HEADER: "\u00e9" * 8}), "invalid"),
    (credentials(**{HEADER: "k" * 513}), "invalid"),
])
def test_connection_credential_fails_closed_without_echoing_secrets(change, match):
    config = gated("policy-claim-lookup")
    with pytest.raises(ValueError, match=match) as failure:
        mcp_gateway_headers(config, connection(config, **change))
    no_leak(failure.value, repr(failure.value), "".join(traceback.format_exception(failure.value)))


@pytest.mark.parametrize("role", GATED_ROLES)
def test_connection_may_target_exactly_the_role_endpoint(role):
    config = gated(role)
    url = config.mcp_spec()["server_url"]
    for target in (url, url + "/"):
        assert mcp_gateway_headers(config, connection(config, target=target)) == {HEADER: KEY}


@pytest.mark.parametrize("target", [
    GATEWAY_ORIGIN,
    GATEWAY_ORIGIN + "/",
    GATEWAY_ORIGIN + "/insurance",
    GATEWAY_ORIGIN + "/insurance/mcp/",
    "https://APIM-Northwind.azure-api.net/insurance/mcp",
])
def test_one_shared_gateway_connection_serves_both_mcp_servers(target):
    # One connection name is handed to both gated agents, so its target may be a common ancestor.
    for role in GATED_ROLES:
        config = gated(role)
        assert mcp_gateway_headers(config, connection(config, target=target)) == {HEADER: KEY}


@pytest.mark.parametrize("target", [
    GATEWAY_ORIGIN + "/other",
    REVIEW_URL,
    LOOKUP_URL + "/extra",
    GATEWAY_ORIGIN + "/insurance/mcp/tech",
    "https://apim-northwind.azure-api.net:8443/insurance/mcp/tech-lookup",
    "http://apim-northwind.azure-api.net/insurance/mcp/tech-lookup",
    "https://apim-northwind.azure-api.net.evil.example/insurance/mcp/tech-lookup",
    "https://evil.azure-api.net/insurance/mcp/tech-lookup",
    "https://user:pw@apim-northwind.azure-api.net/insurance/mcp/tech-lookup",
    GATEWAY_ORIGIN + "?x=1",
    GATEWAY_ORIGIN + "#fragment",
    "https://[bad",
    "",
])
def test_connection_target_must_not_reach_other_servers_or_paths(target):
    config = gated("policy-claim-lookup")
    with pytest.raises(ValueError, match="MCP endpoint or its gateway base URL"):
        mcp_gateway_headers(config, connection(config, target=target))


def test_connection_must_belong_to_the_role_own_mcp_server():
    lookup, review = gated("policy-claim-lookup"), gated("claim-action-draft")
    with pytest.raises(ValueError, match="MCP endpoint or its gateway base URL"):
        mcp_gateway_headers(lookup, connection(review))
    with pytest.raises(ValueError, match="MCP endpoint or its gateway base URL"):
        mcp_gateway_headers(review, connection(lookup))
    with pytest.raises(ValueError, match="MCP endpoint or its gateway base URL"):
        mcp_gateway_headers(review, connection(review, target=GATEWAY_ORIGIN + "/other"))


@pytest.mark.parametrize("model_gateway", [False, True])
def test_policy_claim_lookup_native_mcp_tool_carries_the_gateway_header(model_gateway):
    project = MockProject(lambda *_: [])
    config = gated("policy-claim-lookup", **(GATEWAY_ENV if model_gateway else {}))
    client = project.get_openai_client() if model_gateway else None
    try:
        agent = build_agent(config, project, account_client=client, gateway_headers={HEADER: KEY})
        native = agent.default_options["tools"][0]
        assert native["server_url"] == LOOKUP_URL
        assert native["headers"] == {HEADER: KEY}
        assert native["allowed_tools"] == ["lookup_policy", "lookup_claim"]
        assert native["require_approval"] == "never"
        assert "project_connection_id" not in native
        no_leak(agent.additional_properties, config.metadata)
    finally:
        asyncio.run(project.close())


@pytest.mark.parametrize("role", GATED_ROLES)
def test_configured_gateway_never_runs_without_its_credential(role):
    project = MockProject(lambda *_: [])
    try:
        for missing in (None, {}):
            with pytest.raises(ValueError, match="requires gateway connection headers"):
                build_agent(gated(role), project, gateway_headers=missing)
    finally:
        asyncio.run(project.close())


@pytest.mark.parametrize("role", GATED_ROLES)
def test_nothing_changes_while_the_gateway_is_not_configured(role, monkeypatch):
    project = MockProject(lambda *_: [])
    # Headers handed to the builder are inert unless MCP_GATEWAY_CONNECTION selects them.
    agent = build_agent(settings(role), project, gateway_headers={HEADER: KEY})
    native = agent.default_options["tools"][0]
    if role == "policy-claim-lookup":
        assert "headers" not in native and native["server_url"] == BASE["MCP_POLICY_LOOKUP_URL"]
    else:
        mcp = AsyncMock(side_effect=lambda url, workflow, draft, tool_name, **extra: pending(workflow, draft))
        monkeypatch.setattr(tools, "call_proposal_mcp", mcp)

        async def run():
            token = current_scope.set(RequestScope(workflow_id="wf-owned"))
            try:
                await native.invoke(arguments={"draft": DRAFT})
            finally:
                current_scope.reset(token)
        asyncio.run(run())
        mcp.assert_awaited_once_with(BASE["MCP_CLAIM_REVIEW_URL"], "wf-owned", DRAFT, "draft_claim_action")
    asyncio.run(project.close())


@pytest.mark.parametrize("stream", [False, True])
def test_policy_claim_lookup_request_reaches_the_model_service_with_the_gateway_header(monkeypatch, stream):
    async def run():
        project = MockProject(lambda *_: model_events([
            {"id": "mcp_gateway", "type": "mcp_call", "name": "lookup_claim",
             "server_label": "northwind-insurance-policy-claim-lookup",
             "arguments": '{"claim_id":"CLM-2001"}', "status": "completed", "output": '{"claim_id":"CLM-2001"}'},
            message("Claim CLM-2001 is open."),
        ]))
        monkeypatch.setattr(hosted_tests, "build_agent", partial(build_agent, gateway_headers={HEADER: KEY}))
        result = await hosted_request(gated("policy-claim-lookup"), project, {"stream": stream})
        final = result[-1]["response"] if stream else result
        assert final["status"] == "completed", final.get("error")
        native = project.requests[0][1]["tools"][0]
        assert native["server_url"] == LOOKUP_URL
        assert native["headers"] == {HEADER: KEY}
        assert native["allowed_tools"] == ["lookup_policy", "lookup_claim"]
        no_leak(json.dumps(result))
    asyncio.run(run())


def test_claim_action_is_persisted_through_the_gateway_without_leaking_the_key(monkeypatch):
    async def run():
        async def persisted(url, workflow, draft, tool_name, gateway_headers=None):
            assert url == REVIEW_URL and tool_name == "draft_claim_action"
            return pending(workflow, draft)
        mcp = AsyncMock(side_effect=persisted)
        monkeypatch.setattr(tools, "call_proposal_mcp", mcp)
        monkeypatch.setattr(hosted_tests, "build_agent", partial(build_agent, gateway_headers={HEADER: KEY}))
        count = 0

        def infer(name, body):
            nonlocal count
            count += 1
            return model_events([function("draft_claim_action", {"draft": DRAFT})] if count == 1
                                else [message("The draft is awaiting human review.")])
        events = await hosted_request(gated("claim-action-draft"), MockProject(infer), {
            "stream": True, "metadata": {"workflow_id": "wf-owned"},
        })
        assert events[-1]["type"] == "response.completed", events[-1]
        mcp.assert_awaited_once()
        assert mcp.await_args.kwargs == {"gateway_headers": {HEADER: KEY}}
        assert mcp.await_args.args[:2] == (REVIEW_URL, "wf-owned")
        no_leak(json.dumps(events))
    asyncio.run(run())


def mcp_server(seen):
    """Minimal stateless streamable-HTTP MCP server that records every request it receives."""
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "unit-mcp", "version": "1"}}})
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        if body.get("method") == "tools/call":
            arguments = body["params"]["arguments"]
            approval = pending(arguments["workflow_id"], arguments["draft"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": json.dumps(approval)}],
                "structuredContent": approval, "isError": False}})
        return httpx.Response(202)
    return handle


def use_mcp_server(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **options: real(transport=httpx.MockTransport(handler), **options))


@pytest.mark.asyncio
async def test_gateway_header_is_sent_on_every_mcp_request_beside_the_workflow_binding(monkeypatch):
    seen = []
    use_mcp_server(monkeypatch, mcp_server(seen))
    # A caller-supplied header set can never displace the application-owned workflow binding.
    result = await asyncio.wait_for(tools.call_proposal_mcp(
        REVIEW_URL, "wf-owned", DRAFT,
        gateway_headers={HEADER: KEY, "x-workflow-id": "wf-attacker", "x-orders-workflow": "wf-attacker"}), 30)
    assert result["hitl_id"] == "hitl-real"
    methods = [json.loads(r.content).get("method") for r in seen if r.method == "POST"]
    assert methods[0] == "initialize" and "tools/call" in methods
    for request in seen:
        assert str(request.url) == REVIEW_URL
        assert request.headers[HEADER] == KEY
        assert request.headers["x-workflow-id"] == "wf-owned"
        assert request.headers["x-orders-workflow"] == "wf-owned"
        assert "x-harness-workflow" not in request.headers


@pytest.mark.asyncio
async def test_no_gateway_header_is_sent_unless_configured(monkeypatch):
    seen = []
    use_mcp_server(monkeypatch, mcp_server(seen))
    await asyncio.wait_for(tools.call_proposal_mcp(BASE["MCP_CLAIM_REVIEW_URL"], "wf-owned", DRAFT), 30)
    assert seen and all(HEADER not in request.headers for request in seen)
    assert all(request.headers["x-workflow-id"] == "wf-owned" for request in seen)
    assert all(request.headers["x-orders-workflow"] == "wf-owned" for request in seen)


@pytest.mark.asyncio
async def test_gateway_header_is_never_sent_over_plain_http(monkeypatch):
    seen = []
    use_mcp_server(monkeypatch, mcp_server(seen))
    with pytest.raises(ValueError, match="HTTPS") as failure:
        await tools.call_proposal_mcp("http://apim-northwind.azure-api.net/insurance/mcp/work-order",
                                      "wf-owned", DRAFT, gateway_headers={HEADER: KEY})
    assert not seen
    no_leak(failure.value)


@pytest.mark.asyncio
async def test_rejected_key_does_not_leak_through_errors_or_logs(monkeypatch, caplog):
    seen = []

    def rejected(request):
        seen.append(request)
        return httpx.Response(401, json={"statusCode": 401, "message": "Access denied due to invalid subscription key."})
    use_mcp_server(monkeypatch, rejected)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            await asyncio.wait_for(tools.call_proposal_mcp(
                REVIEW_URL, "wf-owned", DRAFT, gateway_headers={HEADER: KEY}), 30)
    assert seen and seen[0].headers[HEADER] == KEY
    assert not isinstance(failure.value, TimeoutError)
    no_leak(failure.value, repr(failure.value), "".join(traceback.format_exception(failure.value)), caplog.text)


@pytest.fixture
def azure_fakes(monkeypatch):
    seen = {"credentials": [], "projects": [], "gets": [], "connection": None, "error": None}

    class Credential:
        def __init__(self, **options):
            seen["credentials"].append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Project(Credential):
        def __init__(self, **options):
            seen["projects"].append(options)
            self.connections = self

        def get(self, name, include_credentials=False):
            seen["gets"].append((name, include_credentials))
            if seen["error"]:
                raise seen["error"]
            return SimpleNamespace(as_dict=lambda: seen["connection"])

    monkeypatch.setattr(hosted_main, "ConnectionCredential", Credential)
    monkeypatch.setattr(hosted_main, "ConnectionProjectClient", Project)
    return seen


@pytest.mark.parametrize("role", GATED_ROLES)
def test_credential_is_loaded_once_at_startup_under_the_agent_identity(azure_fakes, caplog, role):
    config = gated(role, AZURE_CLIENT_ID="11111111-1111-1111-1111-111111111111")
    azure_fakes["connection"] = connection(config)
    with caplog.at_level(logging.DEBUG):
        assert hosted_main.load_gateway_headers(config) == {HEADER: KEY}
    assert azure_fakes["credentials"] == [{
        "managed_identity_client_id": "11111111-1111-1111-1111-111111111111",
        "exclude_interactive_browser_credential": True}]
    assert azure_fakes["projects"] == [{
        "endpoint": BASE["FOUNDRY_PROJECT_ENDPOINT"], "credential": ANY, "allow_preview": True}]
    assert azure_fakes["gets"] == [(CONNECTION, True)]
    # Enabling the feature is visible in the container log, which names the connection but never the key.
    assert f"MCP gateway authentication enabled through project connection {CONNECTION}" in caplog.text
    no_leak(caplog.text)


@pytest.mark.parametrize("config", [
    settings("policy-claim-lookup"),
    settings("claim-action-draft", MCP_GATEWAY_CONNECTION=""),
    settings("policy-wording", MCP_GATEWAY_CONNECTION=CONNECTION),
    settings("orchestrator", MCP_GATEWAY_CONNECTION=CONNECTION),
    settings("triage", MCP_GATEWAY_CONNECTION=CONNECTION),
])
def test_startup_never_touches_foundry_connections_unless_a_gated_role_configures_one(azure_fakes, config):
    assert hosted_main.load_gateway_headers(config) is None
    assert azure_fakes["credentials"] == azure_fakes["projects"] == azure_fakes["gets"] == []


def test_startup_fails_clearly_when_the_connection_does_not_match_the_mcp_server(azure_fakes):
    config = gated("policy-claim-lookup")
    azure_fakes["connection"] = connection(config, target=REVIEW_URL)
    with pytest.raises(ValueError, match="MCP endpoint or its gateway base URL") as failure:
        hosted_main.load_gateway_headers(config)
    no_leak(failure.value)
    azure_fakes["connection"] = connection(config, **credentials())
    with pytest.raises(ValueError, match="did not supply") as failure:
        hosted_main.load_gateway_headers(config)
    no_leak(failure.value)


def test_startup_does_not_continue_without_the_key_when_foundry_cannot_supply_it(azure_fakes):
    azure_fakes["error"] = RuntimeError("connection lookup failed")
    with pytest.raises(RuntimeError, match="lookup failed"):
        hosted_main.load_gateway_headers(gated("claim-action-draft"))


def test_a_pre_cleanup_environment_selects_the_same_gated_role_and_connection(azure_fakes):
    legacy = Settings.from_env({
        **{key: value for key, value in BASE.items() if not key.startswith("MCP_")},
        "ORDER_AGENT_ROLE": "orderlookup", "MCP_TECH_LOOKUP_URL": LOOKUP_URL, "MCP_GATEWAY_CONNECTION": CONNECTION,
    })
    assert legacy.role == "policy-claim-lookup" and legacy.mcp_route == "api-management"
    azure_fakes["connection"] = connection(legacy)
    assert hosted_main.load_gateway_headers(legacy) == {HEADER: KEY}


@pytest.mark.parametrize("enabled", [True, False])
def test_main_hands_the_startup_credential_to_the_agent(monkeypatch, azure_fakes, enabled):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    environment = {**BASE, "AGENT_ROLE": "policy-claim-lookup", "MCP_POLICY_LOOKUP_URL": LOOKUP_URL,
                   **({"MCP_GATEWAY_CONNECTION": CONNECTION} if enabled else {})}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    azure_fakes["connection"] = connection(Settings.from_env(environment)) if enabled else None
    built = {}

    class Host:
        def __init__(self, agent, config, *, resources=()):
            built.update(agent=agent, config=config)

        def run(self, **options):
            built["run"] = options

    monkeypatch.setattr(hosted_main, "DefaultAzureCredential", lambda **options: SimpleNamespace())
    monkeypatch.setattr(hosted_main, "AIProjectClient", lambda **options: SimpleNamespace())
    monkeypatch.setattr(hosted_main, "build_agent", lambda *args, **options: built.update(options=options) or "agent")
    monkeypatch.setattr(hosted_main, "InsuranceResponsesHost", Host)
    hosted_main.main()
    assert built["agent"] == "agent" and built["run"] == {"host": "0.0.0.0", "port": 8088}
    assert built["options"]["gateway_headers"] == ({HEADER: KEY} if enabled else None)
    assert built["options"]["knowledge_headers"] is None
    assert len(azure_fakes["gets"]) == int(enabled)
    no_leak(built["config"], built["config"].metadata, dict(built["config"].env))
