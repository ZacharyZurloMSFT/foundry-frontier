"""The AI Gateway tool servers and the API Management gateway can both exist, but never both route one role,
and neither ever receives the other's key."""
import asyncio

import pytest

from agents.hosted.runtime import build_agent
from agents.tests.test_hosted import MockProject, settings
from agents.tests.test_mcp_gateway import (
    KB,
    LOOKUP,
    REVIEW,
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
    assert settings("policy-claim-lookup").mcp_route == "direct"
    assert mcp_settings("policy-claim-lookup", {"policy-claim-lookup": entry(LOOKUP, "lookup_")}).mcp_route == "ai-gateway"
    for role in ("policy-claim-lookup", "claim-action-draft"):
        config = settings(role, MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
        assert config.mcp_route == "api-management" and config.metadata["mcp_route"] == "api-management"
    # API Management does not front the knowledge base, so its connection means nothing to that role.
    policy = settings("policy-wording", MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    assert policy.mcp_route == "direct" and policy.mcp_gateway_connection == ""


@pytest.mark.parametrize("role,gateway_entry", [
    ("policy-claim-lookup", entry(LOOKUP, "lookup_")),
    ("claim-action-draft", entry(REVIEW, "review_", workflow_binding="signed")),
])
def test_one_role_cannot_be_routed_by_both_gateways(role, gateway_entry):
    with pytest.raises(ValueError, match="configure only one"):
        mcp_settings(role, {role: gateway_entry}, MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    # Either gateway on its own is fine.
    assert mcp_settings(role, {role: gateway_entry}).mcp_route == "ai-gateway"
    assert settings(role, MCP_GATEWAY_CONNECTION=APIM_CONNECTION).mcp_route == "api-management"


def test_the_deprecated_role_key_is_just_as_exclusive_with_the_api_management_route():
    with pytest.raises(ValueError, match="configure only one"):
        settings("policy-claim-lookup", MCP_GATEWAY_CONNECTION=APIM_CONNECTION,
                 ORDERS_MCP_GATEWAY='{"orderlookup": {"url": "' + LOOKUP + '", "tool_prefix": "lookup_",'
                                   ' "connection": "unit-connection"}}')


def test_an_api_management_connection_does_not_clash_with_the_knowledge_role_served_by_the_ai_gateway():
    config = mcp_settings("policy-wording", {"policy-wording": entry(KB, "knowledge_")},
                          MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    assert config.mcp_route == "ai-gateway" and config.mcp_gateway_connection == ""


def test_the_ai_gateway_never_receives_the_api_management_key():
    config = mcp_settings("policy-claim-lookup", {"policy-claim-lookup": entry(LOOKUP, "lookup_")})
    with pytest.raises(ValueError, match="connection headers"):
        build(config, gateway_headers=APIM_KEY)
    agent = build(config, mcp_headers=AI_KEY, gateway_headers=APIM_KEY)
    assert agent.default_options["tools"][0]["headers"] == AI_KEY


def test_api_management_never_receives_the_ai_gateway_key():
    config = settings("policy-claim-lookup", MCP_GATEWAY_CONNECTION=APIM_CONNECTION)
    with pytest.raises(ValueError, match="gateway connection headers"):
        build(config, mcp_headers=AI_KEY)
    agent = build(config, mcp_headers=AI_KEY, gateway_headers=APIM_KEY)
    assert agent.default_options["tools"][0]["headers"] == APIM_KEY
