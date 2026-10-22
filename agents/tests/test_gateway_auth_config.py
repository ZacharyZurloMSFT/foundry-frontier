"""MCP gateway authentication configuration contracts, runnable without installing the host."""
from pathlib import Path

import pytest
import yaml

from agents.hosted.config import AGENTS_DIR, ENV_KEYS, Settings

ROOT = Path(__file__).resolve().parents[2]
CONNECTION = "northwind-apim-gateway"
BASE = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://account.services.ai.azure.com/api/projects/northwind-orders",
    "MODEL_DEPLOYMENT_NAME": "baseline-deployment",
    "MCP_TECH_LOOKUP_URL": "https://apim-northwind.azure-api.net/northwind/mcp/tech-lookup",
    "MCP_WORK_ORDER_URL": "https://apim-northwind.azure-api.net/northwind/mcp/work-order",
    "AZURE_SEARCH_ENDPOINT": "https://search.example",
    "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-kb",
    "ORDERS_KB_CONNECTION_NAME": "northwind-kb-mcp",
    "ORDERS_OPENAI_ENDPOINT": "https://account.openai.azure.com/openai/v1/",
}
GATED_ROLES = ("orderlookup", "resolutiondraft")


def config(role, **overrides):
    return Settings.from_env({**BASE, "ORDER_AGENT_ROLE": role, **overrides})


def test_gateway_connection_is_an_allowlisted_environment_key():
    assert "MCP_GATEWAY_CONNECTION" in ENV_KEYS
    assert config("orderlookup", MCP_GATEWAY_CONNECTION=CONNECTION).env["MCP_GATEWAY_CONNECTION"] == CONNECTION


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


@pytest.mark.parametrize("role", ["manager", "triage", "policylookup"])
def test_roles_without_a_gated_mcp_server_ignore_the_connection(role):
    # One shared environment must never change, or break, the roles that do not call a gated MCP server.
    assert config(role, MCP_GATEWAY_CONNECTION=CONNECTION).mcp_gateway_connection == ""
    assert config(role, MCP_GATEWAY_CONNECTION="not a connection name").mcp_gateway_connection == ""


def test_connection_never_enters_response_metadata():
    for role in GATED_ROLES:
        assert CONNECTION not in str(config(role, MCP_GATEWAY_CONNECTION=CONNECTION).metadata)


def test_azd_and_hosted_manifests_plumb_the_connection_to_only_the_two_mcp_agents():
    azure = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))["services"]
    for service in ("orchestrator", "triage", "orderlookup", "policylookup", "resolutiondraft"):
        azd_env = {item["name"]: item["value"] for item in azure[service]["environmentVariables"]}
        manifest = yaml.safe_load((AGENTS_DIR / service / "agent.yaml").read_text(encoding="utf-8"))
        manifest_env = {item["name"]: item["value"] for item in manifest["environment_variables"]}
        gated = service in GATED_ROLES
        # An unset azd variable resolves to an empty value, which leaves gateway authentication off.
        assert azd_env.get("MCP_GATEWAY_CONNECTION") == ("${MCP_GATEWAY_CONNECTION}" if gated else None), service
        assert manifest_env.get("MCP_GATEWAY_CONNECTION") == ("${MCP_GATEWAY_CONNECTION}" if gated else None), service
