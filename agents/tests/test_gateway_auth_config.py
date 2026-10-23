"""MCP gateway authentication configuration contracts, runnable without installing the host."""
import pytest

from agents.hosted.config import ENV_KEYS, ROLE_ALIASES, Settings

CONNECTION = "northwind-apim-gateway"
BASE = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://account.services.ai.azure.com/api/projects/nw-insurance-claims",
    "MODEL_DEPLOYMENT_NAME": "baseline-deployment",
    "MCP_POLICY_LOOKUP_URL": "https://apim-northwind.azure-api.net/insurance/mcp/tech-lookup",
    "MCP_CLAIM_REVIEW_URL": "https://apim-northwind.azure-api.net/insurance/mcp/work-order",
    "AZURE_SEARCH_ENDPOINT": "https://search.example",
    "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
    "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
    "AGENT_KB_CONNECTION_NAME": "northwind-insurance-kb-mcp",
    "AGENT_OPENAI_ENDPOINT": "https://account.openai.azure.com/openai/v1/",
}
GATED_ROLES = ("policy-claim-lookup", "claim-action-draft")


def config(role, **overrides):
    return Settings.from_env({**BASE, "AGENT_ROLE": role, **overrides})


def test_gateway_connection_is_an_allowlisted_environment_key():
    assert "MCP_GATEWAY_CONNECTION" in ENV_KEYS
    assert config("policy-claim-lookup", MCP_GATEWAY_CONNECTION=CONNECTION).env["MCP_GATEWAY_CONNECTION"] == CONNECTION


@pytest.mark.parametrize("role", GATED_ROLES)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_gateway_authentication_is_off_unless_a_connection_is_configured(role, value):
    overrides = {} if value is None else {"MCP_GATEWAY_CONNECTION": value}
    assert config(role, **overrides).mcp_gateway_connection == ""


@pytest.mark.parametrize("role", GATED_ROLES)
def test_configured_connection_name_is_trimmed(role):
    assert config(role, MCP_GATEWAY_CONNECTION=f"  {CONNECTION}\n").mcp_gateway_connection == CONNECTION


@pytest.mark.parametrize("role", GATED_ROLES)
@pytest.mark.parametrize("name", [
    "bad name", "../connections", "a/b", "a?b=c", "${MCP_GATEWAY_CONNECTION}", "-leading", "key\nvalue", "x" * 129,
])
def test_connection_name_must_be_a_foundry_connection_name(role, name):
    with pytest.raises(ValueError, match="MCP_GATEWAY_CONNECTION"):
        config(role, MCP_GATEWAY_CONNECTION=name)


@pytest.mark.parametrize("role", ["orchestrator", "triage", "policy-wording"])
def test_roles_without_a_gated_mcp_server_ignore_the_connection(role):
    # One shared environment must never change, or break, the roles that do not call a gated MCP server.
    assert config(role, MCP_GATEWAY_CONNECTION=CONNECTION).mcp_gateway_connection == ""
    assert config(role, MCP_GATEWAY_CONNECTION="not a connection name").mcp_gateway_connection == ""


@pytest.mark.parametrize("legacy", ["orderlookup", "resolutiondraft", "manager", "policylookup"])
def test_deprecated_role_ids_are_gated_exactly_like_their_canonical_role(legacy):
    canonical = ROLE_ALIASES[legacy]
    old = config(legacy, MCP_GATEWAY_CONNECTION=CONNECTION)
    new = config(canonical, MCP_GATEWAY_CONNECTION=CONNECTION)
    assert old.role == canonical and old.mcp_gateway_connection == new.mcp_gateway_connection
    assert old.mcp_route == new.mcp_route and old.metadata == new.metadata


def test_connection_never_enters_response_metadata():
    for role in GATED_ROLES:
        assert CONNECTION not in str(config(role, MCP_GATEWAY_CONNECTION=CONNECTION).metadata)
