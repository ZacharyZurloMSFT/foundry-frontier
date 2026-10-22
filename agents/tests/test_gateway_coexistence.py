"""The AI Gateway tool servers and the API Management gateway can both exist, but never both route one role,
and neither ever receives the other's key."""
import asyncio

import pytest

from agents.hosted.runtime import build_agent
from agents.tests.test_hosted import MockProject, settings
from agents.tests.test_mcp_gateway import (
    INSURANCE_KB,
    RETAIL_LOOKUP,
    WORK_ORDER,
    entry,
    mcp_settings,
)

APIM_CONNECTION = "apim-gateway-connection"
APIM_KEY = {"Ocp-Apim-Subscription-Key": "unit-apim-subscription-key-0123456789"}
AI_KEY = {"api-key": "unit-ai-gateway-runtime-key"}


def build(config, **options):
    project = MockProject(lambda *_: [])
    try:
        return build_agent(config, project, **options)
    finally:
        asyncio.run(project.close())


def test_route_names_tell_the_two_gateways_apart():
    assert settings("orderlookup").mcp_route == "direct"
    assert mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")}).mcp_route == "ai-gateway"
    for role in ("orderlookup", "resolutiondraft"):
        config = settings(role, MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
        assert config.mcp_route == "api-management" and config.metadata["mcp_route"] == "api-management"
    # API Management does not front the knowledge base, so its connection means nothing to that role.
    policy = settings("policylookup", MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    assert policy.mcp_route == "direct" and policy.mcp_gateway_connection == ""


@pytest.mark.parametrize("role,gateway_entry", [
    ("orderlookup", entry(RETAIL_LOOKUP, "lookup_")),
    ("resolutiondraft", entry(WORK_ORDER, "wo_", forwards_headers=True)),
])
def test_one_role_cannot_be_routed_by_both_gateways(role, gateway_entry):
    with pytest.raises(ValueError, match="configure only one"):
        mcp_settings(role, {role: gateway_entry}, MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    # Either gateway on its own is fine.
    assert mcp_settings(role, {role: gateway_entry}).mcp_route == "ai-gateway"
    assert settings(role, MCP_GATEWAY_CONNECTION=APIM_CONNECTION).mcp_route == "api-management"


def test_an_api_management_connection_does_not_clash_with_the_knowledge_role_served_by_the_ai_gateway():
    config = mcp_settings("policylookup", {"policylookup": entry(INSURANCE_KB, "knowledge_")}, "insurance",
                          MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    assert config.mcp_route == "ai-gateway" and config.mcp_gateway_connection == ""


def test_the_ai_gateway_never_receives_the_api_management_key():
    config = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")})
    with pytest.raises(ValueError, match="connection headers"):
        build(config, gateway_headers=APIM_KEY)
    agent = build(config, mcp_headers=AI_KEY, gateway_headers=APIM_KEY)
    assert agent.default_options["tools"][0]["headers"] == AI_KEY


def test_api_management_never_receives_the_ai_gateway_key():
    config = settings("orderlookup", MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    with pytest.raises(ValueError, match="gateway connection headers"):
        build(config, mcp_headers=AI_KEY)
    agent = build(config, mcp_headers=AI_KEY, gateway_headers=APIM_KEY)
    assert agent.default_options["tools"][0]["headers"] == APIM_KEY
