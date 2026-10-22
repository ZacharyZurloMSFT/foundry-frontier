"""MCP gateway subscription key for the Copilot-SDK harness: opt-in, header-only and never surfaced."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import insurance
from app.harness import insurance_tools, routes, runtime, store, tools
from app.harness.models import HarnessError
from app.settings import Settings
from copilot.tools import ToolInvocation
from fastapi import FastAPI

HEADER = "Ocp-Apim-Subscription-Key"
KEY = "unit-apim-subscription-key-0123456789"
WID = "wf-harness-test"
# APIM serves the MCP servers without the backend's trailing /mcp.
WORK_ORDER = "https://apim-northwind.azure-api.net/northwind/mcp/work-order"
INSURANCE_WORK_ORDER = "https://apim-northwind.azure-api.net/insurance/mcp/work-order"
KEYS = [("", {}), ("   ", {}), (KEY, {HEADER: KEY}), (f"  {KEY}\n", {HEADER: KEY})]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("MCP_GATEWAY_SUBSCRIPTION_KEY", raising=False)


def retail(**extra):
    return Settings(_env_file=None, **{
        "COPILOT_HARNESS_ENABLED": True, "FOUNDRY_PROJECT_ENDPOINT": "https://unit.test/api/projects/northwind",
        "MCP_WORK_ORDER_URL": WORK_ORDER, "COSMOS_ENDPOINT": "https://unit.test/cosmos", **extra})


def insurance_settings(**extra):
    return Settings(_env_file=None, **{
        "NORTHWIND_BUSINESS_DOMAIN": "insurance", "COPILOT_HARNESS_ENABLED": True,
        "NORTHWIND_INSURANCE_AGENTS_READY": True, "ORDER_AGENTS_HOSTED": True,
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator",
        "FOUNDRY_PROJECT_ENDPOINT": "https://unit.test/api/projects/northwind-orders",
        "MCP_WORK_ORDER_URL": INSURANCE_WORK_ORDER, "COSMOS_ENDPOINT": "https://cosmos.unit.test", **extra})


def install(monkeypatch, settings):
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


def no_leak(*values):
    assert all(KEY not in str(value) for value in values)


def retail_specialists(settings, events=None):
    unsaved = SimpleNamespace(approval=AsyncMock(return_value=None))
    return tools.SpecialistTools(settings, "harness-test", WID, (events if events is not None else []).append, unsaved)


async def retail_draft(specialists):
    return await specialists.handler("resolution_draft")(invocation({"prompt": "draft"}))


async def insurance_draft(settings, events=None):
    unsaved = SimpleNamespace(require_active=AsyncMock(), approval=AsyncMock(return_value=None))
    specialists = insurance_tools.InsuranceTools(
        settings, "harness-unit", WID, (events if events is not None else []).append, unsaved)
    await specialists.handler("policy_claim_lookup")(invocation({"claim_id": "CLM-2001"}))
    return await specialists.handler("claim_action_draft")(invocation(draft_args()))


def test_key_is_an_opt_in_secret_setting(monkeypatch):
    assert Settings(_env_file=None).harness_gateway_key.get_secret_value() == ""
    monkeypatch.setenv("MCP_GATEWAY_SUBSCRIPTION_KEY", KEY)
    for settings in (Settings(_env_file=None), Settings(_env_file=None, MCP_GATEWAY_SUBSCRIPTION_KEY=KEY)):
        assert settings.harness_gateway_key.get_secret_value() == KEY
        no_leak(repr(settings), str(settings), settings.model_dump(), settings.model_dump_json(),
                repr(settings.harness_gateway_key))


@pytest.mark.parametrize("key,expected", KEYS)
def test_header_exists_only_when_a_key_is_configured(key, expected):
    assert tools.gateway_headers(retail(MCP_GATEWAY_SUBSCRIPTION_KEY=key)) == expected
    assert tools.GATEWAY_KEY_HEADER == HEADER


@pytest.mark.parametrize("url", ["http://mcp.unit.test/mcp/", "ftp://mcp.unit.test/mcp/", "mcp.unit.test", "", "https://[bad"])
def test_key_is_only_ever_sent_over_https(url):
    settings = retail(MCP_WORK_ORDER_URL=url, MCP_GATEWAY_SUBSCRIPTION_KEY=KEY)
    with pytest.raises(HarnessError, match="HTTPS") as failure:
        tools.gateway_headers(settings)
    assert failure.value.code == "mcp_unconfigured"
    no_leak(failure.value)
    # Without a key the URL is not this feature's concern; today's behavior is untouched.
    assert tools.gateway_headers(retail(MCP_WORK_ORDER_URL=url)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", KEYS)
async def test_retail_draft_sends_the_key_beside_the_workflow_binding(monkeypatch, key, expected):
    invoke = AsyncMock(return_value=SimpleNamespace(status="completed", output_text="Draft", output=[], usage=None))
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", invoke)
    outcome = await retail_draft(retail_specialists(retail(MCP_GATEWAY_SUBSCRIPTION_KEY=key)))
    assert outcome.result_type == "failure"  # the stub never saved a draft; only the outgoing call is inspected
    mcp_tool = invoke.await_args.kwargs["mcp_tool"]
    assert mcp_tool["server_url"] == WORK_ORDER
    assert mcp_tool["allowed_tools"] == ["propose_resolution"]
    assert mcp_tool["headers"] == {"x-harness-workflow": WID, **expected}
    assert invoke.await_args.kwargs["workflow_id"] == WID


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", KEYS)
async def test_insurance_draft_sends_the_key_beside_the_workflow_binding(monkeypatch, key, expected):
    settings = install(monkeypatch, insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=key))
    native = AsyncMock(return_value=SimpleNamespace(status="completed", output=[]))
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    outcome = await insurance_draft(settings)
    assert outcome.result_type == "failure"  # the stub never saved a review; only the outgoing call is inspected
    mcp_tool = native.await_args.kwargs["mcp_tool"]
    assert mcp_tool["server_url"] == INSURANCE_WORK_ORDER
    assert mcp_tool["allowed_tools"] == ["draft_claim_action"]
    assert mcp_tool["headers"] == {"x-orders-workflow": WID, **expected}
    assert native.await_args.kwargs["workflow_id"] == WID


@pytest.mark.asyncio
async def test_retail_draft_fails_closed_instead_of_sending_the_key_over_http(monkeypatch, caplog):
    invoke = AsyncMock()
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", invoke)
    events = []
    settings = retail(MCP_WORK_ORDER_URL="http://mcp.unit.test/mcp/", MCP_GATEWAY_SUBSCRIPTION_KEY=KEY)
    with caplog.at_level(logging.DEBUG):
        outcome = await retail_draft(retail_specialists(settings, events))
    assert outcome.result_type == "failure" and "HTTPS" in outcome.text_result_for_llm
    invoke.assert_not_awaited()
    no_leak(outcome, caplog.text, [event.model_dump_json() for event in events])


@pytest.mark.asyncio
async def test_insurance_draft_fails_closed_instead_of_sending_the_key_over_http(monkeypatch, caplog):
    settings = install(monkeypatch, insurance_settings(
        MCP_WORK_ORDER_URL="http://mcp.unit.test/mcp/", MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    native = AsyncMock()
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    events = []
    with caplog.at_level(logging.DEBUG):
        outcome = await insurance_draft(settings, events)
    assert outcome.result_type == "failure" and "HTTPS" in outcome.text_result_for_llm
    native.assert_not_awaited()
    no_leak(outcome, caplog.text, [event.model_dump_json() for event in events])


@pytest.mark.asyncio
async def test_failures_never_surface_the_key_even_when_the_provider_error_contains_it(monkeypatch, caplog):
    leaky = RuntimeError(f"401 from the MCP server for {HEADER}: {KEY}")
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", AsyncMock(side_effect=leaky))
    events = []
    with caplog.at_level(logging.DEBUG):
        outcome = await retail_draft(retail_specialists(retail(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY), events))
    assert outcome.result_type == "failure"
    assert outcome.text_result_for_llm == "resolution_draft failed; inspect the server logs."
    assert any(record.stage == "specialist_resolution_draft" for record in caplog.records if hasattr(record, "stage"))
    no_leak(outcome, caplog.text, [event.model_dump_json() for event in events])


@pytest.mark.parametrize("domain", ["retail", "insurance"])
def test_status_never_exposes_the_key_or_its_header(monkeypatch, domain):
    settings = retail if domain == "retail" else insurance_settings
    install(monkeypatch, settings(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    status = runtime.HarnessRuntime().status()
    assert status["state"] == "configured", status["message"]
    rendered = json.dumps(status)
    no_leak(rendered)
    assert HEADER.lower() not in rendered.lower() and "SUBSCRIPTION_KEY" not in rendered


@pytest.mark.asyncio
async def test_status_endpoint_never_exposes_the_key(monkeypatch):
    install(monkeypatch, retail(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/harness/status")
    assert response.status_code == 200 and response.json()["state"] == "configured"
    no_leak(response.text, response.headers)


@pytest.mark.parametrize("url,accepted", [
    ("https://ca-mcp-work-order.example.azurecontainerapps.io/mcp/", True),
    ("https://ca-mcp-work-order.example.azurecontainerapps.io/mcp", True),
    (INSURANCE_WORK_ORDER, True),
    (INSURANCE_WORK_ORDER + "/", True),
    ("http://apim-northwind.azure-api.net/insurance/mcp/work-order", False),
    ("https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup", False),
    ("https://apim-northwind.azure-api.net/insurance/work-order", False),
    ("https://apim-northwind.azure-api.net/insurance/mcp/work-order?subscription-key=in-the-url", False),
    ("https://user:pw@apim-northwind.azure-api.net/insurance/mcp/work-order", False),
    ("", False),
])
def test_insurance_status_accepts_direct_and_gateway_work_order_endpoints(monkeypatch, url, accepted):
    install(monkeypatch, insurance_settings(MCP_WORK_ORDER_URL=url))
    status = runtime.HarnessRuntime().status()
    assert (status["state"] == "configured") is accepted, status["message"]
    assert ("MCP_WORK_ORDER_URL must identify the HTTPS insurance draft MCP endpoint." in status["message"]) is not accepted
