"""MCP gateway subscription key for the Copilot-SDK harness: opt-in, header-only and never surfaced."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import insurance
from app.harness import insurance_tools, mcp, routes, runtime, store
from app.harness.models import HarnessError
from app.settings import Settings
from copilot.tools import ToolInvocation
from fastapi import FastAPI

HEADER = "Ocp-Apim-Subscription-Key"
KEY = "unit-apim-subscription-key-0123456789"
WID = "wf-harness-test"
# APIM serves the MCP servers without the backend's trailing /mcp.
CLAIM_REVIEW = "https://apim-northwind.azure-api.net/insurance/mcp/work-order"
KEYS = [("", {}), ("   ", {}), (KEY, {HEADER: KEY}), (f"  {KEY}\n", {HEADER: KEY})]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("MCP_GATEWAY_SUBSCRIPTION_KEY", raising=False)


def insurance_settings(**extra):
    return Settings(_env_file=None, **{
        "COPILOT_HARNESS_ENABLED": True,
        "NORTHWIND_INSURANCE_AGENTS_READY": True, "HOSTED_AGENTS_ENABLED": True,
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator",
        "FOUNDRY_PROJECT_ENDPOINT": "https://unit.test/api/projects/northwind-orders",
        "MCP_CLAIM_REVIEW_URL": CLAIM_REVIEW, "COSMOS_ENDPOINT": "https://cosmos.unit.test", **extra})


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
    assert mcp.gateway_headers(insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=key)) == expected
    assert mcp.GATEWAY_KEY_HEADER == HEADER


@pytest.mark.parametrize("url", ["http://mcp.unit.test/mcp/", "ftp://mcp.unit.test/mcp/", "mcp.unit.test", "", "https://[bad"])
def test_key_is_only_ever_sent_over_https(url):
    settings = insurance_settings(MCP_CLAIM_REVIEW_URL=url, MCP_GATEWAY_SUBSCRIPTION_KEY=KEY)
    with pytest.raises(HarnessError, match="HTTPS") as failure:
        mcp.gateway_headers(settings)
    assert failure.value.code == "mcp_unconfigured"
    no_leak(failure.value)
    # Without a key the URL is not this feature's concern; today's behavior is untouched.
    assert mcp.gateway_headers(insurance_settings(MCP_CLAIM_REVIEW_URL=url)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", KEYS)
async def test_insurance_draft_sends_the_key_beside_the_workflow_binding(monkeypatch, key, expected):
    settings = install(monkeypatch, insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=key))
    native = AsyncMock(return_value=SimpleNamespace(status="completed", output=[]))
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", native)
    outcome = await insurance_draft(settings)
    assert outcome.result_type == "failure"  # the stub never saved a review; only the outgoing call is inspected
    mcp_tool = native.await_args.kwargs["mcp_tool"]
    assert mcp_tool["server_url"] == CLAIM_REVIEW
    assert mcp_tool["allowed_tools"] == ["draft_claim_action"]
    # The deprecated header is still sent so MCP builds deployed before the rename keep binding the workflow.
    assert mcp_tool["headers"] == {"x-workflow-id": WID, "x-orders-workflow": WID, **expected}
    assert native.await_args.kwargs["workflow_id"] == WID


@pytest.mark.asyncio
async def test_insurance_draft_fails_closed_instead_of_sending_the_key_over_http(monkeypatch, caplog):
    settings = install(monkeypatch, insurance_settings(
        MCP_CLAIM_REVIEW_URL="http://mcp.unit.test/mcp/", MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
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
    settings = install(monkeypatch, insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    leaky = RuntimeError(f"401 from the MCP server for {HEADER}: {KEY}")
    monkeypatch.setattr(insurance_tools, "invoke_prompt_agent_once", AsyncMock(side_effect=leaky))
    events = []
    with caplog.at_level(logging.DEBUG):
        outcome = await insurance_draft(settings, events)
    assert outcome.result_type == "failure"
    assert outcome.text_result_for_llm.startswith("Insurance tool unavailable.")
    assert any(record.stage == "insurance_tool_claim_action_draft"
               for record in caplog.records if hasattr(record, "stage"))
    no_leak(outcome, caplog.text, [event.model_dump_json() for event in events])


def test_status_never_exposes_the_key_or_its_header(monkeypatch):
    install(monkeypatch, insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    status = runtime.HarnessRuntime().status()
    assert status["state"] == "configured", status["message"]
    rendered = json.dumps(status)
    no_leak(rendered)
    assert HEADER.lower() not in rendered.lower() and "SUBSCRIPTION_KEY" not in rendered


@pytest.mark.asyncio
async def test_status_endpoint_never_exposes_the_key(monkeypatch):
    install(monkeypatch, insurance_settings(MCP_GATEWAY_SUBSCRIPTION_KEY=KEY))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/harness/status")
    assert response.status_code == 200 and response.json()["state"] == "configured"
    no_leak(response.text, response.headers)


@pytest.mark.parametrize("url,accepted", [
    ("https://ca-insurance-review.example.azurecontainerapps.io/mcp/", True),
    ("https://ca-insurance-review.example.azurecontainerapps.io/mcp", True),
    (CLAIM_REVIEW, True),
    (CLAIM_REVIEW + "/", True),
    ("http://apim-northwind.azure-api.net/insurance/mcp/work-order", False),
    ("https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup", False),
    ("https://apim-northwind.azure-api.net/insurance/work-order", False),
    ("https://apim-northwind.azure-api.net/insurance/mcp/work-order?subscription-key=in-the-url", False),
    ("https://user:pw@apim-northwind.azure-api.net/insurance/mcp/work-order", False),
    ("", False),
])
def test_insurance_status_accepts_direct_and_gateway_claim_review_endpoints(monkeypatch, url, accepted):
    install(monkeypatch, insurance_settings(MCP_CLAIM_REVIEW_URL=url))
    status = runtime.HarnessRuntime().status()
    assert (status["state"] == "configured") is accepted, status["message"]
    assert ("MCP_CLAIM_REVIEW_URL must identify the HTTPS claim-review MCP endpoint." in status["message"]) is not accepted
